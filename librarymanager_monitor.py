#!/usr/bin/env python3
"""Background filesystem watcher for Stash Library Manager."""

import argparse
import logging
import json
import os
import sys
import signal
import sqlite3
import subprocess
import shutil
import threading
import queue
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from stashapi.stashapp import StashInterface

import re
import unicodedata
from librarymanager_core import (
    generate_video_contact_sheet, connect, consume_expected_create, consume_expected_move,
    expect_filesystem_create, expect_filesystem_move, fingerprint_value,
                                 opensubtitles_hash, record_activity, record_filesystem_event,
                                 resolve_filesystem_event, refresh_scene_inventory, utc_now)


logger = logging.getLogger("librarymanager.monitor")


VIDEO_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".ogm", ".ogv", ".rm", ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}

TEMPORARY_DOWNLOAD_EXTENSIONS = {".part", ".partial", ".crdownload", ".download", ".tmp", ".temp", ".!qb"}
COMPANION_EXTENSIONS = {
    ".funscript", ".srt", ".vtt", ".scc", ".ttml", ".dfxp", ".lrc", ".txt",
    ".jpg", ".jpeg", ".png", ".webp", ".nfo", ".json", ".xml", ".sub", ".idx"
}
WATCHED_EXTENSIONS = VIDEO_EXTENSIONS | COMPANION_EXTENSIONS | TEMPORARY_DOWNLOAD_EXTENSIONS


def _sidecar_match_key(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(name or ""))
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", "", no_accents.casefold())


def match_companion_to_video(candidate: Path, video: Path):
    c_suffix = candidate.suffix.lower()
    c_name = candidate.name.lower()
    v_name = video.name.lower()
    c_stem = candidate.stem.lower()
    v_stem = video.stem.lower()

    if c_name == v_name + c_suffix:
        return True, ""
    if c_stem == v_stem:
        return True, ""
    if _sidecar_match_key(c_stem) == _sidecar_match_key(v_stem):
        return True, ""
    if c_stem.startswith(v_stem) and len(c_stem) > len(v_stem):
        remainder = candidate.stem[len(video.stem):]
        if remainder.startswith(".") or remainder.startswith("-") or remainder.startswith("_"):
            return True, remainder

    k_cand = _sidecar_match_key(c_stem)
    k_vid = _sidecar_match_key(v_stem)
    if k_vid and k_cand.startswith(k_vid) and len(k_cand) - len(k_vid) <= 6:
        return True, ""

    return False, None


def find_scene_for_companion(con, companion_name: str):
    c_p = Path(companion_name)
    c_stem = c_p.stem
    c_key = _sidecar_match_key(c_stem)

    if c_stem.lower().endswith(tuple(VIDEO_EXTENSIONS)):
        cur = con.execute("SELECT file_id, scene_id, path, basename FROM files WHERE exists_on_disk=1 AND basename = ?", (c_stem,))
        row = cur.fetchone()
        if row:
            return row, ""

    cur = con.execute("SELECT file_id, scene_id, path, basename FROM files WHERE exists_on_disk=1 AND basename LIKE ?", (c_stem + ".%",))
    for row in cur.fetchall():
        v_p = Path(row["basename"])
        if v_p.stem.lower() == c_stem.lower():
            return row, ""

    parts = c_stem.rsplit(".", 1)
    if len(parts) == 2 and len(parts[1]) in (2, 3, 6):
        base_stem = parts[0]
        remainder = "." + parts[1]
        cur = con.execute("SELECT file_id, scene_id, path, basename FROM files WHERE exists_on_disk=1 AND basename LIKE ?", (base_stem + ".%",))
        for row in cur.fetchall():
            v_p = Path(row["basename"])
            if v_p.stem.lower() == base_stem.lower():
                return row, remainder

    for row in con.execute("SELECT file_id, scene_id, path, basename FROM files WHERE exists_on_disk=1"):
        v_stem = Path(row["basename"]).stem
        v_key = _sidecar_match_key(v_stem)
        if v_key == c_key:
            return row, ""
        if c_key.startswith(v_key) and len(c_key) - len(v_key) <= 6:
            return row, ""

    return None, None
INCOMING_SCAN_FLAGS = {
    "scanGenerateCovers": True,
    "scanGeneratePreviews": True,
    "scanGenerateSprites": True,
    "scanGeneratePhashes": True,
    "scanGenerateThumbnails": True,
}

SCENE_BY_PATH_QUERY = """
query LibraryManagerSceneByPath($path: String!) {
  findScenes(scene_filter: {path: {value: $path, modifier: EQUALS}}, filter: {per_page: 10}) {
    scenes {
      id title details date director code rating100 organized urls
      studio { name }
      performers { id name }
      tags { id name }
      galleries { id title }
      stash_ids { endpoint stash_id }
      groups { group { id name } scene_index }
      files { id path basename size duration fingerprints { type value } }
    }
  }
}
"""


def notify(enabled, message):
    if not enabled:
        return
    try:
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e",
                            "display notification (item 1 of argv) with title (item 2 of argv)",
                            "-e", "end run", "--", str(message), "Stash Library Manager"],
                           capture_output=True, text=True, timeout=10, check=False)
        elif sys.platform == "win32":
            msg_esc = str(message).replace('"', '`"')
            ps_cmd = (
                f'[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; '
                f'$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); '
                f'$textNodes = $template.GetElementsByTagName("text"); '
                f'$textNodes.Item(0).AppendChild($template.CreateTextNode("Stash Library Manager")) > $null; '
                f'$textNodes.Item(1).AppendChild($template.CreateTextNode("{msg_esc}")) > $null; '
                f'$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Stash Library Manager"); '
                f'$notification = [Windows.UI.Notifications.ToastNotification]::new($template); '
                f'$notifier.Show($notification)'
            )
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                           capture_output=True, text=True, timeout=10, check=False)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "Stash Library Manager", "Stash Library Manager", str(message)],
                           capture_output=True, text=True, timeout=5, check=False)
    except Exception:
        pass


def tracked_move(database_path, source, destination):
    """Verify a move from the exact inventoried path without changing any record."""
    target = Path(destination)
    if not target.is_file():
        return None, "Destination is not a file"
    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM files WHERE path=?", (source,)).fetchone()
        if not row:
            return None, "Source path is not in the inventory"
        if row["size"] is not None and target.stat().st_size != int(row["size"]):
            return None, "Destination size differs from the inventory"
        expected = fingerprint_value(row["fingerprints_json"], "oshash")
        if expected and opensubtitles_hash(target) != expected:
            return None, "Destination oshash differs from the inventory"
        return dict(row), "Exact source move verified by size" + (" and oshash" if expected else "")
    finally:
        connection.close()


class MoveWorker(threading.Thread):
    def __init__(self, database_path, stash, enabled, notifications):
        super().__init__(daemon=True)
        self.database_path, self.stash = database_path, stash
        self.enabled, self.notifications = enabled, notifications
        self.items = queue.Queue()
        self.stopping = False

    @property
    def automatic_move_reconciliation(self):
        return self.enabled

    @automatic_move_reconciliation.setter
    def automatic_move_reconciliation(self, value):
        self.enabled = bool(value)

    def submit(self, source, destination):
        self.items.put((source, destination))

    def stop(self):
        self.stopping = True
        self.items.put((None, None))

    def run(self):
        while not self.stopping:
            source, destination = self.items.get()
            if source is None:
                break
            try:
                row, reason = tracked_move(self.database_path, source, destination)
                if not row:
                    record_activity(self.database_path, "filesystem", "external move", "review",
                                    severity="warning", old_path=source, new_path=destination, detail=reason)
                    notify(self.notifications, f"File move needs review: {Path(destination).name}")
                    continue
                record_activity(self.database_path, "filesystem", "external move", "verified",
                                scene_id=row["scene_id"], file_id=row["file_id"], old_path=source,
                                new_path=destination, detail=reason)
                notify(self.notifications, f"File moved: {Path(source).name} → {Path(destination).parent.name}")
                if not self.enabled:
                    continue
                job_id = self.stash.metadata_scan(paths=[destination])
                completed = self.stash.wait_for_job(job_id, timeout=180)
                result = self.stash.call_GQL(
                    "query SceneFiles($id: ID!) { findScene(id: $id) { files { id path basename } } }",
                    {"id": str(row["scene_id"])})
                paths = {item.get("path") for item in ((result or {}).get("findScene") or {}).get("files") or []}
                if completed and destination in paths:
                    connection = connect(self.database_path)
                    try:
                        connection.execute("UPDATE files SET path=?,basename=?,exists_on_disk=1,last_seen_at=?,missing_since=NULL WHERE file_id=?",
                                           (destination, Path(destination).name, utc_now(), row["file_id"]))
                        connection.commit()
                    finally:
                        connection.close()
                    record_activity(self.database_path, "reconciliation", "targeted Stash scan", "updated",
                                    scene_id=row["scene_id"], file_id=row["file_id"], old_path=source,
                                    new_path=destination, detail=f"Stash scan job {job_id} confirmed the new path")
                    resolve_filesystem_event(self.database_path, "moved", source, destination)
                    notify(self.notifications, f"Stash updated: {Path(destination).name}")

                    # Automatically move companion files alongside the video
                    source_p = Path(source)
                    dest_p = Path(destination)
                    if source_p.parent.is_dir() and dest_p.parent.is_dir():
                        try:
                            for c in list(source_p.parent.iterdir()):
                                if not c.is_file() or c == source_p:
                                    continue
                                if c.suffix.lower() not in COMPANION_EXTENSIONS:
                                    continue
                                matched, rem = match_companion_to_video(c, source_p)
                                if matched:
                                    if c.name.lower().startswith(source_p.name.lower()):
                                        target_name = dest_p.name + c.suffix
                                    elif rem:
                                        target_name = dest_p.stem + rem + c.suffix
                                    else:
                                        target_name = dest_p.stem + c.suffix
                                    target_c = dest_p.parent / target_name
                                    if not target_c.exists():
                                        expect_filesystem_move(self.database_path, str(c), str(target_c))
                                        c.rename(target_c)
                                        record_activity(self.database_path, "companion", "moved companion", "recorded",
                                                        scene_id=row["scene_id"], old_path=str(c), new_path=str(target_c),
                                                        detail=f"Moved companion file alongside {dest_p.name}")
                        except OSError:
                            pass
                else:
                    detail = f"Stash scan job {job_id} did not attach the destination to the original scene"
                    record_activity(self.database_path, "reconciliation", "targeted Stash scan", "review",
                                    severity="warning", scene_id=row["scene_id"], file_id=row["file_id"],
                                    old_path=source, new_path=destination, detail=detail)
                    notify(self.notifications, f"Stash did not adopt moved file; review scene {row['scene_id']}")
            except Exception as error:
                record_activity(self.database_path, "reconciliation", "external move", "failed",
                                severity="error", old_path=source, new_path=destination, detail=str(error))
                notify(self.notifications, f"Move reconciliation failed: {Path(destination).name}")


class CompletedDownloadWorker(threading.Thread):
    """Wait for new incoming videos to settle, then request one targeted Stash scan."""
    def __init__(self, database_path, stash, incoming_folder, enabled, settle_seconds, notifications,
                 fallback_seconds=60, max_attempts=3, incoming_folders=None):
        super().__init__(daemon=True)
        self.database_path, self.stash = database_path, stash
        raw_folders = incoming_folders if incoming_folders is not None else ([incoming_folder] if incoming_folder else [])
        self.incoming_folders = []
        for f in raw_folders:
            if f:
                try:
                    p = Path(f).resolve()
                    if p not in self.incoming_folders:
                        self.incoming_folders.append(p)
                except Exception:
                    pass
        self.incoming_folder = self.incoming_folders[0] if self.incoming_folders else None
        self.enabled = bool(enabled and self.incoming_folders)
        self.settle_seconds = max(60, int(settle_seconds or 300))
        self.notifications = notifications
        self.fallback_seconds = max(15, int(fallback_seconds or 60))
        self.max_attempts = max(1, int(max_attempts))
        self.candidates = {}
        self.relocations = {}
        self.lock = threading.RLock()
        self.stopping = False
        self.wake = threading.Event()
        self.started_at = time.time()
        self.last_fallback = self.started_at
        self.generate_contact_sheets = False
        self.contact_sheet_grid = "4x4"
        self.contact_sheet_banner = True
        self.contact_sheet_adjust_vertical = True
        self.contact_sheet_script = ""
        self.track_temporary_downloads = False
        if self.enabled:
            self._restore_candidates()
            self._recover_recent_files()

    def _is_inside_incoming(self, candidate_path):
        if not self.incoming_folders or not candidate_path:
            return False
        try:
            resolved = Path(candidate_path).resolve()
            for root in self.incoming_folders:
                try:
                    resolved.relative_to(root)
                    return True
                except (OSError, ValueError):
                    continue
        except (OSError, ValueError):
            return False
        return False

    def accepts(self, path):
        if not self.enabled or not path:
            return False
        candidate = Path(path)
        suffix = candidate.suffix.lower()
        if suffix in TEMPORARY_DOWNLOAD_EXTENSIONS:
            if not getattr(self, "track_temporary_downloads", False):
                return False
            return self._is_inside_incoming(candidate)
        if suffix not in (VIDEO_EXTENSIONS | COMPANION_EXTENSIONS):
            return False
        return self._is_inside_incoming(candidate)

    def _current_incoming_paths(self):
        if not self.incoming_folders:
            return set()
        valid = VIDEO_EXTENSIONS | COMPANION_EXTENSIONS
        if getattr(self, "track_temporary_downloads", False):
            valid = valid | TEMPORARY_DOWNLOAD_EXTENSIONS
        found = set()
        for folder in self.incoming_folders:
            if folder.is_dir():
                try:
                    for path in folder.rglob("*"):
                        if path.is_file() and path.suffix.lower() in valid:
                            found.add(str(path.resolve()))
                except Exception as exc:
                    logger.debug("Failed scanning incoming folder %s: %s", folder, exc)
        return found

    def _current_video_paths(self):
        return self._current_incoming_paths()

    def _is_in_inventory(self, path):
        connection = connect(self.database_path)
        try:
            return connection.execute("SELECT 1 FROM files WHERE path=? AND exists_on_disk=1", (path,)).fetchone() is not None
        finally:
            connection.close()

    def _was_imported(self, path):
        connection = connect(self.database_path)
        try:
            row = connection.execute("SELECT status FROM incoming_files WHERE path=?", (path,)).fetchone()
            return bool(row and row["status"] in ("imported", "paired", "dismissed"))
        finally:
            connection.close()

    def _recover_recent_files(self):
        """Recover downloads that completed shortly before the watcher restarted."""
        recent_after = self.started_at - self.settle_seconds
        for path in self._current_video_paths():
            if path in self.candidates or self._is_in_inventory(path) or self._was_imported(path):
                continue
            try:
                if Path(path).stat().st_mtime >= recent_after:
                    self.submit(path)
            except OSError:
                continue

    def _restore_candidates(self):
        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT path,size,modified_ns,stable_since,attempts FROM incoming_files WHERE status IN ('waiting','scanning','downloading')"
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            path = row["path"]
            if not self.accepts(path) or not Path(path).is_file() or self._is_in_inventory(path):
                continue
            stat = Path(path).stat()
            unchanged = row["size"] == stat.st_size and row["modified_ns"] == stat.st_mtime_ns
            suffix = Path(path).suffix.lower()
            is_temporary = suffix in TEMPORARY_DOWNLOAD_EXTENSIONS
            is_companion = (not is_temporary) and (suffix in COMPANION_EXTENSIONS)
            with self.lock:
                self.candidates[path] = {
                    "size": stat.st_size,
                    "modified_ns": stat.st_mtime_ns,
                    "stable_since": float(row["stable_since"] or self.started_at) if unchanged else self.started_at,
                    "attempts": int(row["attempts"] or 0),
                    "is_companion": is_companion,
                    "is_temporary": is_temporary,
                }

    def _save_state(self, path, status, *, stat=None, stable_since=None, job_id=None, detail=None, attempts=None):
        connection = connect(self.database_path)
        try:
            existing = connection.execute("SELECT attempts FROM incoming_files WHERE path=?", (path,)).fetchone()
            attempt_count = int(existing["attempts"] if existing else 0) if attempts is None else int(attempts)
            connection.execute(
                """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,size,modified_ns,stable_since,settle_seconds,status,attempts,scan_job_id,detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET last_checked_at=excluded.last_checked_at,
                     size=COALESCE(excluded.size,incoming_files.size),
                     modified_ns=COALESCE(excluded.modified_ns,incoming_files.modified_ns),
                     stable_since=COALESCE(excluded.stable_since,incoming_files.stable_since),
                     settle_seconds=excluded.settle_seconds,status=excluded.status,
                     attempts=excluded.attempts,scan_job_id=excluded.scan_job_id,detail=excluded.detail""",
                (path, utc_now(), utc_now(), stat.st_size if stat else None,
                 stat.st_mtime_ns if stat else None, stable_since, self.settle_seconds, status, attempt_count,
                 str(job_id) if job_id is not None else None, detail),
            )
            connection.commit()
        finally:
            connection.close()

    def submit(self, path):
        if not self.accepts(path):
            return False
        normalized = str(Path(path).resolve())
        if consume_expected_create(self.database_path, normalized) or consume_expected_create(self.database_path, str(path)):
            return False
        if self._is_in_inventory(normalized) or self._was_imported(normalized):
            return False
        try:
            stat = Path(normalized).stat()
        except OSError:
            return False
        with self.lock:
            current = self.candidates.get(normalized)
        stable_since = time.time()
        if current and current["size"] == stat.st_size and current["modified_ns"] == stat.st_mtime_ns:
            stable_since = current["stable_since"]
        suffix = Path(normalized).suffix.lower()
        is_temporary = suffix in TEMPORARY_DOWNLOAD_EXTENSIONS
        is_companion = (not is_temporary) and (suffix in COMPANION_EXTENSIONS)
        candidate = {
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "stable_since": stable_since,
            "attempts": current["attempts"] if current else 0,
            "is_companion": is_companion,
            "is_temporary": is_temporary,
        }
        with self.lock:
            self.candidates[normalized] = candidate
        if is_temporary:
            status = "downloading"
            detail = "Incoming download in progress"
        elif is_companion:
            status = "waiting"
            detail = "Waiting for companion file to stabilise"
        else:
            status = "waiting"
            detail = f"Waiting for video to remain unchanged for {self.settle_seconds // 60} minute(s)"
        self._save_state(normalized, status, stat=stat, stable_since=stable_since,
                         attempts=candidate["attempts"], detail=detail)
        self.wake.set()
        return True

    def submit_tree(self, path):
        """Discover videos inside a newly-created or newly-moved download directory."""
        root = Path(path)
        if not self.enabled or not root.is_dir():
            return 0
        return sum(1 for child in root.rglob("*") if child.is_file() and self.submit(child))

    def _resolve_relocation(self, path):
        with self.lock:
            seen = set()
            while path in self.relocations and path not in seen:
                seen.add(path)
                path = self.relocations[path]
        return path

    def relocate(self, source, destination):
        """Carry an unimported download's wait/scan state to its new path."""
        source = str(Path(source).resolve())
        destination = str(Path(destination).resolve())
        if not self.enabled or Path(destination).suffix.lower() not in VIDEO_EXTENSIONS:
            return False
        with self.lock:
            candidate = self.candidates.pop(source, None)
        if candidate is None:
            connection = connect(self.database_path)
            try:
                row = connection.execute(
                    "SELECT size,modified_ns,stable_since,attempts,status FROM incoming_files WHERE path=?", (source,)
                ).fetchone()
            finally:
                connection.close()
            if not row or row["status"] not in ("waiting", "scanning", "downloading"):
                return False
            candidate = {"size": row["size"], "modified_ns": row["modified_ns"],
                         "stable_since": float(row["stable_since"] or time.time()),
                         "attempts": int(row["attempts"] or 0)}
        try:
            stat = Path(destination).stat()
        except OSError:
            return False
        if candidate.get("is_temporary"):
            candidate["is_temporary"] = False
            candidate["stable_since"] = time.time()
        if candidate["size"] != stat.st_size or candidate["modified_ns"] != stat.st_mtime_ns:
            candidate.update(size=stat.st_size, modified_ns=stat.st_mtime_ns, stable_since=time.time())
        with self.lock:
            self.relocations[source] = destination
            self.candidates[destination] = candidate
        self._save_state(source, "moved", detail=f"Download completed and renamed to {destination}")
        self._save_state(destination, "waiting", stat=stat, stable_since=candidate["stable_since"],
                         attempts=candidate["attempts"], detail=f"Waiting for video to remain unchanged for {self.settle_seconds // 60} minute(s)")
        self.wake.set()
        return True

    def relocate_tree(self, source, destination):
        """Transfer active children when an entire download directory is moved."""
        source_root = Path(source)
        moved = 0
        with self.lock:
            active_paths = list(self.candidates)
        for active in active_paths:
            try:
                relative = Path(active).relative_to(source_root)
            except ValueError:
                continue
            if self.relocate(active, Path(destination) / relative):
                moved += 1
        return moved

    def _find_scene(self, path):
        result = self.stash.call_GQL(SCENE_BY_PATH_QUERY, {"path": path})
        scenes = ((result or {}).get("findScenes") or {}).get("scenes") or []
        for scene in scenes:
            if any(str(item.get("path")) == path for item in scene.get("files") or []):
                return scene
        return None

    def _scan(self, path, candidate):
        attempts = candidate["attempts"] + 1
        self._save_state(path, "scanning", stable_since=candidate["stable_since"], attempts=attempts,
                         detail="Stash is adding the video and generating its thumbnail and previews")
        try:
            while True:
                job_id = self.stash.metadata_scan(paths=[path], flags=INCOMING_SCAN_FLAGS)
                self._save_state(path, "scanning", stable_since=candidate["stable_since"], attempts=attempts,
                                 job_id=job_id, detail=f"Waiting for Stash scan job {job_id}")
                completed = self.stash.wait_for_job(job_id, timeout=300)
                current_path = self._resolve_relocation(path)
                if current_path == path:
                    break
                path = current_path
                with self.lock:
                    self.candidates.pop(path, None)
            scene = self._find_scene(path) if completed else None
            if not scene:
                raise RuntimeError(f"Stash scan job {job_id} finished without adding the video")
            refresh_scene_inventory(self.database_path, scene)
            with self.lock:
                self.candidates.pop(path, None)
            self._pair_companions_for_video(path, scene)
            self._save_state(path, "imported", attempts=attempts, job_id=job_id,
                             detail=f"Added as Stash scene {scene['id']}")
            record_activity(self.database_path, "incoming", "completed video scan", "imported",
                            scene_id=scene["id"], new_path=path,
                            detail=f"Stash scan job {job_id} added the completed video")
            notify(self.notifications, f"Added to Stash: {Path(path).name}")

            if self.generate_contact_sheets:
                sheet_p = f"{path}.jpg"
                try:
                    self._save_state(sheet_p, "generating_sheet", detail=f"Creating contact sheet for scene {scene['id']}")
                except Exception as exc:
                    logger.debug("save_state before CSM generation failed: %s", exc)
                try:
                    expect_filesystem_create(self.database_path, sheet_p)
                    csm_res = generate_video_contact_sheet(
                        path,
                        grid=self.contact_sheet_grid,
                        include_banner=self.contact_sheet_banner,
                        adjust_vertical=self.contact_sheet_adjust_vertical,
                        custom_script=self.contact_sheet_script
                    )
                    if csm_res.get("status") == "generated":
                        sheet_p = csm_res.get("path") or sheet_p
                        self._save_state(sheet_p, "paired", detail=f"Generated contact sheet for scene {scene['id']}")
                        record_activity(
                            self.database_path,
                            "companion",
                            "contact sheet generated",
                            "recorded",
                            scene_id=scene["id"],
                            new_path=sheet_p,
                            detail=f"Generated {csm_res.get('grid', self.contact_sheet_grid)} contact sheet for visual file browsing"
                        )
                        record_activity(
                            self.database_path,
                            "companion",
                            "auto-paired companion",
                            "recorded",
                            scene_id=scene["id"],
                            old_path=sheet_p,
                            new_path=sheet_p,
                            detail=f"Contact sheet automatically paired with scene {scene['id']}"
                        )
                        notify(self.notifications, f"Contact sheet created & paired: {Path(sheet_p).name} → Scene {scene['id']}")
                    else:
                        self._save_state(sheet_p, "gone", detail="Contact sheet generation skipped")
                except Exception as exc:
                    logger.debug("Contact sheet generation failed for %s: %s", path, exc)
                    try:
                        self._save_state(sheet_p, "gone", detail="Contact sheet generation failed")
                    except Exception as save_exc:
                        logger.debug("save_state after contact sheet failure failed: %s", save_exc)

            return True
        except Exception as error:
            if attempts < self.max_attempts and Path(path).is_file():
                retry_at = time.time()
                with self.lock:
                    self.candidates[path] = {**candidate, "stable_since": retry_at, "attempts": attempts}
                self._save_state(path, "waiting", stable_since=retry_at, attempts=attempts,
                                 detail=f"Scan attempt {attempts} failed; it will retry: {error}")
            else:
                self._save_state(path, "failed", attempts=attempts, detail=str(error))
                record_activity(self.database_path, "incoming", "completed video scan", "failed",
                                severity="error", new_path=path, detail=str(error))
                notify(self.notifications, f"Could not add completed video: {Path(path).name}")
            return False

    def _pair_companions_for_video(self, video_path, scene):
        video_p = Path(video_path)
        actual_path = None
        for f in scene.get("files") or []:
            if f.get("path"):
                actual_path = Path(f["path"])
                break
        if not actual_path:
            actual_path = video_p

        with self.lock:
            candidates_list = list(self.candidates.items())

        for c_path, c_info in candidates_list:
            cand = Path(c_path)
            if cand.suffix.lower() not in COMPANION_EXTENSIONS or not cand.is_file():
                continue
            matched, remainder = match_companion_to_video(cand, video_p)
            if matched:
                if cand.name.lower().startswith(actual_path.name.lower()):
                    target_name = actual_path.name + cand.suffix
                elif remainder:
                    target_name = actual_path.stem + remainder + cand.suffix
                else:
                    target_name = actual_path.stem + cand.suffix
                target_path = actual_path.parent / target_name

                if target_path != cand:
                    try:
                        expect_filesystem_move(self.database_path, str(cand), str(target_path))
                        cand.rename(target_path)
                    except OSError:
                        continue

                with self.lock:
                    self.candidates.pop(c_path, None)

                try:
                    self.stash.metadata_scan(paths=[str(target_path)])
                except Exception as exc:
                    logger.debug("Stash metadata_scan failed for %s: %s", target_path, exc)

                self._save_state(c_path, "paired", detail=f"Paired with scene {scene['id']} at {target_path.name}")
                record_activity(self.database_path, "companion", "auto-paired companion", "recorded",
                                scene_id=scene["id"], old_path=c_path, new_path=str(target_path),
                                detail=f"Companion relocated and paired with newly scanned scene {scene['id']}")
                resolve_filesystem_event(self.database_path, "created", c_path)
                notify(self.notifications, f"Companion paired: {cand.name} → Scene {scene['id']}")

    def _process_companion(self, path, candidate):
        cand_path = Path(path)
        if not cand_path.is_file():
            with self.lock:
                self.candidates.pop(path, None)
            self._save_state(path, "gone", detail="Companion disappeared before processing")
            return

        with self.lock:
            all_pending = list(self.candidates.keys())
        for other in all_pending:
            if Path(other).suffix.lower() in VIDEO_EXTENSIONS:
                matched, _ = match_companion_to_video(cand_path, Path(other))
                if matched:
                    self._save_state(path, "waiting", detail=f"Waiting for video {Path(other).name} to finish downloading")
                    return

        connection = connect(self.database_path)
        try:
            row, remainder = find_scene_for_companion(connection, cand_path.name)
        finally:
            connection.close()

        if row and row["path"]:
            actual_video = Path(row["path"])
            if actual_video.parent.is_dir():
                if cand_path.name.lower().startswith(actual_video.name.lower()):
                    target_name = actual_video.name + cand_path.suffix
                elif remainder:
                    target_name = actual_video.stem + remainder + cand_path.suffix
                else:
                    target_name = actual_video.stem + cand_path.suffix
                target_path = actual_video.parent / target_name

                if target_path != cand_path:
                    try:
                        expect_filesystem_move(self.database_path, str(cand_path), str(target_path))
                        cand_path.rename(target_path)
                    except OSError as err:
                        self._save_state(path, "waiting", detail=f"Could not relocate to {target_path}: {err}")
                        return

                with self.lock:
                    self.candidates.pop(path, None)

                try:
                    self.stash.metadata_scan(paths=[str(target_path)])
                except Exception as exc:
                    logger.debug("Stash metadata_scan failed for late companion %s: %s", target_path, exc)

                self._save_state(path, "paired", detail=f"Paired with scene {row['scene_id']} at {target_path.name}")
                record_activity(self.database_path, "companion", "auto-paired late companion", "recorded",
                                scene_id=row["scene_id"], old_path=path, new_path=str(target_path),
                                detail=f"Companion automatically relocated and paired with scene {row['scene_id']}")
                resolve_filesystem_event(self.database_path, "created", path)
                notify(self.notifications, f"Companion paired: {cand_path.name} → Scene {row['scene_id']}")
                return

        self._save_state(path, "waiting", detail="Waiting for matching video to arrive")

    def evaluate_once(self, now=None):
        now = time.time() if now is None else float(now)
        with self.lock:
            pending = list(self.candidates.items())
        for path, candidate in pending:
            try:
                stat = Path(path).stat()
            except OSError:
                with self.lock:
                    self.candidates.pop(path, None)
                self._save_state(path, "gone", detail="File disappeared before it finished")
                continue
            if stat.st_size != candidate["size"] or stat.st_mtime_ns != candidate["modified_ns"]:
                candidate.update(size=stat.st_size, modified_ns=stat.st_mtime_ns, stable_since=now)
                if candidate.get("is_temporary"):
                    self._save_state(path, "downloading", stat=stat, stable_since=now,
                                     detail="Incoming download in progress")
                else:
                    self._save_state(path, "waiting", stat=stat, stable_since=now, attempts=candidate["attempts"],
                                     detail=f"File is still changing; the {self.settle_seconds // 60}-minute wait restarted")
                continue
            if candidate.get("is_temporary"):
                continue
            is_comp = candidate.get("is_companion") or Path(path).suffix.lower() in COMPANION_EXTENSIONS
            settle_needed = min(3, self.settle_seconds) if is_comp else self.settle_seconds
            if now - candidate["stable_since"] >= settle_needed:
                if is_comp:
                    self._process_companion(path, candidate)
                else:
                    with self.lock:
                        self.candidates.pop(path, None)
                    self._scan(path, candidate)

    def _fallback_check(self):
        for path in self._current_video_paths():
            with self.lock:
                pending = path in self.candidates
            if pending or self._is_in_inventory(path) or self._was_imported(path):
                continue
            try:
                created_during_this_run = Path(path).stat().st_mtime >= self.started_at
            except OSError:
                continue
            if created_during_this_run:
                self.submit(path)

    def stop(self):
        self.stopping = True
        self.wake.set()

    def run(self):
        while not self.stopping:
            self.evaluate_once()
            now = time.time()
            if now - self.last_fallback >= self.fallback_seconds:
                self._fallback_check()
                self.last_fallback = now
            self.wake.wait(timeout=min(5, self.fallback_seconds))
            self.wake.clear()


class LibraryEventHandler(FileSystemEventHandler):
    def __init__(self, database_path, worker, notifications, incoming_worker=None):
        self.database_path = database_path
        self.worker = worker
        self.notifications = notifications
        self.incoming_worker = incoming_worker
        # Track when each path last had a 'created' event so the delayed
        # "still missing?" check can tell the difference between a genuine
        # deletion and a rapid delete-then-recreate (e.g. atomic download swap).
        self._recent_creates: dict[str, float] = {}
        self._recent_creates_lock = threading.Lock()

    def _relevant(self, path, is_directory):
        return is_directory or Path(path).suffix.lower() in WATCHED_EXTENSIONS

    def on_created(self, event):
        if event.is_directory and self.incoming_worker:
            self.incoming_worker.submit_tree(event.src_path)
        if event.is_directory:
            return
        # Resolve any transient delete event if the file is recreated/present
        resolve_filesystem_event(self.database_path, "deleted", event.src_path)
        if consume_expected_create(self.database_path, event.src_path):
            return
        incoming_candidate = bool(not event.is_directory and self.incoming_worker and self.incoming_worker.submit(event.src_path))
        if self._relevant(event.src_path, event.is_directory) and not incoming_candidate:
            if self.incoming_worker and self.incoming_worker._is_inside_incoming(event.src_path):
                return
            if Path(event.src_path).suffix.lower() in COMPANION_EXTENSIONS:
                cand = Path(event.src_path)
                parent = cand.parent
                has_matching_video = False
                for v_ext in VIDEO_EXTENSIONS:
                    if (parent / (cand.stem + v_ext)).is_file() or (parent / cand.stem).is_file():
                        has_matching_video = True
                        break
                if has_matching_video:
                    return
            with self._recent_creates_lock:
                self._recent_creates[event.src_path] = time.monotonic()
                # Prune entries older than 30 s to prevent unbounded growth
                cutoff = time.monotonic() - 30.0
                self._recent_creates = {k: v for k, v in self._recent_creates.items() if v > cutoff}
            record_filesystem_event(self.database_path, "created", event.src_path, is_directory=event.is_directory, initial_status="pending")

    def on_deleted(self, event):
        if event.is_directory:
            return
        if self._relevant(event.src_path, event.is_directory):
            if self.incoming_worker:
                with self.incoming_worker.lock:
                    self.incoming_worker.candidates.pop(event.src_path, None)
                self.incoming_worker._save_state(event.src_path, "gone", detail="File removed from disk")
            if Path(event.src_path).suffix.lower() not in TEMPORARY_DOWNLOAD_EXTENSIONS:
                record_filesystem_event(self.database_path, "deleted", event.src_path, is_directory=event.is_directory, initial_status="pending")
                if not event.is_directory and Path(event.src_path).suffix.lower() in WATCHED_EXTENSIONS:
                    threading.Timer(3, self._notify_if_still_missing, args=(event.src_path,)).start()

    def _notify_if_still_missing(self, path):
        # Suppress the warning if the file was recreated within 5 s of this check
        # (e.g. an atomic download swap or in-place replacement).
        with self._recent_creates_lock:
            created_at = self._recent_creates.get(path, 0)
        if time.monotonic() - created_at < 5.0:
            return
        if not Path(path).exists():
            record_activity(self.database_path, "filesystem", "external deletion", "review", severity="warning",
                            old_path=path, detail="File remained absent after the notification delay")
            notify(self.notifications, f"File deleted or moved without a paired event: {Path(path).name}")

    def on_modified(self, event):
        # Modification events are useful only for the incoming stability timer. They do not
        # identify a move/deletion and must not create an unexplained review warning.
        if not event.is_directory and self._relevant(event.src_path, False):
            if self.incoming_worker:
                self.incoming_worker.submit(event.src_path)

    def on_moved(self, event):
        if self._relevant(event.src_path, event.is_directory) or self._relevant(event.dest_path, event.is_directory):
            if event.is_directory and self.incoming_worker:
                self.incoming_worker.relocate_tree(event.src_path, event.dest_path)
                self.incoming_worker.submit_tree(event.dest_path)
            if event.is_directory:
                return
            if not event.is_directory and consume_expected_move(self.database_path, event.src_path, event.dest_path):
                return
            if not event.is_directory and self.incoming_worker and self.incoming_worker.relocate(event.src_path, event.dest_path):
                return
            # Stash and some macOS deletion workflows first rename a video to
            # `<filename>.delete`. Present that honestly as a deletion, not as an
            # unexplained companion-file move.
            if (not event.is_directory and Path(event.src_path).suffix.lower() in VIDEO_EXTENSIONS
                    and Path(event.dest_path).suffix.lower() == ".delete"):
                record_filesystem_event(self.database_path, "deleted", event.src_path, is_directory=event.is_directory, initial_status="pending")
                record_activity(self.database_path, "filesystem", "external deletion", "review",
                                severity="warning", old_path=event.src_path, new_path=event.dest_path,
                                detail="Video was renamed to a temporary .delete path during deletion; confirm the Stash scene no longer points to it")
                return
            incoming_candidate = bool(self.incoming_worker and self.incoming_worker.submit(event.dest_path))
            if incoming_candidate and Path(event.src_path).suffix.lower() not in VIDEO_EXTENSIONS:
                return
            record_filesystem_event(self.database_path, "moved", event.src_path, event.dest_path, event.is_directory, initial_status="pending")
            if not event.is_directory and Path(event.dest_path).suffix.lower() in VIDEO_EXTENSIONS:
                self.worker.submit(event.src_path, event.dest_path)
            elif not event.is_directory:
                record_activity(self.database_path, "companion", "external companion move", "recorded",
                                old_path=event.src_path, new_path=event.dest_path,
                                detail="Companion move recorded; Stash does not maintain a separate path for this file")


def update_status(database_path, token, pid, state, roots, unavailable):
    connection = connect(database_path)
    try:
        connection.execute(
            """UPDATE filesystem_monitor_status SET token=?,pid=?,state=?,started_at=COALESCE(started_at,?),
                   heartbeat_at=?,roots_json=?,unavailable_roots_json=? WHERE id=1""",
            (token, pid, state, utc_now(), utc_now(), json.dumps(roots), json.dumps(unavailable)),
        )
        connection.commit()
    finally:
        connection.close()


def _reset_stale_failed_incoming(database_path: Path) -> None:
    """On monitor startup, reset 'failed' incoming files that still exist on disk
    back to 'waiting' so they get another scan attempt in the new session."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT path FROM incoming_files WHERE status='failed'"
        ).fetchall()
        paths_to_reset = [row["path"] for row in rows if Path(row["path"]).is_file()]
        if paths_to_reset:
            now = time.time()
            for p in paths_to_reset:
                connection.execute(
                    """UPDATE incoming_files SET status='waiting', attempts=0,
                       stable_since=?, detail='Auto-retry on monitor startup'
                       WHERE path=?""",
                    (now, p),
                )
            connection.commit()
    except Exception:
        pass  # Startup reset is best-effort; don't block monitor launch
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--control", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--roots-json", required=True)
    parser.add_argument("--runtime", required=True)
    args = parser.parse_args()
    database_path = Path(args.database)
    control_path = Path(args.control)
    roots = json.loads(args.roots_json)
    available = [root for root in roots if Path(root).is_dir()]
    unavailable = [root for root in roots if root not in available]
    runtime_path = Path(args.runtime)
    runtime = {}
    if runtime_path.is_file():
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("Failed to read runtime config from %s: %s", runtime_path, exc)
    stash = StashInterface(runtime["server_connection"])
    worker = MoveWorker(database_path, stash, runtime.get("automatic_move_reconciliation") is True,
                        runtime.get("mac_notifications") is True)
    incoming_worker = CompletedDownloadWorker(
        database_path, stash, runtime.get("incoming_folder"), runtime.get("incoming_imports") is True,
        runtime.get("incoming_settle_seconds", 300), runtime.get("mac_notifications") is True,
        runtime.get("incoming_fallback_seconds", 60),
        incoming_folders=runtime.get("incoming_folders"),
    )
    incoming_worker.generate_contact_sheets = runtime.get("generate_contact_sheets") is True
    incoming_worker.track_temporary_downloads = True
    incoming_worker.contact_sheet_grid = runtime.get("contact_sheet_grid") or "4x4"
    incoming_worker.contact_sheet_banner = runtime.get("contact_sheet_banner") is not False
    incoming_worker.contact_sheet_adjust_vertical = runtime.get("contact_sheet_adjust_vertical") is not False
    incoming_worker.contact_sheet_script = runtime.get("contact_sheet_script") or ""
    observer = Observer()
    handler = LibraryEventHandler(database_path, worker, runtime.get("mac_notifications") is True, incoming_worker)
    for root in available:
        observer.schedule(handler, root, recursive=True)
    update_status(database_path, args.token, os.getpid(), "running", available, unavailable)
    for root in unavailable:
        record_activity(database_path, "monitor", "library root unavailable", "warning", severity="warning",
                        old_path=root, detail="Root was unavailable when monitoring started")
    if unavailable:
        notify(runtime.get("mac_notifications") is True, f"{len(unavailable)} library root(s) unavailable")
    # Reset any 'failed' incoming files from a previous session so the worker
    # will retry them automatically (up to max_attempts) without user action.
    _reset_stale_failed_incoming(database_path)
    worker.start()
    incoming_worker.start()
    observer.start()
    try:
        while True:
            if control_path.exists():
                try:
                    request = json.loads(control_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    request = {}
                if request.get("token") == args.token:
                    if request.get("action") == "stop":
                        break
                    elif request.get("action") == "reload":
                        try:
                            new_cfg = request.get("config") or {}
                            worker.enabled = bool(new_cfg.get("automatic_move_reconciliation", worker.enabled))
                            worker.notifications = bool(new_cfg.get("mac_notifications", worker.notifications))
                            _new_folders = new_cfg.get("incoming_folders")
                            _new_folder_str = new_cfg.get("incoming_folder")
                            if _new_folders is not None:
                                incoming_worker.incoming_folders = [Path(f).resolve() for f in _new_folders if f]
                                incoming_worker.incoming_folder = incoming_worker.incoming_folders[0] if incoming_worker.incoming_folders else None
                            elif _new_folder_str is not None:
                                incoming_worker.incoming_folder = Path(_new_folder_str).resolve() if _new_folder_str else None
                                incoming_worker.incoming_folders = [incoming_worker.incoming_folder] if incoming_worker.incoming_folder else []
                            _new_enabled = new_cfg.get("incoming_imports")
                            if _new_enabled is not None:
                                incoming_worker.enabled = bool(_new_enabled and incoming_worker.incoming_folders)
                            incoming_worker.settle_seconds = new_cfg.get("incoming_settle_seconds", incoming_worker.settle_seconds)
                            incoming_worker.generate_contact_sheets = new_cfg.get("generate_contact_sheets", incoming_worker.generate_contact_sheets)
                            incoming_worker.contact_sheet_grid = new_cfg.get("contact_sheet_grid", incoming_worker.contact_sheet_grid)
                            incoming_worker.contact_sheet_banner = new_cfg.get("contact_sheet_banner", incoming_worker.contact_sheet_banner)
                            incoming_worker.contact_sheet_adjust_vertical = new_cfg.get("contact_sheet_adjust_vertical", incoming_worker.contact_sheet_adjust_vertical)
                            incoming_worker.contact_sheet_script = new_cfg.get("contact_sheet_script", incoming_worker.contact_sheet_script)
                        except Exception as reload_err:
                            logger.debug("Failed to apply reload configuration: %s", reload_err)
                        finally:
                            try:
                                control_path.unlink(missing_ok=True)
                            except Exception:
                                pass
            update_status(database_path, args.token, os.getpid(), "running", available, unavailable)
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    except Exception as fatal_error:
        try:
            record_activity(
                database_path, "monitor", "daemon stopped unexpectedly", "failed",
                severity="error", detail=f"Filesystem watcher crashed: {fatal_error}"
            )
        except Exception:
            pass
        raise
    finally:
        observer.stop()
        observer.join(timeout=10)
        worker.stop()
        worker.join(timeout=10)
        incoming_worker.stop()
        incoming_worker.join(timeout=10)
        update_status(database_path, args.token, os.getpid(), "stopped", available, unavailable)


if __name__ == "__main__":
    main()
