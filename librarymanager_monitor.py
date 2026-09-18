#!/usr/bin/env python3
"""Background filesystem watcher for Stash Library Manager."""

import argparse
import base64
import errno
import threading
import time
from pathlib import Path
NETWORK_DISCONNECT_ERRNOS = {
    getattr(errno, "ENOTCONN", 57),       # Socket is not connected
    getattr(errno, "ETIMEDOUT", 60),      # Operation timed out
    getattr(errno, "EHOSTDOWN", 64),      # Host is down
    getattr(errno, "EHOSTUNREACH", 65),   # No route to host
    getattr(errno, "ECONNRESET", 54),     # Connection reset by peer
    getattr(errno, "ECONNABORTED", 53),   # Software caused connection abort
    getattr(errno, "ENETDOWN", 50),       # Network is down
    getattr(errno, "ENETUNREACH", 51),    # Network is unreachable
    getattr(errno, "EIO", 5),             # Input/output error
    getattr(errno, "ESTALE", 70),         # Stale NFS file handle
}


def is_network_disconnect_error(err: BaseException, path=None) -> bool:
    if isinstance(err, OSError):
        if err.errno in NETWORK_DISCONNECT_ERRNOS:
            return True
        err_msg = str(err).lower()
        if any(msg in err_msg for msg in ("socket is not connected", "timed out", "host is down", "no route to host", "network is down", "stale file handle", "input/output error")):
            return True
    return False


def is_path_available(path, timeout=2.0) -> bool:
    if not path:
        return False
    result = [False]

    def _check():
        try:
            p = Path(path)
            if not p.is_dir():
                return
            try:
                with os.scandir(p) as it:
                    pass
            except OSError as e:
                if is_network_disconnect_error(e, path=p):
                    return
            result[0] = True
        except (OSError, Exception):
            result[0] = False

    t = threading.Thread(target=_check, name=f"check_{Path(path).name}", daemon=True)
    t.start()
    t.join(timeout=float(timeout))
    if t.is_alive():
        logger.warning("Availability probe for %s timed out after %.1fs (unresponsive mount)", path, timeout)
        return False
    return result[0]


class BoundedFSOperation:
    """Executes filesystem operations with bounded timeout and per-root concurrency limits.
    Prevents repeated events from accumulating unlimited blocked threads on unresponsive mounts."""
    def __init__(self, max_workers_per_root=2):
        self.max_workers_per_root = int(max_workers_per_root)
        self._lock = threading.Lock()
        self._active_workers: dict[str, list[dict]] = {}
        self._exhausted_logged: set[str] = set()

    def run(self, root_key, func, args=(), timeout=1.5):
        root_key = str(root_key or "")
        timeout = float(timeout)
        now = time.monotonic()
        res = [None]
        err = [None]
        entry = None

        with self._lock:
            # 1. Prune dead threads & detect hung threads
            active = []
            for w in self._active_workers.get(root_key, []):
                t = w["thread"]
                if t.is_alive():
                    if not w["timed_out"] and (now - w["started_at"] > w["timeout"]):
                        w["timed_out"] = True
                    active.append(w)
            self._active_workers[root_key] = active

            # 2. Check for confirmed outage (active hung thread) vs busy
            has_hung_thread = any(w["timed_out"] for w in active)
            if len(active) >= self.max_workers_per_root:
                if has_hung_thread:
                    if root_key not in self._exhausted_logged:
                        self._exhausted_logged.add(root_key)
                        logger.warning(
                            "Root %s in confirmed outage (%d active hung worker(s)); rejecting operations",
                            root_key, len([w for w in active if w["timed_out"]])
                        )
                    return None, "outage"
                else:
                    # Workers are busy processing concurrent requests normally; do NOT treat as outage
                    return None, "busy"

            self._exhausted_logged.discard(root_key)

            def _target():
                try:
                    res[0] = func(*args)
                except Exception as e:
                    err[0] = e

            t = threading.Thread(
                target=_target,
                name=f"fs_op_{Path(root_key).name if root_key else 'default'}",
                daemon=True,
            )
            entry = {
                "thread": t,
                "started_at": now,
                "timeout": timeout,
                "timed_out": False,
            }
            self._active_workers.setdefault(root_key, []).append(entry)
            # Concurrency-safe: start the thread while holding the lock so it is never
            # pruned prematurely before is_alive() becomes True
            t.start()

        t.join(timeout=timeout)
        if t.is_alive():
            with self._lock:
                entry["timed_out"] = True
            logger.warning("Filesystem operation on root %s timed out after %.1fs", root_key, timeout)
            return None, "timeout"

        if err[0]:
            raise err[0]
        return res[0], "ok"


default_fs_limiter = BoundedFSOperation(max_workers_per_root=2)


def probe_roots_startup(roots, timeout=2.0, max_workers=4):
    """Probes root availability at startup with a bounded worker pool and global deadline,
    avoiding nested thread-per-root explosion."""
    if not roots:
        return [], []

    results = {}
    pending = list(roots)
    deadline = time.monotonic() + float(timeout)

    def _worker(r):
        try:
            p = Path(r)
            if not p.is_dir():
                results[r] = False
                return
            try:
                with os.scandir(p) as it:
                    pass
                results[r] = True
            except OSError as e:
                results[r] = not is_network_disconnect_error(e, path=p)
        except (OSError, Exception):
            results[r] = False

    active_threads = []
    while (pending or active_threads) and time.monotonic() < deadline:
        while pending and len(active_threads) < max_workers:
            r = pending.pop(0)
            t = threading.Thread(target=_worker, args=(r,), name=f"startup_probe_{Path(r).name}", daemon=True)
            t.start()
            active_threads.append(t)

        time.sleep(0.02)
        active_threads = [t for t in active_threads if t.is_alive()]

    available = [r for r in roots if results.get(r) is True]
    unavailable = [r for r in roots if r not in available]
    return available, unavailable




class RootAvailabilityTracker:
    """Tracks availability of monitored roots asynchronously without blocking the caller."""
    def __init__(self, roots, probe_timeout=5.0, max_hung_probes=2):
        self.roots = list(roots or [])
        self.probe_timeout = float(probe_timeout)
        self.max_hung_probes = int(max_hung_probes)
        self.lock = threading.Lock()
        self._states = {}
        for r in self.roots:
            self._states[r] = {
                "status": "available",
                "in_flight": False,
                "started_at": 0.0,
                "last_probed_at": 0.0,
                "probe_id": 0,
                "active_threads": [],
                "exhausted_logged": False,
            }
        self._recovered_batch = []
        self._lost_batch = []

    def update_roots(self, roots):
        with self.lock:
            self.roots = list(roots or [])
            for r in self.roots:
                if r not in self._states:
                    self._states[r] = {
                        "status": "available",
                        "in_flight": False,
                        "started_at": 0.0,
                        "last_probed_at": 0.0,
                        "probe_id": 0,
                        "active_threads": [],
                        "exhausted_logged": False,
                    }

    def get_unavailable_roots(self) -> list[str]:
        with self.lock:
            return [r for r, s in self._states.items() if s.get("status") != "available"]

    def mark_root_unavailable(self, root):
        with self.lock:
            state = self._states.get(root)
            if state and state["status"] == "available":
                state["status"] = "unavailable"
                self._lost_batch.append(root)

    def poll(self):
        """Non-blocking call by the main monitor loop.
        Returns (available_roots, unavailable_roots, recovered_roots, lost_roots).
        NEVER performs filesystem operations on the calling thread.
        """
        mono_now = time.monotonic()
        recovered = []
        lost = []
        available = []
        unavailable = []

        with self.lock:
            for root in self.roots:
                state = self._states.get(root)
                if not state:
                    continue

                state["active_threads"] = [t for t in state["active_threads"] if t.is_alive()]
                alive_count = len(state["active_threads"])

                if state["in_flight"]:
                    if mono_now - state["started_at"] > self.probe_timeout:
                        state["in_flight"] = False
                        state["last_probed_at"] = mono_now
                        if state["status"] == "available":
                            state["status"] = "unavailable"
                            lost.append(root)
                else:
                    if alive_count >= self.max_hung_probes:
                        if state["status"] == "available":
                            state["status"] = "unavailable"
                            lost.append(root)
                        if not state.get("exhausted_logged"):
                            logger.warning(
                                "Root %s exhausted probe worker allowance (%d active blocked thread(s)); "
                                "automatic recovery paused until thread capacity becomes available",
                                root, alive_count
                            )
                            state["exhausted_logged"] = True
                    else:
                        state["exhausted_logged"] = False
                        interval = 15.0 if alive_count > 0 else 5.0
                        if mono_now - state["last_probed_at"] >= interval or state["last_probed_at"] == 0.0:
                            self._launch_probe_unlocked(root, mono_now)

                if state["status"] == "available":
                    available.append(root)
                else:
                    unavailable.append(root)

            recovered_batch = self._recovered_batch
            lost_batch = self._lost_batch
            self._recovered_batch = []
            self._lost_batch = []

        all_recovered = list(dict.fromkeys(recovered + recovered_batch))
        all_lost = list(dict.fromkeys(lost + lost_batch))
        return available, unavailable, all_recovered, all_lost

    def _launch_probe_unlocked(self, root, mono_now):
        state = self._states[root]
        state["in_flight"] = True
        state["started_at"] = mono_now
        state["probe_id"] += 1
        current_probe_id = state["probe_id"]

        def probe_worker(p_id):
            success = False
            try:
                p = Path(root)
                if p.is_dir():
                    try:
                        with os.scandir(p) as it:
                            pass
                        success = True
                    except OSError as e:
                        if not is_network_disconnect_error(e):
                            success = True
            except (OSError, Exception):
                success = False

            with self.lock:
                if p_id != state["probe_id"]:
                    return
                state["in_flight"] = False
                state["last_probed_at"] = time.monotonic()
                prev_status = state["status"]
                new_status = "available" if success else "unavailable"
                state["status"] = new_status
                if prev_status != new_status:
                    if new_status == "available":
                        self._recovered_batch.append(root)
                    else:
                        self._lost_batch.append(root)

        t = threading.Thread(target=probe_worker, args=(current_probe_id,), name=f"probe_{Path(root).name}", daemon=True)
        state["active_threads"].append(t)
        t.start()

import hashlib
import logging
import json
import os
import posixpath
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
    is_file_on_unavailable_root,
                                 opensubtitles_hash, record_activity, record_filesystem_event,
                                 resolve_filesystem_event, refresh_scene_inventory, utc_now)


logger = logging.getLogger("librarymanager.monitor")


def monitor_code_signature(path=None):
    """Return a content signature so a detached daemon can detect plugin updates."""
    try:
        return hashlib.sha256(Path(path or __file__).read_bytes()).hexdigest()
    except OSError:
        return None


def persist_monitor_runtime(runtime_path, runtime, worker, incoming_worker):
    """Preserve live settings across an in-place daemon code reload."""
    snapshot = dict(runtime)
    snapshot.update({
        "automatic_move_reconciliation": bool(worker.enabled),
        "transcoder_replacement_compatibility": bool(worker.transcoder_compatibility),
        "mac_notifications": bool(worker.notifications),
        "incoming_imports": bool(incoming_worker.enabled),
        "incoming_folder": str(incoming_worker.incoming_folder or ""),
        "incoming_folders": [str(path) for path in incoming_worker.incoming_folders],
        "incoming_settle_seconds": incoming_worker.settle_seconds,
        "incoming_fallback_seconds": incoming_worker.fallback_seconds,
        "generate_contact_sheets": bool(incoming_worker.generate_contact_sheets),
        "contact_sheet_grid": incoming_worker.contact_sheet_grid,
        "contact_sheet_banner": bool(incoming_worker.contact_sheet_banner),
        "contact_sheet_adjust_vertical": bool(incoming_worker.contact_sheet_adjust_vertical),
        "contact_sheet_script": incoming_worker.contact_sheet_script,
        "allow_custom_contact_sheet_script": bool(incoming_worker.allow_custom_contact_sheet_script),
    })
    temporary = runtime_path.with_name(f"{runtime_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(snapshot), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, runtime_path)


def reload_monitor_if_code_changed(loaded_signature, runtime_path, runtime, worker,
                                   incoming_worker, database_path, script_path=None):
    """Replace the current process when an installed update changes monitor code."""
    current_signature = monitor_code_signature(script_path)
    if not loaded_signature or not current_signature or current_signature == loaded_signature:
        return False
    persist_monitor_runtime(runtime_path, runtime, worker, incoming_worker)
    record_activity(database_path, "monitor", "code update", "restarting",
                    detail="Watchtower code changed on disk; reloading the detached monitor")
    logger.info("Watchtower code changed on disk; replacing monitor process in place")
    os.execv(sys.executable, [sys.executable, *sys.argv])
    return True


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


def is_temporary_download(path) -> bool:
    if not path:
        return False
    p = Path(path)
    name = p.name.lower()
    suffix = p.suffix.lower()
    if suffix in TEMPORARY_DOWNLOAD_EXTENSIONS:
        return True
    if name.startswith(".com.google.chrome.") or name.startswith("com.google.chrome."):
        return True
    if name.startswith("unconfirmed ") and (name.endswith(".crdownload") or ".crdownload" in name):
        return True
    if name.endswith(".crdownload"):
        return True
    return False


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
GENERIC_ARTWORK_STEMS = {"cover", "poster", "fanart", "folder", "thumb"}


def is_image_companion(path) -> bool:
    try:
        return Path(path).suffix.lower() in IMAGE_EXTENSIONS
    except Exception:
        return False


def is_generic_artwork(path) -> bool:
    try:
        p = Path(path)
        return p.suffix.lower() in IMAGE_EXTENSIONS and p.stem.lower() in GENERIC_ARTWORK_STEMS
    except Exception:
        return False


def find_eligible_videos_in_folder(folder: Path, database_path: Path = None, candidates: dict = None) -> set:
    eligible = set()
    try:
        f_resolved = folder.resolve()
    except OSError:
        return eligible

    # 1. On disk in folder
    try:
        if folder.is_dir():
            for item in folder.iterdir():
                if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
                    if not is_temporary_download(item):
                        try:
                            eligible.add(item.resolve())
                        except OSError:
                            pass
    except OSError:
        pass

    # 2. In candidates (in-memory)
    if candidates:
        for c_p, c_cand in candidates.items():
            if not c_cand.get("is_companion") and not c_cand.get("is_temporary"):
                p = Path(c_p)
                if p.suffix.lower() in VIDEO_EXTENSIONS:
                    try:
                        if p.parent.resolve() == f_resolved:
                            eligible.add(p.resolve())
                    except OSError:
                        pass

    # 3. In database files table (existing scenes)
    if database_path:
        try:
            con = connect(database_path)
            try:
                rows = con.execute("SELECT path FROM files WHERE exists_on_disk=1").fetchall()
                for r in rows:
                    p = Path(r["path"])
                    if p.suffix.lower() in VIDEO_EXTENSIONS:
                        try:
                            if p.parent.resolve() == f_resolved:
                                eligible.add(p.resolve())
                        except OSError:
                            pass
            finally:
                con.close()
        except Exception:
            pass

    return eligible


def _sidecar_match_key(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(name or ""))
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", "", no_accents.casefold())


def match_companion_to_video(candidate: Path, video: Path):
    """Match colocated companions without fuzzy cross-name guesses."""
    c_suffix = candidate.suffix.lower()
    if c_suffix not in COMPANION_EXTENSIONS:
        return False, None

    c_name = candidate.name.lower()
    v_name = video.name.lower()
    c_stem = candidate.stem.lower()
    v_stem = video.stem.lower()

    # Explicit compound form: video.mp4.jpg / video.mp4.srt
    if c_name == v_name + c_suffix:
        return True, ""

    # Standard same-stem form: video.jpg / video.srt for video.mp4
    if c_stem == v_stem:
        return True, ""

    # Accent/bracket tolerant exact equivalence only.
    k_cand = _sidecar_match_key(c_stem)
    k_vid = _sidecar_match_key(v_stem)
    if k_cand and k_cand == k_vid:
        return True, ""

    # Short language/part suffixes such as Scene.en.srt or Scene-ptBR.srt.
    if candidate.stem.casefold().startswith(video.stem.casefold()) and len(candidate.stem) > len(video.stem):
        remainder = candidate.stem[len(video.stem):]
        if remainder.startswith((".", "-", "_")) and len(remainder) <= 8:
            return True, remainder

    return False, None


def _compound_video_name(companion_name: str):
    base = Path(companion_name).stem
    if Path(base).suffix.lower() in VIDEO_EXTENSIONS:
        return base
    return None


def find_scene_for_companion(con, companion_path_or_name):
    """Resolve companions conservatively: local exact matches or unique compound names only."""
    c_raw = str(companion_path_or_name)
    c_path = Path(c_raw.replace("\\", "/"))
    c_name = c_path.name
    c_stem = c_path.stem
    c_dir = str(c_path.parent) if len(c_path.parts) > 1 and str(c_path.parent) not in (".", "") else None

    if c_path.suffix.lower() not in COMPANION_EXTENSIONS:
        return None, ""

    def _normalize_dir_key(p: str) -> str:
        return os.path.normcase(posixpath.normpath(str(p).replace("\\", "/")))

    def _dir_of(p: str) -> str:
        return posixpath.dirname(str(p).replace("\\", "/"))

    # Strong cross-directory form only: Movie.m4v.jpg -> Movie.m4v.
    compound_name = _compound_video_name(c_name)
    if compound_name:
        rows = con.execute(
            "SELECT file_id, scene_id, path, basename FROM files WHERE exists_on_disk=1 AND basename = ?",
            (compound_name,),
        ).fetchall()
        if len(rows) == 1:
            return rows[0], ""
        if len(rows) > 1 and c_dir:
            target_key = _normalize_dir_key(c_dir)
            local = [r for r in rows if _normalize_dir_key(_dir_of(r["path"])) == target_key]
            if len(local) == 1:
                return local[0], ""
        return None, ""

    # Weak stem matching is allowed only inside the exact same directory.
    if not c_dir:
        return None, ""

    c_dir_norm = str(c_dir).rstrip("/\\")
    p1 = c_dir_norm.replace("\\", "/") + "/"
    u1 = p1[:-1] + "0"
    p2 = c_dir_norm.replace("/", "\\") + "\\"
    u2 = p2[:-1] + "]"

    if p1 == p2:
        rows = con.execute(
            "SELECT file_id, scene_id, path, basename FROM files WHERE exists_on_disk=1 AND path >= ? AND path < ?",
            (p1, u1),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT file_id, scene_id, path, basename FROM files WHERE exists_on_disk=1 AND ((path >= ? AND path < ?) OR (path >= ? AND path < ?))",
            (p1, u1, p2, u2),
        ).fetchall()

    target_dir_key = _normalize_dir_key(c_dir)
    local_rows = [
        r for r in rows
        if _normalize_dir_key(_dir_of(r["path"])) == target_dir_key
    ]

    # Generic artwork matching: only associate when exactly one video is in that folder
    if is_generic_artwork(c_path):
        eligible = find_eligible_videos_in_folder(c_path.parent)
        all_vid_paths = {_normalize_dir_key(p) for p in eligible}
        for r in local_rows:
            all_vid_paths.add(_normalize_dir_key(r["path"]))
        if len(all_vid_paths) == 1 and len(local_rows) == 1:
            return local_rows[0], f".{c_stem.lower()}" if c_stem.lower() != "cover" else ""
        return None, ""
    matches = []
    for row in local_rows:
        video = Path(row["basename"])
        if video.stem.casefold() == c_stem.casefold():
            matches.append((row, ""))
            continue
        c_key = _sidecar_match_key(c_stem)
        v_key = _sidecar_match_key(video.stem)
        if c_key and c_key == v_key:
            matches.append((row, ""))
            continue
        if c_stem.casefold().startswith(video.stem.casefold()) and len(c_stem) > len(video.stem):
            remainder = c_stem[len(video.stem):]
            if remainder.startswith((".", "-", "_")) and len(remainder) <= 8:
                matches.append((row, remainder))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning("[REVIEW / SKIP] Companion %s has %d same-directory video matches", c_name, len(matches))
    return None, ""


def relocate_companions_transactionally(database_path: Path, source_path: str, destination_path: str):
    """Move every matching companion or restore all completed moves on failure."""
    source = Path(source_path)
    destination = Path(destination_path)
    if not source.parent.is_dir() or not destination.parent.is_dir():
        return []
    planned = []
    for candidate in sorted(source.parent.iterdir(), key=lambda item: item.name.casefold()):
        if not candidate.is_file() or candidate == source or candidate.suffix.lower() not in COMPANION_EXTENSIONS:
            continue
        matched, remainder = match_companion_to_video(candidate, source)
        if not matched:
            continue
        if candidate.name.lower().startswith(source.name.lower()):
            target_name = destination.name + candidate.suffix
        elif remainder:
            target_name = destination.stem + remainder + candidate.suffix
        else:
            target_name = destination.stem + candidate.suffix
        target = destination.parent / target_name
        if target.exists() and target != candidate:
            raise FileExistsError(f"Companion destination already exists: {target}")
        if target != candidate:
            planned.append((candidate, target))

    moved = []
    try:
        for source_companion, target_companion in planned:
            expect_filesystem_move(database_path, str(source_companion), str(target_companion))
            source_companion.rename(target_companion)
            moved.append((source_companion, target_companion))
    except Exception as move_error:
        rollback_errors = []
        for source_companion, target_companion in reversed(moved):
            try:
                if target_companion.exists() and not source_companion.exists():
                    expect_filesystem_move(database_path, str(target_companion), str(source_companion))
                    target_companion.rename(source_companion)
            except Exception as rollback_error:
                rollback_errors.append(f"{target_companion.name}: {rollback_error}")
        detail = f"Companion relocation failed and completed moves were restored: {move_error}"
        if rollback_errors:
            detail += f"; rollback also failed for {', '.join(rollback_errors)}"
        raise RuntimeError(detail) from move_error
    return moved
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
            message_b64 = base64.b64encode(str(message).encode("utf-8")).decode("ascii")
            ps_cmd = (
                f'[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; '
                f'$message = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("{message_b64}")); '
                f'$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); '
                f'$textNodes = $template.GetElementsByTagName("text"); '
                f'$textNodes.Item(0).AppendChild($template.CreateTextNode("Stash Library Manager")) > $null; '
                f'$textNodes.Item(1).AppendChild($template.CreateTextNode($message)) > $null; '
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


TRANSCODER_SUFFIXES = ("encoded", "transcoded", "h265", "hevc", "x265")
TRANSCODER_DECISION_WINDOW_SECONDS = 5.0


def _normalise_transcoder_stem(stem):
    value = str(stem or "").strip().casefold()
    for suffix in TRANSCODER_SUFFIXES:
        for separator in (" ", "-", "_"):
            token = separator + suffix
            if value.endswith(token):
                return value[:-len(token)].rstrip(" -_")
    return value


def likely_transcoder_replacement(source, destination):
    """Return True only for a conservative same-directory transcode-style replacement."""
    src = Path(source)
    dst = Path(destination)
    try:
        if src.parent.resolve() != dst.parent.resolve():
            return False
    except (OSError, ValueError):
        return False
    if src.suffix.lower() not in VIDEO_EXTENSIONS or dst.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    src_stem = src.stem.strip().casefold()
    dst_stem = dst.stem.strip().casefold()
    if not src_stem or not dst_stem:
        return False
    return src_stem == dst_stem or _normalise_transcoder_stem(dst_stem) == src_stem


def _normalized_path(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def destination_inventory_conflict(database_path, destination, source_row, connection=None):
    """Return a reason when destination belongs to any other inventoried file or scene."""
    owns_connection = connection is None
    connection = connection or connect(database_path)
    try:
        destination_key = _normalized_path(destination)
        rows = connection.execute("SELECT file_id,scene_id,path FROM files").fetchall()
        conflicts = [row for row in rows
                     if _normalized_path(row["path"]) == destination_key
                     and (str(row["file_id"]) != str(source_row["file_id"])
                          or str(row["scene_id"]) != str(source_row["scene_id"]))]
        if not conflicts:
            return None
        owners = ", ".join(
            f"file {row['file_id']} / scene {row['scene_id']}" for row in conflicts
        )
        return f"Destination is already tracked by {owners}"
    finally:
        if owns_connection:
            connection.close()


def register_transcoder_candidate(database_path, candidate):
    """Persist a neutral candidate only when it maps to one existing inventoried source."""
    candidate_path = Path(candidate)
    connection = connect(database_path)
    try:
        prior = connection.execute(
            "SELECT status FROM transcoder_candidates WHERE candidate_path=?", (str(candidate_path),)
        ).fetchone()
        if prior and prior["status"] == "independent":
            return None
        rows = [dict(row) for row in connection.execute("SELECT file_id,scene_id,path FROM files")]
    finally:
        connection.close()
    sources = [row for row in rows if Path(row["path"]).is_file()
               and likely_transcoder_replacement(row["path"], candidate_path)
               and not destination_inventory_conflict(database_path, candidate_path, row)]
    if len(sources) != 1:
        return None
    source = sources[0]["path"]
    now = utc_now()
    connection = connect(database_path)
    try:
        connection.execute(
            """INSERT INTO transcoder_candidates(candidate_path,source_path,detected_at,updated_at,status,detail)
               VALUES (?,?,?,?, 'waiting', ?)
               ON CONFLICT(candidate_path) DO UPDATE SET source_path=excluded.source_path,
                   updated_at=excluded.updated_at,
                   status=CASE WHEN transcoder_candidates.status='independent'
                               THEN 'independent' ELSE 'waiting' END,
                   detail=CASE WHEN transcoder_candidates.status='independent'
                               THEN transcoder_candidates.detail ELSE excluded.detail END""",
            (str(candidate_path), source, now, now,
             "Possible encoded replacement detected; waiting for the original file"),
        )
        connection.commit()
        return source
    finally:
        connection.close()


def remove_transcoder_candidate(database_path, candidate_path):
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT source_path FROM transcoder_candidates WHERE candidate_path=?",
            (str(candidate_path),),
        ).fetchone()
        connection.execute("DELETE FROM transcoder_candidates WHERE candidate_path=?", (str(candidate_path),))
        connection.commit()
        return row["source_path"] if row else None
    finally:
        connection.close()


def promote_transcoder_review(database_path, source, detail):
    """Expose deferred created/deleted events only when automatic replacement cannot proceed."""
    connection = connect(database_path)
    try:
        candidates = [row["candidate_path"] for row in connection.execute(
            "SELECT candidate_path FROM transcoder_candidates WHERE source_path=?", (str(source),)
        )]
        connection.execute(
            "UPDATE filesystem_events SET status='pending' WHERE event_type='deleted' AND source_path=?",
            (str(source),),
        )
        for candidate in candidates:
            connection.execute(
                "UPDATE filesystem_events SET status='pending' WHERE event_type='created' AND source_path=?",
                (candidate,),
            )
        connection.execute(
            "UPDATE transcoder_candidates SET status='review',updated_at=?,detail=? WHERE source_path=?",
            (utc_now(), str(detail), str(source)),
        )
        connection.commit()
    finally:
        connection.close()


def clear_transcoder_candidates(database_path, source):
    connection = connect(database_path)
    try:
        connection.execute("DELETE FROM transcoder_candidates WHERE source_path=?", (str(source),))
        connection.commit()
    finally:
        connection.close()


def transcoder_replacement_decision(database_path, source, source_row):
    """Select the sole safe same-folder replacement, or fail closed with a reason."""
    source_path = Path(source)
    try:
        candidates = [candidate for candidate in source_path.parent.iterdir()
                      if candidate.is_file()
                      and _normalized_path(candidate) != _normalized_path(source_path)
                      and likely_transcoder_replacement(source_path, candidate)]
    except (OSError, ValueError) as error:
        return None, f"Could not inspect replacement folder safely: {error}"
    candidates.sort(key=lambda candidate: candidate.name.casefold())
    conflicts = []
    for candidate in candidates:
        conflict = destination_inventory_conflict(database_path, candidate, source_row)
        if conflict:
            conflicts.append(f"{candidate.name}: {conflict}")
    if conflicts:
        return None, "; ".join(conflicts)
    if not candidates:
        return None, "No plausible same-folder transcoder replacement exists"
    if len(candidates) != 1:
        names = ", ".join(candidate.name for candidate in candidates)
        return None, f"Multiple plausible same-folder transcoder replacements exist: {names}"
    return str(candidates[0]), "Exactly one unowned same-folder transcoder replacement exists"


def stash_destination_is_exclusive(stash, destination, expected_scene_id):
    """Confirm Stash associates destination with the expected scene and no other scene."""
    result = stash.call_GQL(SCENE_BY_PATH_QUERY, {"path": str(destination)})
    scenes = ((result or {}).get("findScenes") or {}).get("scenes") or []
    owners = []
    destination_key = _normalized_path(destination)
    for scene in scenes:
        if any(_normalized_path(item.get("path")) == destination_key
               for item in scene.get("files") or [] if item.get("path")):
            owners.append(str(scene.get("id")))
    unique_owners = sorted(set(owners))
    if unique_owners != [str(expected_scene_id)]:
        label = ", ".join(unique_owners) if unique_owners else "no scene"
        return False, f"Stash reports destination ownership as {label}, expected only scene {expected_scene_id}"
    return True, "Stash reports the destination only on the expected scene"


def inventoried_source(database_path, source):
    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM files WHERE path=?", (source,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


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
    def __init__(self, database_path, stash, enabled, notifications, transcoder_compatibility=False):
        super().__init__(daemon=True)
        self.database_path, self.stash = database_path, stash
        self.enabled, self.notifications = enabled, notifications
        self.transcoder_compatibility = bool(transcoder_compatibility)
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
            compatibility_candidate = False
            try:
                row, reason = tracked_move(self.database_path, source, destination)
                transcode_replacement = False
                if not row and self.transcoder_compatibility and likely_transcoder_replacement(source, destination):
                    row = inventoried_source(self.database_path, source)
                    if row:
                        transcode_replacement = True
                        reason = "Likely same-folder transcoder replacement accepted by explicit compatibility setting"
                if not row:
                    record_activity(self.database_path, "filesystem", "external move", "review",
                                    severity="warning", old_path=source, new_path=destination, detail=reason)
                    notify(self.notifications, f"File move needs review: {Path(destination).name}")
                    continue
                compatibility_candidate = (self.transcoder_compatibility
                                           and likely_transcoder_replacement(source, destination))
                if compatibility_candidate:
                    selected, safety_reason = transcoder_replacement_decision(
                        self.database_path, source, row
                    )
                    if not selected or _normalized_path(selected) != _normalized_path(destination):
                        detail = safety_reason if not selected else (
                            f"Queued destination is no longer the sole safe replacement; selected {selected}"
                        )
                        record_activity(self.database_path, "filesystem", "transcoder replacement", "review",
                                        severity="warning", scene_id=row["scene_id"], file_id=row["file_id"],
                                        old_path=source, new_path=destination, detail=detail)
                        notify(self.notifications, f"Transcoder replacement needs review: {Path(destination).name}")
                        promote_transcoder_review(self.database_path, source, detail)
                        continue
                record_activity(self.database_path, "filesystem",
                                "transcoder replacement" if transcode_replacement else "external move",
                                "accepted" if transcode_replacement else "verified",
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
                    if compatibility_candidate:
                        selected, safety_reason = transcoder_replacement_decision(
                            self.database_path, source, row
                        )
                        stash_safe, stash_reason = stash_destination_is_exclusive(
                            self.stash, destination, row["scene_id"]
                        )
                        if (not selected or _normalized_path(selected) != _normalized_path(destination)
                                or not stash_safe):
                            detail = safety_reason if not selected else stash_reason
                            record_activity(self.database_path, "reconciliation", "transcoder replacement", "review",
                                            severity="warning", scene_id=row["scene_id"], file_id=row["file_id"],
                                            old_path=source, new_path=destination, detail=detail)
                            notify(self.notifications, f"Transcoder replacement changed during scan; review scene {row['scene_id']}")
                            promote_transcoder_review(self.database_path, source, detail)
                            continue
                    connection = connect(self.database_path)
                    try:
                        conflict = None
                        if compatibility_candidate:
                            connection.execute("BEGIN IMMEDIATE")
                            conflict = destination_inventory_conflict(
                                self.database_path, destination, row, connection=connection
                            )
                        if conflict:
                            connection.rollback()
                            record_activity(self.database_path, "reconciliation", "transcoder replacement", "review",
                                            severity="warning", scene_id=row["scene_id"], file_id=row["file_id"],
                                            old_path=source, new_path=destination, detail=conflict)
                            notify(self.notifications, f"Transcoder replacement ownership changed; review scene {row['scene_id']}")
                            promote_transcoder_review(self.database_path, source, conflict)
                            continue
                        connection.execute("UPDATE files SET path=?,basename=?,exists_on_disk=1,last_seen_at=?,missing_since=NULL WHERE file_id=?",
                                           (destination, Path(destination).name, utc_now(), row["file_id"]))
                        connection.commit()
                    finally:
                        connection.close()
                    record_activity(self.database_path, "reconciliation", "targeted Stash scan", "updated",
                                    scene_id=row["scene_id"], file_id=row["file_id"], old_path=source,
                                    new_path=destination, detail=f"Stash scan job {job_id} confirmed the new path")
                    if transcode_replacement:
                        resolve_filesystem_event(self.database_path, "deleted", source)
                        resolve_filesystem_event(self.database_path, "created", destination)
                    else:
                        resolve_filesystem_event(self.database_path, "moved", source, destination)
                    if compatibility_candidate:
                        resolve_filesystem_event(self.database_path, "deleted", source)
                        resolve_filesystem_event(self.database_path, "created", destination)
                        clear_transcoder_candidates(self.database_path, source)
                    notify(self.notifications, f"Stash updated: {Path(destination).name}")

                    # Companion relocation is all-or-nothing: a partial failure restores
                    # every companion already moved and leaves a clear review warning.
                    try:
                        moved_companions = relocate_companions_transactionally(
                            self.database_path, source, destination
                        )
                        for old_companion, new_companion in moved_companions:
                            record_activity(self.database_path, "companion", "moved companion", "recorded",
                                            scene_id=row["scene_id"], old_path=str(old_companion),
                                            new_path=str(new_companion),
                                            detail=f"Moved companion file alongside {Path(destination).name}")
                    except Exception as companion_error:
                        record_activity(self.database_path, "companion", "external move companions", "review",
                                        severity="warning", scene_id=row["scene_id"], old_path=source,
                                        new_path=destination, detail=str(companion_error))
                        notify(self.notifications, f"Companion files need review: {Path(destination).name}")
                else:
                    detail = f"Stash scan job {job_id} did not attach the destination to the original scene"
                    record_activity(self.database_path, "reconciliation", "targeted Stash scan", "review",
                                    severity="warning", scene_id=row["scene_id"], file_id=row["file_id"],
                                    old_path=source, new_path=destination, detail=detail)
                    notify(self.notifications, f"Stash did not adopt moved file; review scene {row['scene_id']}")
                    if compatibility_candidate:
                        promote_transcoder_review(self.database_path, source, detail)
            except Exception as error:
                record_activity(self.database_path, "reconciliation", "external move", "failed",
                                severity="error", old_path=source, new_path=destination, detail=str(error))
                notify(self.notifications, f"Move reconciliation failed: {Path(destination).name}")
                if compatibility_candidate:
                    promote_transcoder_review(self.database_path, source, str(error))


class CompletedDownloadWorker(threading.Thread):
    """Wait for new incoming videos to settle, then request one targeted Stash scan."""
    def __init__(self, database_path, stash, incoming_folder, enabled, settle_seconds, notifications,
                 fallback_seconds=60, max_attempts=3, incoming_folders=None, track_temporary_downloads=False):
        super().__init__(daemon=True)
        self.database_path, self.stash = database_path, stash
        raw_folders = incoming_folders if incoming_folders is not None else ([incoming_folder] if incoming_folder else [])
        self.incoming_folders = []
        for f in raw_folders:
            if f:
                try:
                    p = Path(f)
                    if p not in self.incoming_folders:
                        self.incoming_folders.append(p)
                    resolved = p.resolve()
                    if resolved not in self.incoming_folders:
                        self.incoming_folders.append(resolved)
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
        self.companion_retry_interval = 300.0
        self.started_at = time.time()
        self.last_fallback = self.started_at
        self.generate_contact_sheets = False
        self.contact_sheet_grid = "4x4"
        self.contact_sheet_banner = True
        self.contact_sheet_adjust_vertical = True
        self.contact_sheet_script = ""
        self.allow_custom_contact_sheet_script = False
        self.track_temporary_downloads = track_temporary_downloads
        self._active_scans = {}
        self._pending_scans = {}
        self._max_consecutive_passes = 5
        self._scan_lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._active_recovery_scans = {}
        self._pending_recovery_scans = set()
        self._deferred_lock = threading.Lock()
        self._deferred_busy_submissions = set()
        self._deferred_busy_relocations = []
        self.availability_tracker = None
        if self.enabled:
            self._restore_candidates()
            self._recover_recent_files()

    def _is_inside_incoming(self, candidate_path):
        if not self.incoming_folders or not candidate_path:
            return False
        return self._owning_incoming_folder(candidate_path) is not None

    def accepts(self, path):
        if not self.enabled or not path:
            return False
        candidate = Path(path)
        if is_temporary_download(candidate):
            if not getattr(self, "track_temporary_downloads", False):
                return False
            return self._is_inside_incoming(candidate)
        suffix = candidate.suffix.lower()
        if suffix not in (VIDEO_EXTENSIONS | COMPANION_EXTENSIONS):
            return False
        return self._is_inside_incoming(candidate)

    def _current_incoming_paths(self):
        if not self.incoming_folders:
            return set()
        valid = self._scannable_extensions()
        found = set()
        for folder in self.incoming_folders:
            try:
                if not folder.is_dir():
                    continue
                for path in folder.rglob("*"):
                    try:
                        if not path.is_file():
                            continue
                        if path.suffix.lower() in valid or (getattr(self, "track_temporary_downloads", False) and is_temporary_download(path)):
                            found.add(str(path.resolve()))
                    except OSError as err:
                        if is_network_disconnect_error(err, path):
                            break
                        continue
            except OSError as exc:
                logger.debug("Failed scanning incoming folder %s: %s", folder, exc)
        return found

    def _current_video_paths(self):
        """Alias for _current_incoming_paths. Retained for test contracts and backwards compatibility.
        Returns all monitored incoming paths (including companions) required for fallback rediscovery."""
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
            return bool(row and row["status"] in ("imported", "paired", "dismissed", "unmatched", "ignored"))
        finally:
            connection.close()

    def _scannable_extensions(self):
        valid = VIDEO_EXTENSIONS | COMPANION_EXTENSIONS
        if getattr(self, "track_temporary_downloads", False):
            valid = valid | TEMPORARY_DOWNLOAD_EXTENSIONS
        return valid

    def _folder_key(self, folder):
        if not folder:
            return ""
        try:
            return os.path.normpath(str(folder))
        except Exception:
            return str(folder)

    def _normalize_prefixes(self, p):
        s = os.path.normpath(os.path.abspath(str(p)))
        if os.name == "nt":
            s = os.path.normcase(s)
        variants = [s]
        if os.name != "nt":
            if s.startswith("/private/"):
                variants.append(s[len("/private"):])
            elif s.startswith("/var/") or s.startswith("/tmp/") or s.startswith("/etc/"):
                variants.append("/private" + s)
        return variants

    def _owning_incoming_folder(self, path):
        if not self.incoming_folders or not path:
            return None
        path_variants = self._normalize_prefixes(path)
        best_match = None
        for folder in self.incoming_folders:
            folder_variants = self._normalize_prefixes(folder)
            matched = any(
                pv == fv or pv.startswith(fv + os.sep)
                for fv in folder_variants
                for pv in path_variants
            )
            if matched:
                folder_str = os.path.normpath(str(folder))
                if best_match is None or len(folder_str) > len(os.path.normpath(str(best_match))):
                    best_match = folder
        return best_match

    def _enqueue_pending_scan(self, key, folder, cutoff=None, tree=None):
        pending = self._pending_scans.setdefault(key, {
            "folder": Path(folder),
            "cutoff": cutoff,
            "trees": set(),
            "full_scan": False,
        })
        if cutoff is not None:
            if pending["cutoff"] is None:
                pending["cutoff"] = cutoff
            else:
                pending["cutoff"] = min(pending["cutoff"], cutoff)
        if tree is not None:
            tree_p = Path(tree)
            try:
                if tree_p.resolve() == Path(folder).resolve():
                    pending["full_scan"] = True
                    pending["trees"].clear()
                elif not pending["full_scan"]:
                    pending["trees"].add(tree_p)
                    if len(pending["trees"]) > 50:
                        pending["full_scan"] = True
                        pending["cutoff"] = 0.0
                        pending["trees"].clear()
            except OSError:
                pass

    def _dispatch_folder_scan(self, folder, cutoff):
        if not self.enabled or self.stopping:
            return None
        folder_path = Path(folder)
        key = self._folder_key(folder_path)
        with self._scan_lock:
            existing = self._active_scans.get(key)
            if existing is not None:
                if existing.is_alive():
                    self._enqueue_pending_scan(key, folder_path, cutoff=cutoff)
                    return None
                else:
                    self._active_scans.pop(key, None)
            thread = threading.Thread(
                target=self._run_folder_scan,
                args=(key, folder_path),
                kwargs={"initial_cutoff": cutoff},
                name=f"incoming-scan-{folder_path.name or 'folder'}",
                daemon=True,
            )
            self._active_scans[key] = thread
            thread.start()
            return thread

    def _run_folder_scan(self, key, folder, initial_cutoff=None, initial_tree=None, result=None, prev_thread=None):
        if prev_thread is not None:
            try:
                prev_thread.join()
            except Exception:
                pass

        pass_count = 0
        max_passes = getattr(self, "_max_consecutive_passes", 5)
        current_cutoff = initial_cutoff
        current_tree = initial_tree
        is_initial = (current_tree is not None or current_cutoff is not None)

        try:
            while not self.stopping and pass_count < max_passes:
                if not is_initial:
                    with self._scan_lock:
                        if self.stopping:
                            break
                        pending = self._pending_scans.pop(key, None)
                        if not pending:
                            break

                    pass_count += 1
                    trees = list(pending.get("trees") or [])
                    cutoff = pending.get("cutoff")
                    full_scan = pending.get("full_scan")

                    for t_path in trees:
                        if self.stopping:
                            break
                        try:
                            self._scan_tree_worker(t_path)
                        except Exception as exc:
                            logger.debug("Error scanning deferred tree %s: %s", t_path, exc)

                    if self.stopping:
                        break

                    if full_scan:
                        try:
                            self._scan_incoming_folder(folder, 0.0)
                        except Exception as exc:
                            logger.debug("Error during full scan for %s: %s", folder, exc)
                    elif cutoff is not None:
                        try:
                            self._scan_incoming_folder(folder, cutoff)
                        except Exception as exc:
                            logger.debug("Error during cutoff scan for %s: %s", folder, exc)
                else:
                    pass_count += 1
                    is_initial = False
                    try:
                        if current_tree is not None:
                            count = self._scan_tree_worker(current_tree)
                            if result is not None and pass_count == 1:
                                result[0] = count
                        elif current_cutoff is not None:
                            self._scan_incoming_folder(folder, current_cutoff)
                    except Exception as exc:
                        logger.debug("Error during initial scan for %s: %s", folder, exc)
        finally:
            with self._scan_lock:
                if key in self._pending_scans and not self.stopping:
                    curr_thread = threading.current_thread()
                    next_thread = threading.Thread(
                        target=self._run_folder_scan,
                        args=(key, folder),
                        kwargs={"prev_thread": curr_thread},
                        name=f"incoming-continuation-{Path(folder).name or 'folder'}",
                        daemon=True,
                    )
                    self._active_scans[key] = next_thread
                    next_thread.start()
                else:
                    if self._active_scans.get(key) is threading.current_thread():
                        self._active_scans.pop(key, None)

    def _scan_incoming_folder(self, folder, cutoff):
        folder_path = Path(folder)
        try:
            try:
                if not folder_path.is_dir():
                    return
            except OSError as err:
                logger.debug("Cannot access incoming folder %s: %s", folder_path, err)
                return

            valid = self._scannable_extensions()
            try:
                for path in folder_path.rglob("*"):
                    if self.stopping:
                        break
                    try:
                        if not path.is_file():
                            continue
                        suffix = path.suffix.lower()
                        if suffix not in valid:
                            if not (getattr(self, "track_temporary_downloads", False) and is_temporary_download(path)):
                                continue
                        resolved = str(path.resolve())
                        with self.lock:
                            pending = resolved in self.candidates
                        if pending or self._is_in_inventory(resolved) or self._was_imported(resolved):
                            continue
                        try:
                            if path.stat().st_mtime >= cutoff:
                                self.submit(resolved)
                        except OSError:
                            continue
                    except OSError as err:
                        if is_network_disconnect_error(err, path):
                            break
                        continue
            except OSError as exc:
                logger.debug("Failed scanning incoming folder %s: %s", folder_path, exc)
        except Exception as exc:
            logger.debug("Unexpected error scanning incoming folder %s: %s", folder_path, exc)

    def _wait_scans(self, threads, max_wait=0.05):
        deadline = time.monotonic() + max_wait
        for t in threads:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            t.join(timeout=rem)

    def _recover_recent_files(self):
        """Recover downloads that completed shortly before the watcher restarted."""
        if not self.enabled:
            return
        recent_after = self.started_at - self.settle_seconds
        threads = []
        for folder in self.incoming_folders:
            t = self._dispatch_folder_scan(folder, recent_after)
            if t:
                threads.append(t)
        self._wait_scans(threads, max_wait=0.05)

    def _restore_candidates(self):
        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT path,size,modified_ns,stable_since,attempts,status,detail,first_seen_at FROM incoming_files WHERE status IN ('waiting','scanning','downloading')"
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            path = row["path"]
            try:
                p = Path(path)
                if not self.accepts(path) or not p.is_file() or self._is_in_inventory(path):
                    continue
                stat = p.stat()
            except OSError as err:
                logger.debug("Candidate file %s inaccessible or removed during restore: %s", path, err)
                continue
            unchanged = row["size"] == stat.st_size and row["modified_ns"] == stat.st_mtime_ns
            is_temporary = is_temporary_download(path)
            is_companion = (not is_temporary) and (Path(path).suffix.lower() in COMPANION_EXTENSIONS)
            with self.lock:
                self.candidates[path] = {
                    "size": stat.st_size,
                    "modified_ns": stat.st_mtime_ns,
                    "stable_since": float(row["stable_since"] or self.started_at) if unchanged else self.started_at,
                    "attempts": int(row["attempts"] or 0),
                    "is_companion": is_companion,
                    "is_temporary": is_temporary,
                    "last_saved_status": row["status"],
                    "last_saved_detail": row["detail"],
                    "first_seen_at": row["first_seen_at"],
                    "check_after": 0.0,
                }

    def _save_state(self, path, status, *, stat=None, stable_since=None, job_id=None, detail=None, attempts=None, first_seen_at=None):
        connection = connect(self.database_path)
        try:
            existing = connection.execute("SELECT attempts, first_seen_at FROM incoming_files WHERE path=?", (path,)).fetchone()
            if status == "gone" and not existing:
                return
            attempt_count = int(existing["attempts"] if existing else 0) if attempts is None else int(attempts)
            initial_seen = (existing["first_seen_at"] if existing and existing["first_seen_at"] else None) or first_seen_at or utc_now()
            connection.execute(
                """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,size,modified_ns,stable_since,settle_seconds,status,attempts,scan_job_id,detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET last_checked_at=excluded.last_checked_at,
                     size=COALESCE(excluded.size,incoming_files.size),
                     modified_ns=COALESCE(excluded.modified_ns,incoming_files.modified_ns),
                     stable_since=COALESCE(excluded.stable_since,incoming_files.stable_since),
                     settle_seconds=excluded.settle_seconds,status=excluded.status,
                     attempts=excluded.attempts,scan_job_id=excluded.scan_job_id,detail=excluded.detail""",
                (path, initial_seen, utc_now(), stat.st_size if stat else None,
                 stat.st_mtime_ns if stat else None, stable_since, self.settle_seconds, status, attempt_count,
                 str(job_id) if job_id is not None else None, detail),
            )
            connection.commit()
        finally:
            connection.close()

    def submit(self, path):
        if not self.accepts(path):
            return False
        root_key = self._owning_incoming_folder(path) or str(path)

        def _resolve_and_stat():
            try:
                norm = str(Path(path).resolve())
            except OSError:
                norm = os.path.normpath(os.path.abspath(str(path)))
            st = Path(norm).stat()
            return norm, st

        try:
            res, status = default_fs_limiter.run(root_key, _resolve_and_stat, timeout=1.5)
            if status in ("timeout", "outage"):
                if getattr(self, "availability_tracker", None):
                    self.availability_tracker.mark_root_unavailable(root_key)
                return False
            elif status == "busy":
                with self._deferred_lock:
                    self._deferred_busy_submissions.add(str(path))
                self.wake.set()
                return False
            if not res:
                return False
            normalized, stat = res
        except OSError:
            return False

        if consume_expected_create(self.database_path, normalized) or consume_expected_create(self.database_path, str(path)):
            return False
        if self._is_in_inventory(normalized) or self._was_imported(normalized):
            return False
        with self.lock:
            current = self.candidates.get(normalized)
        stable_since = time.time()
        first_seen = current.get("first_seen_at") if current else utc_now()
        if current and current["size"] == stat.st_size and current["modified_ns"] == stat.st_mtime_ns:
            stable_since = current["stable_since"]
        is_temporary = is_temporary_download(normalized)
        is_companion = (not is_temporary) and (Path(normalized).suffix.lower() in COMPANION_EXTENSIONS)
        if is_temporary:
            status = "downloading"
            detail = "Incoming download in progress"
        elif is_companion:
            status = "waiting"
            detail = "Waiting for companion file to stabilise"
        else:
            status = "waiting"
            detail = f"Waiting for video to remain unchanged for {self.settle_seconds // 60} minute(s)"
        candidate = {
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "stable_since": stable_since,
            "attempts": current["attempts"] if current else 0,
            "is_companion": is_companion,
            "is_temporary": is_temporary,
            "last_saved_status": status,
            "last_saved_detail": detail,
            "first_seen_at": first_seen,
            "check_after": 0.0,
        }
        with self.lock:
            self.candidates[normalized] = candidate
            if not is_companion and not is_temporary:
                video_p = Path(normalized)
                for c_path, c_info in self.candidates.items():
                    if c_info.get("is_companion"):
                        cand_p = Path(c_path)
                        try:
                            if cand_p.parent == video_p.parent:
                                matched, _ = match_companion_to_video(cand_p, video_p)
                                if matched or is_generic_artwork(cand_p):
                                    c_info["check_after"] = 0.0
                        except OSError:
                            pass
        self._save_state(normalized, status, stat=stat, stable_since=stable_since,
                         attempts=candidate["attempts"], detail=detail, first_seen_at=first_seen)
        self.wake.set()
        return True

    def submit_tree(self, path):
        """Discover videos inside a newly-created or newly-moved download directory."""
        if not self.enabled or not path:
            return 0
        owning_folder = self._owning_incoming_folder(path)
        if owning_folder is None:
            return 0

        key = self._folder_key(owning_folder)
        root = Path(path)
        result = [0]
        with self._scan_lock:
            existing = self._active_scans.get(key)
            if existing is not None and existing.is_alive():
                self._enqueue_pending_scan(key, owning_folder, tree=root)
                return 0
            thread = threading.Thread(
                target=self._run_folder_scan,
                args=(key, owning_folder),
                kwargs={"initial_tree": root, "result": result},
                name=f"incoming-tree-{root.name or 'tree'}",
                daemon=True,
            )
            self._active_scans[key] = thread
            thread.start()

        self._wait_scans([thread], max_wait=0.05)
        return result[0]

    def _scan_tree_worker(self, root, key=None, result=None):
        try:
            try:
                if not root.is_dir():
                    return 0
            except OSError as err:
                logger.debug("submit_tree cannot access %s: %s", root, err)
                return 0

            count = 0
            for child in root.rglob("*"):
                if self.stopping:
                    break
                try:
                    if child.is_file() and self.submit(child):
                        count += 1
                except OSError as err:
                    if is_network_disconnect_error(err, child):
                        break
                    continue
            if result is not None:
                result[0] = count
            return count
        except OSError as exc:
            logger.debug("submit_tree scan failed on %s: %s", root, exc)
            return 0

    def _resolve_relocation(self, path):
        with self.lock:
            seen = set()
            while path in self.relocations and path not in seen:
                seen.add(path)
                path = self.relocations[path]
        return path

    def relocate(self, source, destination):
        """Carry an unimported download's wait/scan state to its new path."""
        root_key = self._owning_incoming_folder(destination) or self._owning_incoming_folder(source) or str(destination)

        def _resolve_both():
            try:
                s = str(Path(source).resolve())
            except OSError:
                s = os.path.normpath(os.path.abspath(str(source)))
            try:
                d = str(Path(destination).resolve())
            except OSError:
                d = os.path.normpath(os.path.abspath(str(destination)))
            return s, d

        try:
            res, status = default_fs_limiter.run(root_key, _resolve_both, timeout=1.5)
            if status in ("timeout", "outage"):
                if getattr(self, "availability_tracker", None):
                    self.availability_tracker.mark_root_unavailable(root_key)
                return False
            elif status == "busy":
                with self._deferred_lock:
                    self._deferred_busy_relocations.append((str(source), str(destination)))
                self.wake.set()
                return False
            if not res:
                return False
            source, destination = res
        except OSError:
            return False

        is_dest_video = Path(destination).suffix.lower() in VIDEO_EXTENSIONS
        is_dest_temp = is_temporary_download(destination)
        if not self.enabled or (not is_dest_video and not is_dest_temp):
            return False
        if self._is_in_inventory(destination) or self._was_imported(destination):
            return False

        with self.lock:
            if self.relocations.get(source) == destination and destination in self.candidates:
                return True
            in_candidates = source in self.candidates

        if not in_candidates:
            if self._is_in_inventory(source) or self._was_imported(source):
                return False
            connection = connect(self.database_path)
            try:
                row = connection.execute(
                    "SELECT size,modified_ns,stable_since,attempts,status,first_seen_at FROM incoming_files WHERE path=? AND status IN ('waiting','scanning','downloading')",
                    (source,)
                ).fetchone()
            finally:
                connection.close()
            if not row:
                return False

        try:
            stat, status = default_fs_limiter.run(root_key, lambda: Path(destination).stat(), timeout=1.5)
            if status in ("timeout", "outage"):
                if getattr(self, "availability_tracker", None):
                    self.availability_tracker.mark_root_unavailable(root_key)
                return False
            elif status == "busy":
                with self._deferred_lock:
                    self._deferred_busy_relocations.append((str(source), str(destination)))
                self.wake.set()
                return False
            if not stat:
                return False
        except OSError:
            return False

        with self.lock:
            if in_candidates:
                candidate = self.candidates.pop(source, None)
                if candidate is None:
                    return False
            else:
                if source in self.candidates:
                    candidate = self.candidates.pop(source, None)
                else:
                    if self._is_in_inventory(source) or self._was_imported(source):
                        return False
                    connection = connect(self.database_path)
                    try:
                        row = connection.execute(
                            "SELECT size,modified_ns,stable_since,attempts,status,first_seen_at FROM incoming_files WHERE path=? AND status IN ('waiting','scanning','downloading')",
                            (source,)
                        ).fetchone()
                    finally:
                        connection.close()
                    if not row:
                        return False
                    candidate = {
                        "size": row["size"],
                        "modified_ns": row["modified_ns"],
                        "stable_since": float(row["stable_since"] or time.time()),
                        "attempts": int(row["attempts"] or 0),
                        "is_temporary": row["status"] == "downloading",
                        "first_seen_at": row["first_seen_at"],
                    }

            if is_dest_temp:
                candidate["is_temporary"] = True
                if candidate.get("size") != stat.st_size or candidate.get("modified_ns") != stat.st_mtime_ns:
                    candidate.update(size=stat.st_size, modified_ns=stat.st_mtime_ns, stable_since=time.time())
                self.relocations[source] = destination
                self.candidates[destination] = candidate
                self._save_state(source, "moved", detail=f"Download state transferred to {destination}")
                self._save_state(destination, "downloading", stat=stat, stable_since=candidate["stable_since"],
                                 attempts=candidate.get("attempts", 0), detail="Incoming download in progress",
                                 first_seen_at=candidate.get("first_seen_at"))
                self.wake.set()
                return True
            else:
                if candidate.get("is_temporary"):
                    candidate["is_temporary"] = False
                    candidate["stable_since"] = time.time()
                if candidate.get("size") != stat.st_size or candidate.get("modified_ns") != stat.st_mtime_ns:
                    candidate.update(size=stat.st_size, modified_ns=stat.st_mtime_ns, stable_since=time.time())
                self.relocations[source] = destination
                self.candidates[destination] = candidate
                self._save_state(source, "moved", detail=f"Download completed and renamed to {destination}")
                self._save_state(destination, "waiting", stat=stat, stable_since=candidate["stable_since"],
                                 attempts=candidate.get("attempts", 0), detail=f"Waiting for video to remain unchanged for {self.settle_seconds // 60} minute(s)",
                                 first_seen_at=candidate.get("first_seen_at"))
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
        try:
            self._save_state(path, "scanning", stable_since=candidate["stable_since"], attempts=attempts,
                             detail="Stash is adding the video and generating its thumbnail and previews")
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
                        custom_script=self.contact_sheet_script,
                        allow_custom_script=self.allow_custom_contact_sheet_script
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
                        result_detail = csm_res.get("error") or csm_res.get("message") or "Contact sheet generation did not complete"
                        self._save_state(sheet_p, "gone", detail=result_detail)
                        if csm_res.get("status") == "error":
                            record_activity(
                                self.database_path, "companion", "contact sheet generation", "failed",
                                severity="error", scene_id=scene["id"], old_path=path, detail=result_detail
                            )
                except Exception as exc:
                    logger.debug("Contact sheet generation failed for %s: %s", path, exc)
                    record_activity(
                        self.database_path, "companion", "contact sheet generation", "failed",
                        severity="error", scene_id=scene["id"], old_path=path, detail=str(exc)
                    )
                    try:
                        self._save_state(sheet_p, "gone", detail="Contact sheet generation failed")
                    except Exception as save_exc:
                        logger.debug("save_state after contact sheet failure failed: %s", save_exc)

            return True
        except Exception as error:
            is_db_error = isinstance(error, sqlite3.Error)
            if is_db_error:
                # Safeguard 2: Database lock/error is not a scan failure; do not exhaust normal scan attempts
                original_stable = candidate.get("stable_since", time.time())
                with self.lock:
                    self.candidates[path] = {
                        **candidate,
                        "stable_since": original_stable,
                        "check_after": time.monotonic() + 10.0,
                    }
                logger.warning(
                    "Database error during incoming scan for %s; scheduled retry in 10s (attempt count preserved at %d): %s",
                    path, candidate.get("attempts", 0), error
                )
                try:
                    self._save_state(path, "waiting", stable_since=original_stable, attempts=candidate.get("attempts", 0),
                                     detail=f"Database busy during scan; will retry: {error}")
                except Exception as save_err:
                    logger.debug("Could not update state for %s after database error: %s", path, save_err)
            else:
                file_present = True
                try:
                    file_present = Path(path).is_file()
                except OSError:
                    file_present = True
                if attempts < self.max_attempts and file_present:
                    retry_at = time.time()
                    with self.lock:
                        self.candidates[path] = {
                            **candidate,
                            "stable_since": retry_at,
                            "attempts": attempts,
                            "check_after": time.monotonic() + 5.0,
                        }
                    try:
                        self._save_state(path, "waiting", stable_since=retry_at, attempts=attempts,
                                         detail=f"Scan attempt {attempts} failed; it will retry: {error}")
                    except Exception as save_err:
                        logger.warning("Could not update state for %s after scan failure: %s", path, save_err)
                else:
                    try:
                        self._save_state(path, "failed", attempts=attempts, detail=str(error))
                    except Exception as save_err:
                        logger.warning("Could not mark %s failed: %s", path, save_err)
                    try:
                        record_activity(self.database_path, "incoming", "completed video scan", "failed",
                                        severity="error", new_path=path, detail=str(error))
                    except Exception as act_err:
                        logger.warning("Could not record failure activity for %s: %s", path, act_err)
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

        eligible_videos = find_eligible_videos_in_folder(
            video_p.parent, self.database_path, dict(candidates_list)
        )
        single_video_in_folder = (len(eligible_videos) <= 1)

        for c_path, c_info in candidates_list:
            cand = Path(c_path)
            if cand.suffix.lower() not in COMPANION_EXTENSIONS or not cand.is_file():
                continue
            # Never pair pending companions across incoming directories.
            if cand.parent.resolve() != video_p.parent.resolve():
                continue

            matched, remainder = match_companion_to_video(cand, video_p)
            generic = is_generic_artwork(cand)
            is_img = is_image_companion(cand)

            if not matched and generic:
                if single_video_in_folder:
                    matched = True
                    exact_target = actual_path.parent / f"{actual_path.stem}{cand.suffix}"
                    if cand.stem.lower() == "cover" and not exact_target.exists():
                        remainder = ""
                    else:
                        remainder = f".{cand.stem.lower()}"
                else:
                    with self.lock:
                        self.candidates.pop(c_path, None)
                    self._save_state(c_path, "unmatched", detail="Ambiguous generic artwork: multiple videos in folder")
                    record_activity(self.database_path, "companion", "ambiguous artwork skipped", "review",
                                    old_path=c_path, detail="Generic artwork not paired because folder contains multiple videos")
                    continue

            if not matched and is_img:
                matches_other_video = False
                for other_vid in eligible_videos:
                    if other_vid.resolve() != video_p.resolve():
                        other_m, _ = match_companion_to_video(cand, other_vid)
                        if other_m:
                            matches_other_video = True
                            break
                if matches_other_video:
                    continue

                with self.lock:
                    self.candidates.pop(c_path, None)
                self._save_state(c_path, "unmatched", detail="Unrelated image: filename does not match any video in folder")
                record_activity(self.database_path, "companion", "unrelated image skipped", "recorded",
                                old_path=c_path, detail="Image filename does not match any video in folder")
                continue

            if matched:
                if cand.name.lower().startswith(actual_path.name.lower()):
                    target_name = actual_path.name + cand.suffix
                elif remainder:
                    target_name = actual_path.stem + remainder + cand.suffix
                else:
                    target_name = actual_path.stem + cand.suffix
                target_path = actual_path.parent / target_name

                if target_path != cand:
                    if target_path.exists():
                        if generic and remainder == "":
                            target_name = actual_path.stem + f".{cand.stem.lower()}" + cand.suffix
                            target_path = actual_path.parent / target_name
                    if target_path.exists() and target_path != cand:
                        self._save_state(c_path, "waiting", detail=f"Companion destination already exists: {target_path}")
                        record_activity(self.database_path, "companion", "companion collision", "review",
                                        severity="warning", old_path=str(cand), new_path=str(target_path),
                                        detail="Companion not moved because destination already exists")
                        continue
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

    def _save_unmatched_companion(self, path, candidate, detail, activity_action=None, activity_detail=None):
        try:
            self._save_state(path, "unmatched", detail=detail)
            with self.lock:
                self.candidates.pop(path, None)
            if activity_action:
                record_activity(self.database_path, "companion", activity_action, "recorded",
                                old_path=path, detail=activity_detail or detail)
        except Exception as exc:
            logger.warning("Failed saving unmatched companion state for %s: %s", path, exc)
            with self.lock:
                candidate["check_after"] = time.monotonic() + 5.0

    def _save_companion_waiting(self, path, candidate, detail):
        with self.lock:
            needs_save = (
                candidate.get("last_saved_status") != "waiting"
                or candidate.get("last_saved_detail") != detail
            )
        if needs_save:
            try:
                self._save_state(path, "waiting", detail=detail)
                with self.lock:
                    candidate["last_saved_status"] = "waiting"
                    candidate["last_saved_detail"] = detail
            except Exception as exc:
                logger.warning("Failed saving companion state for %s: %s", path, exc)
                with self.lock:
                    candidate["check_after"] = time.monotonic() + 5.0
                return
        with self.lock:
            candidate["check_after"] = time.monotonic() + self.companion_retry_interval

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
                    self._save_companion_waiting(path, candidate, f"Waiting for video {Path(other).name} to finish downloading")
                    return

        connection = connect(self.database_path)
        try:
            row, remainder = find_scene_for_companion(connection, cand_path)
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
                    if target_path.exists():
                        self._save_companion_waiting(path, candidate, f"Companion destination already exists: {target_path}")
                        record_activity(self.database_path, "companion", "companion collision", "review",
                                        severity="warning", scene_id=row["scene_id"], old_path=str(cand_path),
                                        new_path=str(target_path),
                                        detail="Companion not moved because destination already exists")
                        return
                    try:
                        expect_filesystem_move(self.database_path, str(cand_path), str(target_path))
                        cand_path.rename(target_path)
                    except OSError as err:
                        self._save_companion_waiting(path, candidate, f"Could not relocate to {target_path}: {err}")
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

        # Check image companion matching rules if no match was found above
        if is_image_companion(cand_path):
            with self.lock:
                cand_map = dict(self.candidates)
            eligible_videos = find_eligible_videos_in_folder(cand_path.parent, self.database_path, cand_map)

            if is_generic_artwork(cand_path):
                if len(eligible_videos) > 1:
                    self._save_unmatched_companion(path, candidate, "Ambiguous generic artwork: multiple videos in folder",
                                                   activity_action="ambiguous artwork skipped",
                                                   activity_detail="Generic artwork not paired because folder contains multiple videos")
                    return
                elif len(eligible_videos) == 1:
                    vid = next(iter(eligible_videos))
                    self._save_companion_waiting(path, candidate, f"Waiting for video {vid.name} to finish downloading")
                    return
                else:
                    self._save_companion_waiting(path, candidate, "Waiting for matching video to arrive")
                    return
            else:
                matching_vid = None
                for vid in eligible_videos:
                    m, _ = match_companion_to_video(cand_path, vid)
                    if m:
                        matching_vid = vid
                        break

                if matching_vid:
                    self._save_companion_waiting(path, candidate, f"Waiting for video {matching_vid.name} to finish downloading")
                    return
                elif len(eligible_videos) >= 1:
                    self._save_unmatched_companion(path, candidate, "Unrelated image: filename does not match any video in folder",
                                                   activity_action="unrelated image skipped",
                                                   activity_detail="Image filename does not match any video in folder")
                    return
                else:
                    self._save_companion_waiting(path, candidate, "Waiting for matching video to arrive")
                    return

        self._save_companion_waiting(path, candidate, "Waiting for matching video to arrive")

    def _retry_deferred_busy_operations(self):
        with self._deferred_lock:
            pending_relocations = list(self._deferred_busy_relocations)
            self._deferred_busy_relocations.clear()
            pending_submissions = list(self._deferred_busy_submissions)
            self._deferred_busy_submissions.clear()

        for src, dst in pending_relocations:
            self.relocate(src, dst)

        for p in pending_submissions:
            self.submit(p)

    def evaluate_once(self, now=None, mono_now=None):
        self._retry_deferred_busy_operations()
        now = time.time() if now is None else float(now)
        mono_now = time.monotonic() if mono_now is None else float(mono_now)
        with self.lock:
            pending = list(self.candidates.items())
        for path, candidate in pending:
            if self._was_imported(path):
                with self.lock:
                    self.candidates.pop(path, None)
                continue
            try:
                stat = Path(path).stat()
            except OSError as err:
                if is_network_disconnect_error(err, path):
                    candidate["check_after"] = mono_now + 5.0
                    continue
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
            with self.lock:
                check_after = candidate.get("check_after", 0.0)
            if mono_now < check_after:
                continue

            is_comp = candidate.get("is_companion") or Path(path).suffix.lower() in COMPANION_EXTENSIONS
            settle_needed = min(3, self.settle_seconds) if is_comp else self.settle_seconds
            if now - candidate["stable_since"] >= settle_needed:
                if is_comp:
                    self._process_companion(path, candidate)
                else:
                    with self.lock:
                        self.candidates.pop(path, None)
                    try:
                        self._scan(path, candidate)
                    except Exception as scan_err:
                        logger.error("Unhandled error scanning %s: %s", path, scan_err, exc_info=True)
                        file_present = True
                        try:
                            file_present = Path(path).is_file()
                        except OSError:
                            file_present = True
                        if file_present:
                            with self.lock:
                                if path not in self.candidates:
                                    self.candidates[path] = {
                                        **candidate,
                                        "stable_since": candidate.get("stable_since", time.time()),
                                        "check_after": mono_now + 10.0,
                                    }

    def _fallback_check(self):
        try:
            if not self.enabled:
                return
            threads = []
            for folder in self.incoming_folders:
                t = self._dispatch_folder_scan(folder, self.started_at)
                if t:
                    threads.append(t)
            self._wait_scans(threads, max_wait=0.05)
        except Exception as exc:
            logger.debug("Error in _fallback_check: %s", exc)

    def trigger_recovery_scan(self, root, max_wait=0.05):
        """Discovers files that arrived while a network share was offline.
        Genuinely asynchronous: delegates recovery work to a background worker so the
        calling thread does not execute the scan, and coalesces repeated recovery requests."""
        if not self.enabled or not root or self.stopping:
            return None
        root_str = str(root)
        with self._recovery_lock:
            existing = self._active_recovery_scans.get(root_str)
            if existing is not None and existing.is_alive():
                self._pending_recovery_scans.add(root_str)
                return existing

            t = threading.Thread(
                target=self._run_recovery_scan,
                args=(root_str,),
                name=f"recovery-scan-{Path(root_str).name or 'root'}",
                daemon=True,
            )
            self._active_recovery_scans[root_str] = t
            t.start()
        if max_wait > 0:
            t.join(timeout=float(max_wait))
        return t

    def _run_recovery_scan(self, root_str):
        while not self.stopping:
            try:
                root_variants = self._normalize_prefixes(root_str)
                threads = []
                for folder in self.incoming_folders:
                    folder_variants = self._normalize_prefixes(folder)
                    matched = any(
                        fv == rv or fv.startswith(rv.rstrip(os.sep) + os.sep)
                        for fv in folder_variants
                        for rv in root_variants
                    )
                    if matched:
                        t = self._dispatch_folder_scan(folder, self.started_at)
                        if t:
                            threads.append(t)
                if threads:
                    self._wait_scans(threads, max_wait=0.2)
            except Exception as exc:
                logger.debug("Error in _run_recovery_scan for %s: %s", root_str, exc)

            with self._recovery_lock:
                if root_str in self._pending_recovery_scans and not self.stopping:
                    self._pending_recovery_scans.discard(root_str)
                    continue
                self._active_recovery_scans.pop(root_str, None)
                break

    def stop(self):
        self.stopping = True
        self.wake.set()

    def run(self):
        while not self.stopping:
            try:
                self.evaluate_once()
            except Exception as exc:
                logger.error("Unexpected error in CompletedDownloadWorker evaluation loop: %s", exc, exc_info=True)
            now = time.time()
            if now - self.last_fallback >= self.fallback_seconds:
                self._fallback_check()
                self.last_fallback = now
            self.wake.wait(timeout=min(5, self.fallback_seconds))
            self.wake.clear()


class LibraryEventHandler(FileSystemEventHandler):
    def __init__(self, database_path, worker, notifications, incoming_worker=None, availability_tracker=None):
        self.database_path = database_path
        self.worker = worker
        self.notifications = notifications
        self.incoming_worker = incoming_worker
        self.availability_tracker = availability_tracker
        if incoming_worker and availability_tracker and not getattr(incoming_worker, "availability_tracker", None):
            incoming_worker.availability_tracker = availability_tracker
        # Track when each path last had a 'created' event so the delayed
        # "still missing?" check can tell the difference between a genuine
        # deletion and a rapid delete-then-recreate (e.g. atomic download swap).
        self._recent_creates: dict[str, float] = {}
        self._recent_deleted_videos: dict[str, float] = {}
        # Keep recently active encoded outputs long enough for the source to be deleted later.
        self._recent_video_candidates: dict[str, float] = {}
        self._transcoder_decision_timers: dict[str, threading.Timer] = {}
        self._recent_creates_lock = threading.Lock()
        # Bounded centralized deletion scheduler
        self._pending_deletions: dict[str, float] = {}
        self._deletion_scheduler_lock = threading.Lock()
        self._deletion_timer: threading.Timer | None = None
        self._deletion_in_flight_roots: set[str] = set()

    def restore_transcoder_candidates(self):
        """Restore persistent candidate decisions after a watcher or Stash restart."""
        connection = connect(self.database_path)
        try:
            rows = [dict(row) for row in connection.execute(
                "SELECT candidate_path,source_path FROM transcoder_candidates WHERE status='waiting'"
            )]
        finally:
            connection.close()
        missing_sources = set()
        for row in rows:
            if not Path(row["candidate_path"]).is_file():
                remove_transcoder_candidate(self.database_path, row["candidate_path"])
                resolve_filesystem_event(self.database_path, "created", row["candidate_path"])
            elif not Path(row["source_path"]).exists():
                missing_sources.add(row["source_path"])
        for source in sorted(missing_sources):
            record_filesystem_event(self.database_path, "deleted", source, initial_status="waiting")
            self._schedule_transcoder_decision(source)

    def _schedule_transcoder_decision(self, source):
        def decide():
            with self._recent_creates_lock:
                self._transcoder_decision_timers.pop(source, None)
            try:
                row = inventoried_source(self.database_path, source)
                if not row:
                    return
                selected, reason = transcoder_replacement_decision(self.database_path, source, row)
                if selected:
                    self.worker.submit(source, selected)
                else:
                    promote_transcoder_review(self.database_path, source, reason)
                    record_activity(self.database_path, "filesystem", "transcoder replacement", "review",
                                    severity="warning", scene_id=row["scene_id"], file_id=row["file_id"],
                                    old_path=source, detail=reason)
                    notify(self.notifications, f"Transcoder replacement needs review: {Path(source).name}")
            except Exception as exc:
                logger.debug("Transcoder decision exception for %s: %s", source, exc)

        with self._recent_creates_lock:
            previous = self._transcoder_decision_timers.pop(source, None)
            if previous:
                previous.cancel()
            timer = threading.Timer(TRANSCODER_DECISION_WINDOW_SECONDS, decide)
            timer.daemon = True
            self._transcoder_decision_timers[source] = timer
            timer.start()

    def _relevant(self, path, is_directory):
        if is_directory:
            return True
        if is_temporary_download(path):
            return True
        return Path(path).suffix.lower() in WATCHED_EXTENSIONS

    def on_created(self, event):
        if event.is_directory and self.incoming_worker:
            self.incoming_worker.submit_tree(event.src_path)
        if event.is_directory:
            return
        if self._is_path_offline(event.src_path):
            return
        # Resolve any transient delete event if the file is recreated/present
        resolve_filesystem_event(self.database_path, "deleted", event.src_path)
        if consume_expected_create(self.database_path, event.src_path):
            return
        is_temp = is_temporary_download(event.src_path)
        incoming_candidate = bool(not event.is_directory and self.incoming_worker and self.incoming_worker.submit(event.src_path))
        if is_temp:
            # Browser and downloader temporary files must never generate unverified pending filesystem events
            return
        if self._relevant(event.src_path, event.is_directory) and not incoming_candidate:
            if self.incoming_worker and self.incoming_worker._is_inside_incoming(event.src_path):
                return
            if Path(event.src_path).suffix.lower() in COMPANION_EXTENSIONS:
                cand = Path(event.src_path)
                parent = cand.parent
                root_key = self._root_for_path(event.src_path)

                def _find_matching_companion_video():
                    for v_ext in VIDEO_EXTENSIONS:
                        try:
                            if (parent / (cand.stem + v_ext)).is_file() or (parent / cand.stem).is_file():
                                return True
                        except OSError:
                            break
                    return False

                has_matching_video = False
                try:
                    res, status = default_fs_limiter.run(root_key, _find_matching_companion_video, timeout=1.0)
                    if status in ("timeout", "outage"):
                        if self.availability_tracker:
                            self.availability_tracker.mark_root_unavailable(root_key)
                        return
                    has_matching_video = bool(res) if status == "ok" else False
                except OSError:
                    has_matching_video = False

                if has_matching_video:
                    return
            with self._recent_creates_lock:
                now = time.monotonic()
                self._recent_creates[event.src_path] = now
                if Path(event.src_path).suffix.lower() in VIDEO_EXTENSIONS:
                    self._recent_video_candidates[event.src_path] = now
                cutoff = now - 30.0
                self._recent_creates = {k: v for k, v in self._recent_creates.items() if v > cutoff}
                self._recent_deleted_videos = {k: v for k, v in self._recent_deleted_videos.items() if v > cutoff}
                video_cutoff = now - 600.0
                self._recent_video_candidates = {k: v for k, v in self._recent_video_candidates.items() if v > video_cutoff}
            candidate_source = None
            if (getattr(self.worker, "transcoder_compatibility", False) is True
                    and Path(event.src_path).suffix.lower() in VIDEO_EXTENSIONS):
                candidate_source = register_transcoder_candidate(self.database_path, event.src_path)
            record_filesystem_event(
                self.database_path, "created", event.src_path, is_directory=event.is_directory,
                initial_status="waiting" if candidate_source else "pending"
            )

    def on_deleted(self, event):
        if event.is_directory:
            return
        if self._relevant(event.src_path, event.is_directory):
            removed_candidate_source = remove_transcoder_candidate(self.database_path, event.src_path)
            if removed_candidate_source:
                resolve_filesystem_event(self.database_path, "created", event.src_path)
                return
            if self.incoming_worker:
                was_candidate = False
                with self.incoming_worker.lock:
                    was_candidate = bool(self.incoming_worker.candidates.pop(event.src_path, None))
                if was_candidate or self.incoming_worker._is_inside_incoming(event.src_path):
                    self.incoming_worker._save_state(event.src_path, "gone", detail="File removed from disk")
            if not is_temporary_download(event.src_path):
                if Path(event.src_path).suffix.lower() in VIDEO_EXTENSIONS:
                    with self._recent_creates_lock:
                        now = time.monotonic()
                        self._recent_deleted_videos[event.src_path] = now
                        video_cutoff = now - 600.0
                        self._recent_video_candidates = {k: v for k, v in self._recent_video_candidates.items() if v > video_cutoff}
                should_decide = False
                if (getattr(self.worker, "transcoder_compatibility", False) is True
                        and Path(event.src_path).suffix.lower() in VIDEO_EXTENSIONS):
                    source_row = inventoried_source(self.database_path, event.src_path)
                    if source_row:
                        # A delete-first transcoder may not have emitted its final create/move
                        # yet. Always grant an inventoried video the short decision window;
                        # the callback enumerates the complete folder and fails closed.
                        should_decide = True
                record_filesystem_event(
                    self.database_path, "deleted", event.src_path,
                    is_directory=event.is_directory,
                    initial_status="waiting" if should_decide else "pending"
                )
                if should_decide:
                    self._schedule_transcoder_decision(event.src_path)
                if (not should_decide and not event.is_directory
                        and Path(event.src_path).suffix.lower() in WATCHED_EXTENSIONS):
                    # Single-flight centralized scheduling replaces per-event Timer(3, self._notify_if_still_missing)
                    self._schedule_deletion_check(event.src_path)

    def _schedule_deletion_check(self, path: str):
        with self._deletion_scheduler_lock:
            self._pending_deletions[str(path)] = time.monotonic() + 3.0
            if self._deletion_timer is None:
                self._deletion_timer = threading.Timer(0.5, self._drain_pending_deletions)
                self._deletion_timer.daemon = True
                self._deletion_timer.start()

    def _is_path_offline(self, path: str) -> bool:
        unavail = []
        if self.availability_tracker:
            unavail = self.availability_tracker.get_unavailable_roots()
        else:
            try:
                con = connect(self.database_path)
                try:
                    row = con.execute("SELECT unavailable_roots_json FROM filesystem_monitor_status WHERE id=1").fetchone()
                    if row and row["unavailable_roots_json"]:
                        unavail = json.loads(row["unavailable_roots_json"])
                finally:
                    con.close()
            except Exception:
                pass
        if not unavail:
            return False
        variants = [str(path)]
        s = str(path)
        if s.startswith("/private/"):
            variants.append(s[len("/private"):])
        elif s.startswith("/var/") or s.startswith("/tmp/") or s.startswith("/etc/"):
            variants.append("/private" + s)
        for v in variants:
            if is_file_on_unavailable_root(v, unavail):
                return True
        return False

    def _root_for_path(self, path: str) -> str:
        norm_p = os.path.normcase(os.path.normpath(str(path))).replace("\\", "/")
        all_roots = []
        if self.availability_tracker:
            all_roots = self.availability_tracker.roots
        for r in sorted(all_roots, key=len, reverse=True):
            norm_r = os.path.normcase(os.path.normpath(str(r))).replace("\\", "/")
            if norm_p == norm_r or norm_p.startswith(norm_r.rstrip("/") + "/"):
                return r
        return os.path.dirname(norm_p)

    def _drain_pending_deletions(self):
        with self._deletion_scheduler_lock:
            self._deletion_timer = None
            now = time.monotonic()

            # Only prune stale queued entries whose root is NOT actively in-flight
            expired = [
                p for p, t in self._pending_deletions.items()
                if now - t > 120.0 and self._root_for_path(p) not in self._deletion_in_flight_roots
            ]
            for p in expired:
                self._pending_deletions.pop(p, None)

            ready_paths = [p for p, t in self._pending_deletions.items() if now >= t]
            work_by_root: dict[str, list[str]] = {}
            for path in ready_paths:
                if self._is_path_offline(path):
                    self._pending_deletions.pop(path, None)
                    continue
                root_key = self._root_for_path(path)
                if root_key in self._deletion_in_flight_roots:
                    continue
                self._pending_deletions.pop(path, None)
                work_by_root.setdefault(root_key, []).append(path)

            for root_key, paths in work_by_root.items():
                self._deletion_in_flight_roots.add(root_key)
                t = threading.Thread(
                    target=self._verify_deletions_for_root,
                    args=(root_key, paths),
                    name=f"del_verify_{Path(root_key).name if root_key else 'default'}",
                    daemon=True,
                )
                t.start()

            if self._pending_deletions:
                earliest = min(self._pending_deletions.values())
                delay = max(0.1, min(1.0, earliest - now))
                self._deletion_timer = threading.Timer(delay, self._drain_pending_deletions)
                self._deletion_timer.daemon = True
                self._deletion_timer.start()

    def _verify_deletions_for_root(self, root_key: str, paths: list[str]):
        try:
            for path in paths:
                with self._recent_creates_lock:
                    created_at = self._recent_creates.get(path, 0)
                if time.monotonic() - created_at < 5.0:
                    continue
                if self._is_path_offline(path):
                    continue
                try:
                    exists = Path(path).exists()
                except OSError as err:
                    if is_network_disconnect_error(err, path):
                        continue
                    exists = False
                if not exists:
                    if self._is_path_offline(path) or is_network_disconnect_error(OSError(errno.ENOENT, "No such file"), path):
                        continue
                    record_activity(self.database_path, "filesystem", "external deletion", "review", severity="warning",
                                    old_path=path, detail="File remained absent after the notification delay")
                    notify(self.notifications, f"File deleted or moved without a paired event: {Path(path).name}")
        finally:
            with self._deletion_scheduler_lock:
                self._deletion_in_flight_roots.discard(root_key)

    def _notify_if_still_missing(self, path):
        # Suppress the warning if the file was recreated within 5 s of this check
        # (e.g. an atomic download swap or in-place replacement).
        with self._recent_creates_lock:
            created_at = self._recent_creates.get(path, 0)
        if time.monotonic() - created_at < 5.0:
            return
        if self._is_path_offline(path):
            return
        try:
            exists = Path(path).exists()
        except OSError as err:
            if is_network_disconnect_error(err, path):
                return
            exists = False
        if not exists:
            if self._is_path_offline(path) or is_network_disconnect_error(OSError(errno.ENOENT, "No such file"), path):
                return
            record_activity(self.database_path, "filesystem", "external deletion", "review", severity="warning",
                            old_path=path, detail="File remained absent after the notification delay")
            notify(self.notifications, f"File deleted or moved without a paired event: {Path(path).name}")

    def on_modified(self, event):
        # Modification events are useful only for the incoming stability timer. They do not
        # identify a move/deletion and must not create an unexplained review warning.
        if not event.is_directory and self._relevant(event.src_path, False):
            if self._is_path_offline(event.src_path):
                return
            if (getattr(self.worker, "transcoder_compatibility", False) is True
                    and Path(event.src_path).suffix.lower() in VIDEO_EXTENSIONS):
                with self._recent_creates_lock:
                    now = time.monotonic()
                    self._recent_video_candidates[event.src_path] = now
                    cutoff = now - 600.0
                    self._recent_video_candidates = {k: v for k, v in self._recent_video_candidates.items() if v > cutoff}
            if self.incoming_worker:
                self.incoming_worker.submit(event.src_path)

    def on_moved(self, event):
        if self._relevant(event.src_path, event.is_directory) or self._relevant(event.dest_path, event.is_directory):
            if event.is_directory and self.incoming_worker:
                self.incoming_worker.relocate_tree(event.src_path, event.dest_path)
                self.incoming_worker.submit_tree(event.dest_path)
            if event.is_directory:
                return
            if self._is_path_offline(event.src_path) or self._is_path_offline(event.dest_path):
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

            src_is_temp = is_temporary_download(event.src_path)
            dest_is_temp = is_temporary_download(event.dest_path)
            dest_is_video = Path(event.dest_path).suffix.lower() in VIDEO_EXTENSIONS
            dest_is_companion = Path(event.dest_path).suffix.lower() in COMPANION_EXTENSIONS

            # Transitions between temporary download stages must never be treated as problem moves or companion moves
            if src_is_temp and dest_is_temp:
                return

            incoming_candidate = bool(self.incoming_worker and self.incoming_worker.submit(event.dest_path))
            if incoming_candidate and not (Path(event.src_path).suffix.lower() in VIDEO_EXTENSIONS and not src_is_temp):
                return

            candidate_source = None
            if (getattr(self.worker, "transcoder_compatibility", False) is True
                    and dest_is_video):
                remove_transcoder_candidate(self.database_path, event.src_path)
                candidate_source = register_transcoder_candidate(self.database_path, event.dest_path)
            if candidate_source:
                record_filesystem_event(
                    self.database_path, "created", event.dest_path,
                    is_directory=False, initial_status="waiting"
                )
                return

            # A temporary download completing outside incoming folders is a newly created video, not an inventory move
            if src_is_temp and dest_is_video:
                record_filesystem_event(self.database_path, "created", event.dest_path, is_directory=False, initial_status="pending")
                return

            if dest_is_video or dest_is_companion:
                record_filesystem_event(self.database_path, "moved", event.src_path, event.dest_path, event.is_directory, initial_status="pending")
                if dest_is_video:
                    self.worker.submit(event.src_path, event.dest_path)
                elif dest_is_companion:
                    record_activity(self.database_path, "companion", "external companion move", "recorded",
                                    old_path=event.src_path, new_path=event.dest_path,
                                    detail="Companion move recorded; Stash does not maintain a separate path for this file")


def update_status(database_path, token, pid, state, roots, unavailable):
    try:
        connection = connect(database_path)
        try:
            connection.execute(
                """UPDATE filesystem_monitor_status SET token=?,pid=?,state=?,started_at=COALESCE(started_at,?),
                       heartbeat_at=?,roots_json=?,unavailable_roots_json=? WHERE id=1 AND token=?""",
                (token, pid, state, utc_now(), utc_now(), json.dumps(roots), json.dumps(unavailable), token),
            )
            connection.commit()
        finally:
            connection.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        logger.debug("update_status deferred (DB busy): %s", err)


def claim_monitor_ownership(database_path, token, pid, roots, unavailable):
    """Atomically ensure only one live monitor can own this database."""
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT token,pid,state FROM filesystem_monitor_status WHERE id=1"
        ).fetchone()
        if current and current["token"] != token and current["state"] in ("starting", "running"):
            existing_pid = int(current["pid"] or 0)
            if existing_pid:
                try:
                    os.kill(existing_pid, 0)
                    connection.rollback()
                    return False
                except PermissionError:
                    connection.rollback()
                    return False
                except OSError:
                    pass
        connection.execute(
            """UPDATE filesystem_monitor_status SET token=?,pid=?,state='starting',started_at=?,
                   heartbeat_at=?,roots_json=?,unavailable_roots_json=? WHERE id=1""",
            (token, pid, utc_now(), utc_now(), json.dumps(roots), json.dumps(unavailable)),
        )
        connection.commit()
        return True
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
    available, unavailable = probe_roots_startup(roots, timeout=2.0, max_workers=4)
    if not claim_monitor_ownership(database_path, args.token, os.getpid(), available, unavailable):
        logger.warning("Another Watchtower monitor already owns this database; exiting duplicate startup")
        return
    runtime_path = Path(args.runtime)
    loaded_code_signature = monitor_code_signature()
    runtime = {}
    if runtime_path.is_file():
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("Failed to read runtime config from %s: %s", runtime_path, exc)
    stash = StashInterface(runtime["server_connection"])
    worker = MoveWorker(database_path, stash, runtime.get("automatic_move_reconciliation") is True,
                        runtime.get("mac_notifications") is True,
                        runtime.get("transcoder_replacement_compatibility") is True)
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
    incoming_worker.allow_custom_contact_sheet_script = runtime.get("allow_custom_contact_sheet_script") is True
    observer = Observer()
    handler = LibraryEventHandler(database_path, worker, runtime.get("mac_notifications") is True, incoming_worker)
    watched_roots = {}
    for root in available:
        try:
            watch = observer.schedule(handler, root, recursive=True)
            watched_roots[root] = watch
        except Exception as exc:
            logger.warning("Failed scheduling observer on %s: %s", root, exc)
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
    if worker.transcoder_compatibility:
        handler.restore_transcoder_candidates()
    tracker = RootAvailabilityTracker(roots, probe_timeout=5.0)
    handler.availability_tracker = tracker
    if incoming_worker:
        incoming_worker.availability_tracker = tracker
    for r in unavailable:
        if r in tracker._states:
            tracker._states[r]["status"] = "unavailable"
    try:
        while True:
            available, unavailable, recovered, lost = tracker.poll()

            for root in lost:
                logger.warning("Library root became unavailable: %s", root)
                record_activity(database_path, "monitor", "library root unavailable", "warning",
                                severity="warning", old_path=root,
                                detail="Network share disconnected or became unresponsive")
                notify(runtime.get("mac_notifications") is True, f"Library root unavailable: {Path(root).name}")
                watch = watched_roots.pop(root, None)
                if watch:
                    try:
                        observer.unschedule(watch)
                    except Exception:
                        pass

            for root in recovered:
                logger.info("Library root reconnected: %s", root)
                record_activity(database_path, "monitor", "library root recovered", "running",
                                severity="info", new_path=root,
                                detail="Network share reconnected; monitoring resumed")
                notify(runtime.get("mac_notifications") is True, f"Library root reconnected: {Path(root).name}")
                if root not in watched_roots:
                    try:
                        watch = observer.schedule(handler, root, recursive=True)
                        watched_roots[root] = watch
                    except Exception as exc:
                        logger.warning("Failed scheduling observer on reconnected root %s: %s", root, exc)
                if incoming_worker:
                    incoming_worker.trigger_recovery_scan(root, max_wait=0)

            reload_monitor_if_code_changed(
                loaded_code_signature, runtime_path, runtime, worker,
                incoming_worker, database_path
            )
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
                            worker.transcoder_compatibility = bool(new_cfg.get("transcoder_replacement_compatibility", worker.transcoder_compatibility))
                            if "mac_notifications" in new_cfg:
                                new_notif = bool(new_cfg["mac_notifications"])
                                worker.notifications = new_notif
                                if incoming_worker:
                                    incoming_worker.notifications = new_notif
                                if handler:
                                    handler.notifications = new_notif
                            else:
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
                            incoming_worker.allow_custom_contact_sheet_script = bool(new_cfg.get(
                                "allow_custom_contact_sheet_script", incoming_worker.allow_custom_contact_sheet_script
                            ))
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
