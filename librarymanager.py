#!/usr/bin/env python3
"""Stash entry point for the read-only Library Manager inventory."""

import base64
import json
import csv
import logging
import plistlib
from logging.handlers import RotatingFileHandler
import os
import subprocess
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from stashapi.stashapp import StashInterface

from librarymanager_core import (
    generate_video_contact_sheet, expect_filesystem_create, apply_manual_filename, apply_scene_filename, build_merge_preview, build_resolution_plan,
                                 claim_due_rename, connect, enqueue_rename, fail_queued_rename,
                                 finish_queued_rename, inventory, preview_safe_filenames,
                                 preview_manual_filename, preview_scene_filename, reconcile_missing_files, refresh_scene_inventory,
                                 release_worker_schedule, scene_naming_signature, filesystem_monitor_summary,
                                 reconcile_filesystem_events, pending_filesystem_events,
                                 pending_transcoder_candidates, promote_transcoder_candidate, utc_now,
                                 find_duplicate_scene_file, inspect_backlog_duplicate,
                                 get_file_stat_snapshot, expect_filesystem_delete, resolve_filesystem_event,
                                 cancel_expected_filesystem_delete, _is_pid_alive)
from librarymanager_core import dashboard_data, incoming_summary, annotate_pending_events_processing_state, record_activity, record_monitor_lifecycle, recent_activity, cancel_pending_rename, make_pending_rename_due, snapshot_incoming_baseline, evaluate_filing_proposal, apply_filing_proposal, ignore_filing_proposal, get_pending_filing_proposals, recover_filing_proposal, invalidate_stale_filing_proposals, get_configured_filing_destination_roots, get_filing_folder_mappings, save_filing_folder_mapping, delete_filing_folder_mapping, invalidate_destination_dir_cache, refresh_destination_dir_cache, process_incoming_file_now, retry_filing_proposal, get_backlog_items, evaluate_backlog_batch, acknowledge_backlog_missing, prune_resolved_filing_baseline, invalidate_incoming_discovery_cache


from librarymanager_reconciliation import (dismiss_review_batch, execute_grouped_move_reconciliation,
                                           list_review_batches)

QUERY = """
query LibraryManagerInventory($filter: FindFilterType) {
  findScenes(filter: $filter) {
    count
    scenes {
      id title details date director code rating100 organized urls
      studio { name }
      performers { id name }
      tags { id name }
      galleries { id title }
      stash_ids { endpoint stash_id }
      groups { group { id name } scene_index }
      files { id path basename size duration width height video_codec fingerprints { type value } }
    }
  }
}
"""

SCENE_QUERY = """
query LibraryManagerScene($id: ID!) {
  findScene(id: $id) {
    id title details date director code rating100 organized urls
    studio { name }
    performers { id name }
    tags { id name }
    galleries { id title }
    stash_ids { endpoint stash_id }
    groups { group { id name } scene_index }
    files { id path basename size duration width height video_codec fingerprints { type value } }
  }
}
"""

PERFORMER_SCENES_QUERY = """
query LibraryManagerPerformerScenes($id: ID!) {
  findScenes(scene_filter: { performers: { value: [$id], modifier: INCLUDES } }, filter: { per_page: -1 }) {
    scenes { id }
  }
}
"""

STUDIO_SCENES_QUERY = """
query LibraryManagerStudioScenes($id: ID!) {
  findScenes(scene_filter: { studios: { value: [$id], modifier: INCLUDES } }, filter: { per_page: -1 }) {
    scenes { id }
  }
}
"""

ROOTS_QUERY = "query LibraryManagerRoots { configuration { general { stashes { path } } } }"
SCENE_COUNT_QUERY = "query LibraryManagerSceneCount { findScenes(filter: {per_page: 1}) { count } }"
DELETE_FILES_MUTATION = """
mutation LibraryManagerDeleteFiles($ids: [ID!]!) {
  deleteFiles(ids: $ids)
}
"""


def current_scene_count(stash):
    result = stash.call_GQL(SCENE_COUNT_QUERY)
    return int(((result or {}).get("findScenes") or {}).get("count") or 0)


def activity_logger():
    logger = logging.getLogger("librarymanager.activity")
    if not logger.handlers:
        handler = RotatingFileHandler(Path(__file__).with_name("librarymanager.log"), maxBytes=5_000_000,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def audit(database_path, category, action, status, **fields):
    if "details" in fields and "detail" not in fields:
        fields["detail"] = str(fields.pop("details"))
    elif "details" in fields:
        fields.pop("details")
    record_activity(database_path, category, action, status, **fields)
    activity_logger().log(logging.ERROR if fields.get("severity") == "error" else logging.INFO,
                          "%s %s %s scene=%s file=%s %s", category, action, status,
                          fields.get("scene_id") or "-", fields.get("file_id") or "-", fields.get("detail") or "")


def send_system_notification(title, message):
    """Send desktop notification natively on macOS, Windows, or Linux."""
    try:
        if sys.platform == "darwin":
            result = subprocess.run([
                "/usr/bin/osascript", "-e", "on run argv", "-e",
                "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e", "end run", "--", str(message), str(title),
            ], capture_output=True, text=True, timeout=10, check=False)
            if result.returncode:
                activity_logger().debug("osascript notification returned %s: %s", result.returncode, result.stderr)
        elif sys.platform == "win32":
            title_b64 = base64.b64encode(str(title).encode("utf-8")).decode("ascii")
            message_b64 = base64.b64encode(str(message).encode("utf-8")).decode("ascii")
            ps_cmd = (
                f'[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; '
                f'$title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("{title_b64}")); '
                f'$message = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("{message_b64}")); '
                f'$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); '
                f'$textNodes = $template.GetElementsByTagName("text"); '
                f'$textNodes.Item(0).AppendChild($template.CreateTextNode($title)) > $null; '
                f'$textNodes.Item(1).AppendChild($template.CreateTextNode($message)) > $null; '
                f'$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Stash Library Manager"); '
                f'$notification = [Windows.UI.Notifications.ToastNotification]::new($template); '
                f'$notifier.Show($notification)'
            )
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                           capture_output=True, text=True, timeout=10, check=False)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "Stash Library Manager", str(title), str(message)],
                           capture_output=True, text=True, timeout=5, check=False)
    except Exception as exc:
        activity_logger().debug("Desktop notification failed: %s", exc)


send_macos_notification = send_system_notification


def maybe_notify(config, message, *, success=False):
    if not (config or {}).get("macNotifications"):
        return
    if success and not (config or {}).get("notifySuccessfulRenames"):
        return
    send_system_notification("Stash Library Manager", message)


def contact_sheet_scope_name(configured_folders):
    """Return a concise label for one or more configured incoming folders."""
    return ", ".join(Path(path).name or str(path) for path in configured_folders)


def assert_scene_removal_safe(scene, deleted_path):
    """Refuse removal unless one missing path is the scene's only attached file."""
    scene_files = scene.get("files") if isinstance(scene, dict) else None
    if not scene_files:
        raise ValueError("Watchtower could not verify this scene's current files, so it was not removed")
    normalized_deleted_path = os.path.normcase(os.path.realpath(deleted_path))
    if Path(deleted_path).exists():
        raise ValueError("The video file exists again, so Watchtower will not remove its Stash scene")
    normalized_scene_paths = []
    for scene_file in scene_files:
        scene_path = str((scene_file or {}).get("path") or "")
        if not scene_path:
            raise ValueError("Watchtower could not verify this scene's current files, so it was not removed")
        normalized_scene_paths.append(os.path.normcase(os.path.realpath(scene_path)))
    if normalized_deleted_path not in normalized_scene_paths:
        raise ValueError("The deleted path is no longer attached to this scene, so Watchtower made no change")
    if len(normalized_scene_paths) != 1:
        raise ValueError(
            "This scene has another attached video file, so Watchtower will not remove the scene. "
            "Review multi-file scenes directly in Stash"
        )


def inventory_progress_path(database_path):
    return Path(database_path).with_name(f"{Path(database_path).name}.inventory-progress.json")


def write_inventory_progress(database_path, status, processed=0, total=0, detail=""):
    """Atomically publish inventory progress for the onboarding UI."""
    progress_path = inventory_progress_path(database_path)
    temporary_path = progress_path.with_name(f"{progress_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = {
        "status": str(status),
        "processed": max(0, int(processed or 0)),
        "total": max(0, int(total or 0)),
        "detail": str(detail or ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary_path.replace(progress_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return payload


def read_inventory_progress(database_path):
    progress_path = inventory_progress_path(database_path)
    if not progress_path.is_file():
        return {"status": "idle", "processed": 0, "total": 0, "detail": ""}
    try:
        payload = json.loads(progress_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"status": "idle", "processed": 0, "total": 0, "detail": ""}
    except (OSError, ValueError):
        return {"status": "unknown", "processed": 0, "total": 0,
                "detail": "Progress is temporarily unavailable"}


def require_bulk_dismissal(resolution):
    """Bulk review is acknowledgement-only; corrective actions require per-item checks."""
    if str(resolution or "dismiss") != "dismiss":
        raise ValueError("Bulk review can only dismiss changes; resolve corrective actions one item at a time")


def validate_filesystem_scan_action(database_path: Path, event: dict, resolution: str):
    """Fail closed before any Stash scan for duplicate or external-move review."""
    destination = event.get("destination_path") or event.get("source_path")
    if not destination or not Path(destination).is_file():
        raise ValueError("The destination file no longer exists, so Stash cannot scan it")

    evidence = find_duplicate_scene_file(database_path, destination, allow_compute=False)
    candidates = (evidence or {}).get("all_candidates") or ([evidence] if evidence else [])
    if any(candidate and candidate.get("is_external_move") for candidate in candidates):
        raise ValueError(
            "External moves are review-only until Watchtower has a verified relinking workflow; no Stash scan was started"
        )
    if evidence and evidence.get("checksum_status") != "verified":
        raise ValueError("Checksum verification is still pending; no Stash scan was started")
    if resolution == "keep_both" and not evidence:
        raise ValueError("The additional file is no longer a verified duplicate; no Stash scan was started")
    return destination, evidence


def delete_verified_backlog_duplicate(
    database_path: Path,
    stash,
    candidate_path: str,
    config: dict,
    expected_scene_id: str,
    expected_file_id: str,
    expected_retained_file_id: str,
    expected_sha256: str,
    selected_companions=None,
) -> dict:
    """Delete one explicitly selected, fully verified duplicate through Stash."""
    info = inspect_backlog_duplicate(
        database_path, stash, candidate_path, config, request_verification=False
    )
    if not info or info.get("checksum_status") != "verified":
        raise ValueError("The files are not currently verified as exact SHA-256 duplicates")
    expected = {
        "scene_id": str(expected_scene_id or ""),
        "candidate_file_id": str(expected_file_id or ""),
        "retained_file_id": str(expected_retained_file_id or ""),
        "sha256": str(expected_sha256 or "").lower(),
    }
    actual = {
        "scene_id": str(info.get("scene_id") or ""),
        "candidate_file_id": str(info.get("candidate_file_id") or ""),
        "retained_file_id": str(info.get("retained_file_id") or ""),
        "sha256": str(info.get("sha256") or "").lower(),
    }
    if expected != actual:
        raise ValueError("The scene, file ownership, or checksum changed; deletion was blocked")

    available_companions = {item["path"]: item for item in (info.get("companions") or [])}
    selected = selected_companions or []
    selected_paths = set()
    for requested in selected:
        requested_path = str((requested or {}).get("path") or "")
        current = available_companions.get(requested_path)
        if not current:
            raise ValueError("A selected companion is no longer an exact companion inside the incoming folder")
        expected_snapshot = tuple(int((requested or {}).get(key, -1)) for key in ("size", "mtime_ns", "device", "inode"))
        current_snapshot = tuple(int(current.get(key, -2)) for key in ("size", "mtime_ns", "device", "inode"))
        if expected_snapshot != current_snapshot:
            raise ValueError(f"Companion changed after confirmation: {Path(requested_path).name}")
        selected_paths.add(requested_path)

    now = utc_now()
    connection = connect(database_path)
    try:
        connection.execute(
            """INSERT INTO duplicate_file_repairs(
                   candidate_path,candidate_file_id,scene_id,retained_file_id,retained_path,
                   sha256,deleted_companions_json,status,detail,created_at,completed_at
               ) VALUES (?,?,?,?,?,?,'[]','processing',?,?,NULL)
               ON CONFLICT(candidate_path) DO UPDATE SET
                   candidate_file_id=excluded.candidate_file_id,scene_id=excluded.scene_id,
                   retained_file_id=excluded.retained_file_id,retained_path=excluded.retained_path,
                   sha256=excluded.sha256,deleted_companions_json='[]',status='processing',
                   detail=excluded.detail,created_at=excluded.created_at,completed_at=NULL""",
            (candidate_path, actual["candidate_file_id"], actual["scene_id"],
             actual["retained_file_id"], info["retained_path"], actual["sha256"],
             "Awaiting Stash file deletion", now),
        )
        connection.commit()
    finally:
        connection.close()

    try:
        expect_filesystem_delete(database_path, candidate_path)
        response = stash.call_GQL(DELETE_FILES_MUTATION, {"ids": [actual["candidate_file_id"]]})
        if not (response or {}).get("deleteFiles"):
            raise RuntimeError("Stash did not confirm deletion of the selected file")
        if Path(candidate_path).exists():
            raise RuntimeError("Stash returned success but the selected file still exists on disk")
        post_scene = stash.find_scene(int(actual["scene_id"]))
        post_files = {
            str(item.get("id")): item
            for item in ((post_scene or {}).get("files") or [])
            if item and item.get("id") is not None
        }
        retained_after = post_files.get(actual["retained_file_id"])
        if not retained_after or os.path.normcase(os.path.realpath(retained_after.get("path") or "")) != os.path.normcase(os.path.realpath(info["retained_path"])):
            raise RuntimeError("Stash did not retain the verified organised file on the scene")
        if actual["candidate_file_id"] in post_files:
            raise RuntimeError("Stash still reports the deleted incoming file on the scene")
        # Stash may implement deletion as an atomic rename to a temporary .delete path.
        # If that watcher event raced with this operation, it belongs to this verified
        # repair and must not be presented as an unexplained scene deletion.
        resolve_filesystem_event(database_path, "deleted", candidate_path)
    except Exception as exc:
        cancel_expected_filesystem_delete(database_path, candidate_path)
        connection = connect(database_path)
        try:
            connection.execute(
                "UPDATE duplicate_file_repairs SET status='failed',detail=? WHERE candidate_path=?",
                (str(exc), candidate_path),
            )
            connection.commit()
        finally:
            connection.close()
        raise

    deleted_companions = []
    companion_errors = []
    for companion_path in sorted(selected_paths):
        companion = available_companions[companion_path]
        expected_snapshot = tuple(int(companion[key]) for key in ("size", "mtime_ns", "device", "inode"))
        if get_file_stat_snapshot(companion_path) != expected_snapshot:
            companion_errors.append(f"{Path(companion_path).name}: changed or unavailable")
            continue
        try:
            expect_filesystem_delete(database_path, companion_path)
            Path(companion_path).unlink()
            deleted_companions.append(companion_path)
        except OSError as exc:
            cancel_expected_filesystem_delete(database_path, companion_path)
            companion_errors.append(f"{Path(companion_path).name}: {exc}")

    detail = f"Deleted verified duplicate {Path(candidate_path).name}; retained {info['retained_path']}"
    if deleted_companions:
        detail += f"; deleted {len(deleted_companions)} selected companion(s)"
    if companion_errors:
        detail += "; companion cleanup incomplete: " + "; ".join(companion_errors)

    connection = connect(database_path)
    try:
        connection.execute(
            """UPDATE duplicate_file_repairs
               SET status='completed',deleted_companions_json=?,detail=?,completed_at=?
               WHERE candidate_path=?""",
            (json.dumps(deleted_companions), detail, utc_now(), candidate_path),
        )
        connection.commit()
    finally:
        connection.close()
    audit(database_path, "duplicate", "exact duplicate deleted", "completed",
          scene_id=actual["scene_id"], file_id=actual["candidate_file_id"],
          old_path=candidate_path, new_path=info["retained_path"], detail=detail,
          metadata={"deleted_companions": deleted_companions, "companion_errors": companion_errors})
    try:
        prune_resolved_filing_baseline(database_path, config)
        invalidate_incoming_discovery_cache()
    except Exception:
        pass
    return {
        "success": True,
        "scene_id": actual["scene_id"],
        "deleted_path": candidate_path,
        "retained_path": info["retained_path"],
        "deleted_companions": deleted_companions,
        "companion_errors": companion_errors,
        "message": detail,
    }


def dashboard_reports():
    """Load bounded copies of the latest human-facing reports for the dashboard."""
    definitions = [
        ("renamed", "Renamed File Search", "reconciliation-report.json", "matches"),
        ("resolution", "Resolution Plan", "resolution-plan.json", "items"),
        ("metadata", "Metadata Merge Preview", "metadata-merge-preview.json", "previews"),
        ("filenames", "Filename Preview", "filename-preview.json", "files"),
        ("filesystem", "Filesystem Event Review", "filesystem-reconciliation-report.json", "proposals"),
        ("activity", "Activity Export", "activity-report.json", "activity"),
    ]
    reports = []
    for report_id, title, filename, rows_key in definitions:
        path = Path(__file__).with_name(filename)
        if not path.exists():
            reports.append({"id": report_id, "title": title, "available": False, "filename": filename})
            continue
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            summary = content.get("summary") if isinstance(content, dict) else None
            if not isinstance(summary, dict):
                summary = {key: value for key, value in (content.items() if isinstance(content, dict) else [])
                           if key not in (rows_key, "action_performed") and not isinstance(value, (list, dict))}
            rows = content.get(rows_key, []) if isinstance(content, dict) else []
            reports.append({"id": report_id, "title": title, "available": True, "filename": filename,
                            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(path.stat().st_mtime)),
                            "summary": summary, "row_count": len(rows) if isinstance(rows, list) else 0,
                            "rows": rows[:100] if isinstance(rows, list) else []})
        except (OSError, ValueError) as error:
            reports.append({"id": report_id, "title": title, "available": False, "filename": filename,
                            "error": str(error)})
    return reports


def load_input():
    return json.load(sys.stdin)


def fetch_scenes(stash, page_size=250):
    scenes = []
    page = 1
    while True:
        result = stash.call_GQL(QUERY, {"filter": {"page": page, "per_page": page_size}})
        found = (result or {}).get("findScenes") or {}
        batch = found.get("scenes") or []
        scenes.extend(batch)
        if not batch or len(scenes) >= int(found.get("count") or 0):
            return scenes
        page += 1


def refresh_scene(stash, database_path, scene_id):
    result = stash.call_GQL(SCENE_QUERY, {"id": str(scene_id)})
    scene = (result or {}).get("findScene")
    if not scene:
        raise ValueError(f"Scene {scene_id} was not found in Stash")
    refresh_scene_inventory(database_path, scene)


def recover_local_rename_cache(stash, database_path, scene_id, result):
    """Re-read Stash after a confirmed rename whose local cache update failed."""
    if not result.get("action_performed") or result.get("local_cache_updated") is not False:
        return result
    try:
        refresh_scene(stash, database_path, scene_id)
        return {**result, "status": "renamed", "local_cache_updated": True,
                "reason": "Stash confirmed the rename and Watchtower refreshed its local record"}
    except Exception as recovery_error:
        return {**result, "status": "renamed_with_warning", "local_cache_updated": False,
                "reason": f"{result.get('reason', 'The local record needs refreshing')}; recovery failed: {recovery_error}"}


def automatic_scene_allowed(config, scene_id):
    """A configured test scene acts as a hard scope lock for automatic hooks."""
    test_scene_id = str((config or {}).get("testSceneId") or "").strip()
    return not test_scene_id or test_scene_id == str(scene_id)


SCENE_NAMING_HOOK_FIELDS = frozenset({"title", "studio_id", "performer_ids", "code", "date"})


def scene_hook_has_naming_changes(changed):
    """Only explicit naming-metadata edits may trigger an automatic filename change.

    Filesystem reconciliation can cause Stash to emit Scene.Update.Post hooks with an
    empty input payload. Treating those as metadata edits would undo a user's manual
    external filename change immediately after Watchtower reconciles it.
    """
    if not isinstance(changed, dict) or not changed:
        return False
    return bool(SCENE_NAMING_HOOK_FIELDS.intersection(changed))


def fetch_library_roots(stash):
    result = stash.call_GQL(ROOTS_QUERY)
    stashes = (((result or {}).get("configuration") or {}).get("general") or {}).get("stashes") or []
    return sorted({str(item.get("path")).strip() for item in stashes if item.get("path")})


def filing_config_with_library_roots(stash, config=None):
    """Attach Stash roots for Automatic Filing without persisting derived paths."""
    effective = dict(config or stash.find_plugin_config("librarymanager") or {})
    effective["_libraryRoots"] = fetch_library_roots(stash)
    return effective


def get_configured_incoming_folders(config):
    raw_folders = (config or {}).get("incomingFolders")
    if isinstance(raw_folders, list):
        cleaned = [str(f).strip() for f in raw_folders if str(f).strip()]
        return cleaned[:5]
    legacy = str((config or {}).get("incomingFolder") or "").strip()
    return [legacy] if legacy else []


def incoming_folder_status_item(folder_path_str, roots, enabled):
    if not folder_path_str:
        return {"enabled": enabled, "valid": False, "exists": False, "path": "", "reason": "Choose an incoming folder first"}
    try:
        folder = Path(folder_path_str).expanduser().resolve()
    except Exception as exc:
        return {"enabled": enabled, "valid": False, "exists": False, "path": folder_path_str, "reason": f"Invalid path: {exc}"}
    if not folder.is_dir():
        return {"enabled": enabled, "valid": False, "exists": False, "path": str(folder), "reason": "The incoming folder is not currently available on disk"}
    inside_root = False
    for root in roots:
        try:
            folder.relative_to(Path(root).expanduser().resolve())
            inside_root = True
            break
        except (ValueError, OSError):
            continue
    if not inside_root:
        return {"enabled": enabled, "valid": False, "exists": True, "path": str(folder),
                "reason": "The incoming folder must be inside a folder configured in Stash"}
    return {"enabled": enabled, "valid": True, "exists": True, "path": str(folder), "reason": "Ready to watch for completed videos"}


def incoming_folders_status(config, roots):
    enabled = (config or {}).get("automaticIncomingScan") is True
    folders = get_configured_incoming_folders(config)
    if not folders:
        return {
            "enabled": enabled,
            "folders": [incoming_folder_status_item("", roots, enabled)],
            "valid_count": 0,
            "total_count": 0,
            "all_valid": False,
        }
    items = [incoming_folder_status_item(f, roots, enabled) for f in folders]
    valid_count = sum(1 for item in items if item["valid"])
    return {
        "enabled": enabled,
        "folders": items,
        "valid_count": valid_count,
        "total_count": len(items),
        "all_valid": len(items) > 0 and valid_count == len(items),
    }


def incoming_folder_status(config, roots):
    multi = incoming_folders_status(config, roots)
    folders = multi.get("folders", [])
    for item in folders:
        if item["valid"]:
            return item
    return folders[0] if folders else {"enabled": multi["enabled"], "valid": False, "path": "", "reason": "Choose an incoming folder first"}


def system_startup_paths():
    runtime_path = Path(__file__).with_name("startup-runtime.json")
    if sys.platform == "darwin":
        label = "com.stash.librarymanager"
        return "macOS", Path.home() / "Library" / "LaunchAgents" / f"{label}.plist", runtime_path
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        startup_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        return "Windows", startup_dir / "stash-librarymanager-startup.vbs", runtime_path
    else:  # Linux / Unix
        autostart_dir = Path.home() / ".config" / "autostart"
        return "Linux", autostart_dir / "stash-librarymanager.desktop", runtime_path


def system_startup_status():
    label, runner_path, runtime_path = system_startup_paths()
    return {
        "supported": True,
        "enabled": runner_path.is_file() and runtime_path.is_file(),
        "platform_label": label,
        "path": str(runner_path),
        "plist_path": str(runner_path)
    }


macos_startup_paths = system_startup_paths
macos_startup_status = system_startup_status


def configure_system_startup(enabled, server_connection, database_path):
    label, runner_path, runtime_path = system_startup_paths()

    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        subprocess.run(["/bin/launchctl", "bootout", domain, str(runner_path)], capture_output=True, check=False)
        if not enabled:
            runner_path.unlink(missing_ok=True)
            runtime_path.unlink(missing_ok=True)
            return system_startup_status()
        runner_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps({"server_connection": server_connection or {},
                                            "database": str(database_path)}), encoding="utf-8")
        try:
            runtime_path.chmod(0o600)
        except OSError:
            pass
        plist = {
            "Label": "com.stash.librarymanager",
            "ProgramArguments": [sys.executable, str(Path(__file__).with_name("librarymanager_startup.py")),
                                 "--runtime", str(runtime_path)],
            "RunAtLoad": True,
            "StartInterval": 60,
            "StandardOutPath": str(Path(__file__).with_name("librarymanager-startup.log")),
            "StandardErrorPath": str(Path(__file__).with_name("librarymanager-startup.log")),
        }
        with runner_path.open("wb") as handle:
            plistlib.dump(plist, handle)
        try:
            subprocess.run(["/bin/launchctl", "bootstrap", domain, str(runner_path)], capture_output=True, check=True)
        except subprocess.CalledProcessError as exc:
            # Exit code 36 (EBUSY) means the service is already loaded — harmless on double-click.
            if exc.returncode != 36:
                raise
        return system_startup_status()

    elif sys.platform == "win32":
        if not enabled:
            runner_path.unlink(missing_ok=True)
            runtime_path.unlink(missing_ok=True)
            return system_startup_status()
        runner_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps({"server_connection": server_connection or {},
                                            "database": str(database_path)}), encoding="utf-8")
        runtime_path.chmod(0o600)
        py_exec = sys.executable
        if py_exec.lower().endswith("python.exe"):
            pyw_exec = py_exec[:-10] + "pythonw.exe"
            if Path(pyw_exec).exists():
                py_exec = pyw_exec
        script_path = str(Path(__file__).with_name("librarymanager_startup.py"))
        escaped_cmd = f'"{py_exec}" "{script_path}" --runtime "{runtime_path}"'.replace('"', '""')
        vbs_script = 'Set WshShell = CreateObject("WScript.Shell")\r\nWshShell.Run "' + escaped_cmd + '", 0, False\r\n'
        runner_path.write_text(vbs_script, encoding="utf-8")
        return system_startup_status()

    else:  # Linux / Unix
        if not enabled:
            runner_path.unlink(missing_ok=True)
            runtime_path.unlink(missing_ok=True)
            return system_startup_status()
        runner_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps({"server_connection": server_connection or {},
                                            "database": str(database_path)}), encoding="utf-8")
        runtime_path.chmod(0o600)
        script_path = str(Path(__file__).with_name("librarymanager_startup.py"))
        desktop_entry = "\n".join([
            "[Desktop Entry]",
            "Type=Application",
            "Name=Watchtower Stash Monitor",
            f'Exec="{sys.executable}" "{script_path}" --runtime "{runtime_path}"',
            "Hidden=false",
            "NoDisplay=true",
            "X-GNOME-Autostart-enabled=true"
        ]) + "\n"
        runner_path.write_text(desktop_entry, encoding="utf-8")
        return system_startup_status()


configure_macos_startup = configure_system_startup


def monitor_process_launch_options(platform=None):
    """Return platform-specific options that isolate the long-running monitor.

    ``start_new_session`` only provides the required isolation on POSIX.  A
    native Windows child otherwise remains attached to Stash's console and can
    receive the console control event used to finish a plugin operation.
    """
    platform = platform or sys.platform
    if platform == "win32":
        detached_process = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": detached_process | new_process_group}
    return {"start_new_session": True}


def start_filesystem_monitor(stash, database_path, server_connection=None):
    current = filesystem_monitor_summary(database_path)
    if current.get("state") in ("running", "starting") and current.get("pid"):
        if _is_pid_alive(int(current["pid"])):
            return {**current, "message": "Filesystem monitor is already running or starting"}
    roots = fetch_library_roots(stash)
    if not roots:
        raise ValueError("Stash has no configured library roots")
    token = uuid.uuid4().hex
    control_path = Path(__file__).with_name("monitor-control.json")
    if control_path.exists():
        control_path.unlink()
    log_path = Path(__file__).with_name("librarymanager-monitor.log")
    config = stash.find_plugin_config("librarymanager") or {}
    incoming = incoming_folder_status(config, roots)
    incoming_multi = incoming_folders_status(config, roots)
    valid_incoming_paths = [item["path"] for item in incoming_multi["folders"] if item["valid"]]
    if incoming_multi["enabled"] and not valid_incoming_paths and incoming_multi["folders"]:
        first_bad = incoming_multi["folders"][0]
        audit(database_path, "incoming", "incoming folder", "disabled", severity="warning",
              old_path=first_bad["path"] or None, detail=first_bad["reason"])
    runtime_path = Path(__file__).with_name("monitor-runtime.json")
    runtime_path.write_text(json.dumps({
        "server_connection": server_connection or {},
        "automatic_move_reconciliation": config.get("automaticMoveReconciliation") is True,
        "transcoder_replacement_compatibility": config.get("transcoderReplacementCompatibility") is True,
        "mac_notifications": config.get("macNotifications") is True,
        "incoming_imports": incoming_multi["enabled"] and len(valid_incoming_paths) > 0,
        "incoming_folder": valid_incoming_paths[0] if valid_incoming_paths else "",
        "incoming_folders": valid_incoming_paths,
        "incoming_settle_seconds": max(60, int(config.get("incomingSettleMinutes") or 5) * 60),
        "incoming_fallback_seconds": 60,
        "generate_contact_sheets": config.get("generateContactSheets") is True,
        "contact_sheet_grid": config.get("contactSheetGrid") or "4x4",
        "contact_sheet_banner": config.get("contactSheetBanner") is not False,
        "contact_sheet_adjust_vertical": config.get("contactSheetAdjustVertical") is not False,
        "contact_sheet_script": config.get("contactSheetScript") or "",
        "allow_custom_contact_sheet_script": config.get("allowCustomContactSheetScript") is True,
        "library_roots": roots,
    }), encoding="utf-8")
    runtime_path.chmod(0o600)
    log_handle = open(log_path, "ab", buffering=0)
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("librarymanager_monitor.py")),
             "--database", str(database_path), "--control", str(control_path),
             "--token", token, "--roots-json", json.dumps(roots), "--runtime", str(runtime_path)],
            stdin=subprocess.DEVNULL, stdout=log_handle, stderr=log_handle,
            close_fds=True, **monitor_process_launch_options(),
        )
    finally:
        log_handle.close()
    # StashInterface initialization can take several seconds on a busy or newly
    # upgraded Stash instance. Do not report a failed restart while the child
    # is still starting successfully in the background.
    for _ in range(100):
        time.sleep(0.1)
        status = filesystem_monitor_summary(database_path)
        if status.get("state") == "running":
            return {**status, "message": "Read-only filesystem monitor started"}
        if process.poll() is not None:
            raise RuntimeError(
                f"Filesystem monitor exited during startup (code {process.returncode}); inspect {log_path}"
            )
    raise RuntimeError(f"Filesystem monitor did not start; inspect {log_path}")


def reload_monitor_runtime(stash, database_path):
    status = filesystem_monitor_summary(database_path)
    if status.get("state") != "running" or not status.get("token"):
        return False
    roots = fetch_library_roots(stash)
    config = stash.find_plugin_config("librarymanager") or {}
    incoming = incoming_folder_status(config, roots)
    incoming_multi = incoming_folders_status(config, roots)
    valid_incoming_paths = [item["path"] for item in incoming_multi["folders"] if item["valid"]]
    control_path = Path(__file__).with_name("monitor-control.json")
    try:
        control_path.write_text(json.dumps({
            "action": "reload",
            "token": status["token"],
            "config": {
                "automatic_move_reconciliation": config.get("automaticMoveReconciliation") is True,
                "mac_notifications": config.get("macNotifications") is True,
                "incoming_imports": incoming_multi["enabled"] and len(valid_incoming_paths) > 0,
                "incoming_folder": valid_incoming_paths[0] if valid_incoming_paths else "",
                "incoming_folders": valid_incoming_paths,
                "incoming_settle_seconds": max(60, int(config.get("incomingSettleMinutes") or 5) * 60),
                "library_roots": roots,
                "generate_contact_sheets": config.get("generateContactSheets") is True,
                "contact_sheet_grid": config.get("contactSheetGrid") or "4x4",
                "contact_sheet_banner": config.get("contactSheetBanner") is not False,
                "contact_sheet_adjust_vertical": config.get("contactSheetAdjustVertical") is not False,
                "contact_sheet_script": config.get("contactSheetScript") or "",
                "allow_custom_contact_sheet_script": config.get("allowCustomContactSheetScript") is True,
            }
        }), encoding="utf-8")
        # Poll until the daemon consumes the control file (it deletes it after processing).
        # This gives a lightweight acknowledgement without any schema changes.
        # Timeout 3s — one full 2s poll cycle plus margin. Non-fatal if it times out.
        for _ in range(30):
            time.sleep(0.1)
            if not control_path.exists():
                return True  # daemon consumed the file — reload acknowledged
        return True  # timed out but file was written; daemon will process it next cycle
    except Exception:
        return False


def stop_filesystem_monitor(database_path):
    status = filesystem_monitor_summary(database_path)
    if status.get("raw_state", status.get("state")) != "running" or not status.get("token"):
        return {**status, "message": "Filesystem monitor is already stopped"}
    if status.get("is_stale") and status.get("pid") and not status.get("pid_alive"):
        connection = connect(database_path)
        try:
            connection.execute("UPDATE filesystem_monitor_status SET state='stopped' WHERE id=1")
            connection.commit()
        finally:
            connection.close()
        record_monitor_lifecycle(
            database_path, "MONITOR STOPPED", "stopped",
            detail=f"MONITOR STOPPED — stale monitor process (PID {status.get('pid')}) reset to stopped",
            metadata={"pid": status.get("pid")}
        )
        return {**status, "state": "stopped", "raw_state": "stopped", "is_stale": False, "message": "Filesystem monitor was dead and is now reset to stopped"}
    control_path = Path(__file__).with_name("monitor-control.json")
    control_path.write_text(json.dumps({"action": "stop", "token": status["token"]}), encoding="utf-8")
    for _ in range(40):
        time.sleep(0.15)
        status = filesystem_monitor_summary(database_path)
        if status.get("state") == "stopped":
            connection = connect(database_path)
            try:
                last = connection.execute(
                    "SELECT action FROM activity_log WHERE category='monitor' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if not last or last["action"] != "MONITOR STOPPED":
                    record_monitor_lifecycle(
                        database_path, "MONITOR STOPPED", "stopped",
                        detail=f"MONITOR STOPPED — filesystem watcher stopped (PID {status.get('pid')})",
                        metadata={"pid": status.get("pid")}
                    )
            finally:
                connection.close()
            return {**status, "message": "Filesystem monitor stopped"}
    return {**status, "message": "Stop requested; monitor is still shutting down"}


def maybe_auto_restart_monitor(stash, database_path, server_connection=None, cooldown_seconds=300):
    """Make one rate-limited recovery attempt when an enabled watcher PID is gone."""
    config = stash.find_plugin_config("librarymanager") or {}
    status = filesystem_monitor_summary(database_path)
    if config.get("autoStartMonitor") is not True or not status.get("is_stale") or status.get("pid_alive"):
        return status

    now = time.time()
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT auto_restart_attempted_at FROM filesystem_monitor_status WHERE id=1"
        ).fetchone()
        last_attempt = float(row["auto_restart_attempted_at"] or 0) if row else 0
        if last_attempt and now - last_attempt < cooldown_seconds:
            connection.rollback()
            return status
        connection.execute(
            "UPDATE filesystem_monitor_status SET auto_restart_attempted_at=? WHERE id=1", (now,)
        )
        connection.commit()
    finally:
        connection.close()

    try:
        stop_filesystem_monitor(database_path)
        recovered = start_filesystem_monitor(stash, database_path, server_connection or {})
        connection = connect(database_path)
        try:
            connection.execute("UPDATE filesystem_monitor_status SET auto_restart_failures=0 WHERE id=1")
            connection.commit()
        finally:
            connection.close()
        audit(database_path, "monitor", "automatic restart", "running",
              detail=f"Watcher recovered automatically as process {recovered.get('pid')}")
        return recovered
    except Exception as error:
        connection = connect(database_path)
        try:
            connection.execute(
                "UPDATE filesystem_monitor_status SET auto_restart_failures=auto_restart_failures+1 WHERE id=1"
            )
            connection.commit()
        finally:
            connection.close()
        audit(database_path, "monitor", "automatic restart", "failed", severity="error",
              detail=f"Automatic watcher recovery failed; retry is paused for {int(cooldown_seconds)} seconds: {error}")
        return filesystem_monitor_summary(database_path)


def refresh_scene_contact_sheet(database_path: Path, video_path: str, scene_id: str, config: dict):
    """Regenerate contact sheet with updated title/metadata if CSM generation is enabled."""
    if not video_path:
        return None
    if config.get("generateContactSheets") is not True or config.get("refreshContactSheetsOnRename") is False:
        return None
    scope = config.get("contactSheetScope") or "all"
    if scope == "incoming":
        inc_str = config.get("incomingFolder") or ""
        if inc_str:
            try:
                Path(video_path).resolve().relative_to(Path(inc_str).resolve())
            except (OSError, ValueError):
                return None
        else:
            return None

    sheet_p = f"{video_path}.jpg"
    expect_filesystem_create(database_path, sheet_p)
    try:
        con = connect(database_path)
        con.execute(
            """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,size,modified_ns,stable_since,settle_seconds,status,attempts,detail)
               VALUES (?,?,?,?,?,?,?,?,0,?)
               ON CONFLICT(path) DO UPDATE SET status='generating_sheet', last_checked_at=excluded.last_checked_at, detail=excluded.detail""",
            (str(sheet_p), utc_now(), utc_now(), None, None, time.time(), 0, "generating_sheet", f"Creating contact sheet for scene {scene_id}")
        )
        con.commit()
        con.close()
    except Exception:
        pass

    try:
        csm_res = generate_video_contact_sheet(
            video_path,
            grid=config.get("contactSheetGrid") or "4x4",
            include_banner=config.get("contactSheetBanner") is not False,
            adjust_vertical=config.get("contactSheetAdjustVertical") is not False,
            custom_script=config.get("contactSheetScript") or "",
            allow_custom_script=config.get("allowCustomContactSheetScript") is True,
            overwrite=True
        )
        if csm_res.get("status") == "generated":
            sheet_p = csm_res.get("path") or sheet_p
            try:
                con = connect(database_path)
                con.execute(
                    """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,size,modified_ns,stable_since,settle_seconds,status,attempts,detail)
                       VALUES (?,?,?,?,?,?,?,?,0,?)
                       ON CONFLICT(path) DO UPDATE SET status='paired', last_checked_at=excluded.last_checked_at, detail=excluded.detail""",
                    (str(sheet_p), utc_now(), utc_now(), Path(sheet_p).stat().st_size if Path(sheet_p).exists() else None,
                     Path(sheet_p).stat().st_mtime_ns if Path(sheet_p).exists() else None, time.time(), 60, "paired",
                     f"Contact sheet regenerated for scene {scene_id}")
                )
                con.commit()
                con.close()
            except Exception:
                pass
            record_activity(
                database_path,
                "companion",
                "contact sheet updated",
                "recorded",
                scene_id=str(scene_id),
                new_path=sheet_p,
                detail="Contact sheet regenerated with new metadata for visual file browsing"
            )
            maybe_notify(
                config,
                f"Contact sheet created & paired: {Path(sheet_p).name} → Scene {scene_id}",
                success=True
            )
            return csm_res
        else:
            if csm_res.get("status") == "error":
                failure_reason = csm_res.get("error") or "Contact sheet generation failed"
                record_activity(
                    database_path, "companion", "contact sheet update", "failed",
                    severity="error", scene_id=str(scene_id), old_path=str(video_path), detail=failure_reason
                )
            try:
                con = connect(database_path)
                con.execute("DELETE FROM incoming_files WHERE path=?", (str(sheet_p),))
                con.commit()
                con.close()
            except Exception:
                pass
    except Exception as csm_err:
        try:
            con = connect(database_path)
            con.execute("DELETE FROM incoming_files WHERE path=?", (str(sheet_p),))
            con.commit()
            con.close()
        except Exception:
            pass
        activity_logger().error("Contact sheet update failed for %s: %s", video_path, csm_err)
        record_activity(
            database_path, "companion", "contact sheet update", "failed",
            severity="error", scene_id=str(scene_id), old_path=str(video_path), detail=str(csm_err)
        )
    return None


def process_rename_queue(stash, database_path):
    config = stash.find_plugin_config("librarymanager") or {}
    processed = skipped = failed = 0
    try:
        while True:
            scene_id, next_at, pending = claim_due_rename(database_path, time.time())
            if scene_id is None:
                if pending and next_at is not None:
                    time.sleep(max(0.05, min(2.0, next_at - time.time())))
                    continue
                break
            try:
                before = scene_naming_signature(database_path, scene_id, config.get("includeSceneDate") is True, config.get("includeVideoQuality") is True)
                refresh_scene(stash, database_path, scene_id)
                after = scene_naming_signature(database_path, scene_id, config.get("includeSceneDate") is True, config.get("includeVideoQuality") is True)
                if before == after:
                    preview = preview_scene_filename(database_path, scene_id, config)
                    if preview.get("status") == "unchanged":
                        finish_queued_rename(database_path, scene_id, "skipped", "Naming metadata is unchanged")
                        audit(database_path, "rename", "automatic rename", "skipped", scene_id=scene_id,
                              detail="Naming metadata is unchanged")
                        skipped += 1
                        continue
                result = apply_scene_filename(
                    database_path, scene_id,
                    lambda file_id, folder, basename: stash.move_files({"ids": [file_id],
                        "destination_folder": folder, "destination_basename": basename}),
                    config,
                )
                result = recover_local_rename_cache(stash, database_path, scene_id, result)
                finish_queued_rename(database_path, scene_id, result.get("status", "unknown"), result.get("reason", ""))
                audit(database_path, "rename", "automatic rename", result.get("status", "unknown"),
                      severity="warning" if result.get("status") == "renamed_with_warning" else "info",
                      scene_id=scene_id, file_id=result.get("file_id"), old_path=result.get("current_path"),
                      new_path=result.get("proposed_path"), detail=result.get("reason", ""),
                      metadata={"base_stem": result.get("base_stem")})
                if result.get("action_performed"):
                    maybe_notify(config, f"Renamed scene {scene_id}: {Path(result['proposed_path']).name}", success=True)
                    refresh_scene_contact_sheet(database_path, result.get("proposed_path"), scene_id, config)
                processed += int(bool(result.get("action_performed")))
                skipped += int(not result.get("action_performed"))
            except Exception as error:
                fail_queued_rename(database_path, scene_id, str(error))
                audit(database_path, "rename", "automatic rename", "failed", severity="error",
                      scene_id=scene_id, detail=str(error))
                try:
                    maybe_notify(config, f"Rename failed for scene {scene_id}: {error}")
                except Exception as notify_error:
                    activity_logger().error("notification failed: %s", notify_error)
                failed += 1
    finally:
        release_worker_schedule(database_path)
    return {"renamed": processed, "skipped": skipped, "failed": failed}


def main():
    plugin_input = load_input()
    mode = ((plugin_input.get("args") or {}).get("mode") or "inventory").lower()
    database_path = Path(__file__).with_name("librarymanager.sqlite3")
    hook_context = (plugin_input.get("args") or {}).get("hookContext") or {}
    if hook_context:
        stash = StashInterface(plugin_input["server_connection"])
        config = filing_config_with_library_roots(stash)
        filing_on_meta = bool(config.get("autoFilingEnabled") and (config.get("autoFilingTrigger") or "import").strip().lower() == "metadata")
        if not config.get("automaticRenaming") and not filing_on_meta:
            print(json.dumps({"output": "Hooks skipped: Automatic Renaming and Metadata-triggered Filing are disabled."}))
            return

        hook_type = str(hook_context.get("type") or "").strip()
        changed = hook_context.get("input") or {}
        entity_id = hook_context.get("id")

        target_scene_ids = []
        if "Performer" in hook_type:
            relevant = bool({"name", "disambiguation", "alias_list"} & set(changed)) if changed else True
            if not relevant:
                print(json.dumps({"output": "Performer update has no relevant naming changes; skipping."}))
                return
            try:
                res = stash.call_GQL(PERFORMER_SCENES_QUERY, {"id": str(entity_id)})
                target_scene_ids = [str(s["id"]) for s in (((res or {}).get("findScenes") or {}).get("scenes") or []) if s.get("id")]
            except Exception as e:
                activity_logger().error("Failed to query scenes for performer %s: %s", entity_id, e)
        elif "Studio" in hook_type:
            relevant = bool({"name"} & set(changed)) if changed else True
            if not relevant:
                print(json.dumps({"output": "Studio update has no name changes; skipping."}))
                return
            try:
                res = stash.call_GQL(STUDIO_SCENES_QUERY, {"id": str(entity_id)})
                target_scene_ids = [str(s["id"]) for s in (((res or {}).get("findScenes") or {}).get("scenes") or []) if s.get("id")]
            except Exception as e:
                activity_logger().error("Failed to query scenes for studio %s: %s", entity_id, e)
        else:
            relevant = scene_hook_has_naming_changes(changed)
            if not relevant:
                detail = (
                    "Scene update has no explicit naming metadata changes; skipping."
                    if not changed
                    else "Scene update has no relevant naming metadata changes; skipping."
                )
                print(json.dumps({"output": detail}))
                return
            if entity_id:
                target_scene_ids = [str(entity_id)]

        if not target_scene_ids:
            print(json.dumps({"output": "No associated scenes found."}))
            return

        # Automatic Filing: evaluate filing proposal on metadata update if enabled
        if filing_on_meta:
            for sid in target_scene_ids:
                try:
                    scene_res = stash.call_GQL(
                        "query FindSceneForFiling($id: ID!) { findScene(id: $id) { id title files { id path } performers { id name disambiguation alias_list } studio { id name aliases } tags { id name aliases } } }",
                        {"id": str(sid)}
                    )
                    sc = (scene_res or {}).get("findScene")
                    if sc and sc.get("files"):
                        fpath = sc["files"][0].get("path")
                        if fpath:
                            evaluate_filing_proposal(database_path, stash, fpath, sc, config)
                except Exception as f_err:
                    activity_logger().warning("Automatic filing evaluation failed for scene %s on metadata update: %s", sid, f_err)

        if not config.get("automaticRenaming"):
            print(json.dumps({"output": "Automatic filing evaluation complete; renaming skipped (disabled)."}))
            return

        rename_settle = int(config.get("renameSettleSeconds") if config.get("renameSettleSeconds") is not None else 30)
        scheduled_any = False
        enqueued_count = 0

        for sid in target_scene_ids:
            if not automatic_scene_allowed(config, sid):
                audit(database_path, "rename", "metadata edit", "skipped", scene_id=sid,
                      detail=f"Automatic filename changes are limited to Test Scene ID {config.get('testSceneId')}")
                continue
            try:
                refresh_scene(stash, database_path, sid)
                preview = preview_scene_filename(database_path, str(sid), config)
                if preview.get("status") == "unchanged":
                    cancel_pending_rename(database_path, str(sid))
                    continue
            except Exception:
                pass
            should_schedule = enqueue_rename(database_path, str(sid), time.time(), debounce_seconds=rename_settle)
            if should_schedule:
                scheduled_any = True
            enqueued_count += 1

        if scheduled_any:
            try:
                job_id = stash.run_plugin_task("librarymanager", "Process Rename Queue")
                print(json.dumps({"output": f"{enqueued_count} scene(s) queued for rename worker job {job_id}."}))
            except Exception:
                release_worker_schedule(database_path)
                raise
        elif enqueued_count > 0:
            print(json.dumps({"output": f"{enqueued_count} scene(s) coalesced into the pending rename queue."}))
        else:
            print(json.dumps({"output": "No scene filenames required renaming."}))
        return
    elif mode == "generate_incoming_contact_sheets":
        stash = StashInterface(plugin_input["server_connection"])
        config = stash.find_plugin_config("librarymanager") or {}
        configured_folders = get_configured_incoming_folders(config)
        valid_paths = [Path(p) for p in configured_folders if p and Path(p).is_dir()]
        if not valid_paths:
            message = f"No valid incoming folder(s) configured ({configured_folders})."
        else:
            grid = config.get("contactSheetGrid") or "4x4"
            banner = config.get("contactSheetBanner") is not False
            adjust_vert = config.get("contactSheetAdjustVertical") is not False
            custom_script = config.get("contactSheetScript") or ""
            allow_custom_script = config.get("allowCustomContactSheetScript") is True
            video_extensions = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv"}
            candidates = []
            for inc_dir in valid_paths:
                candidates.extend([
                    p for p in inc_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in video_extensions
                ])
            missing = [
                p for p in candidates
                if not Path(f"{p}.jpg").exists() and not Path(f"{p.stem}.jpg").exists()
            ]
            generated_count = 0
            skipped_count = len(candidates) - len(missing)
            errors = []
            total_missing = len(missing)
            for i, vid in enumerate(missing):
                current_num = i + 1
                remaining_num = total_missing - current_num
                try:
                    import stashapi.log as stash_log
                    stash_log.progress(current_num / max(1, total_missing))
                except Exception:
                    pass
                try:
                    con = connect(database_path)
                    now_ts = time.time()
                    con.execute(
                        "INSERT INTO incoming_files "
                        "(path, first_seen_at, last_checked_at, size, stable_since, settle_seconds, status, detail) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(path) DO UPDATE SET last_checked_at=excluded.last_checked_at, "
                        "status=excluded.status, detail=excluded.detail",
                        (str(vid), now_ts, now_ts, vid.stat().st_size if vid.exists() else 0,
                         now_ts, 0, "generating_sheet",
                         f"Generating contact sheet {current_num} of {total_missing} ({remaining_num} remaining)")
                    )
                    con.commit()
                    con.close()
                except Exception:
                    pass

                sheet_path_str = f"{vid}.jpg"
                expect_filesystem_create(database_path, sheet_path_str)
                expect_filesystem_create(database_path, f"{vid.stem}.jpg")

                res = generate_video_contact_sheet(
                    vid,
                    grid=grid,
                    include_banner=banner,
                    adjust_vertical=adjust_vert,
                    custom_script=custom_script,
                    allow_custom_script=allow_custom_script
                )
                try:
                    con = connect(database_path)
                    con.execute("DELETE FROM incoming_files WHERE path=?", (str(vid),))
                    con.commit()
                    con.close()
                except Exception:
                    pass

                if res.get("status") == "generated":
                    generated_count += 1
                    try:
                        record_activity(
                            database_path,
                            "companion",
                            "contact sheet generated",
                            "recorded",
                            new_path=res.get("path"),
                            detail=f"[{current_num}/{total_missing}] Generated {res.get('grid', grid)} contact sheet for {vid.name}"
                        )
                    except Exception:
                        pass
                elif res.get("status") == "error":
                    error_detail = res.get("error") or "Contact sheet generation failed"
                    errors.append(f"{vid.name}: {error_detail}")
                    record_activity(
                        database_path, "companion", "contact sheet generation", "failed",
                        severity="error", old_path=str(vid), detail=error_detail
                    )
            err_str = f" Errors: {', '.join(errors)}" if errors else ""
            scope_name = contact_sheet_scope_name(configured_folders)
            message = (
                f"Generated {generated_count} contact sheet(s) in {scope_name}. "
                f"{skipped_count} already had artwork.{err_str}"
            )
    elif mode == "inventory_progress":
        message = json.dumps(read_inventory_progress(database_path), ensure_ascii=False)
    elif mode in ("inventory", "build_inventory"):
        stash = StashInterface(plugin_input["server_connection"])
        write_inventory_progress(database_path, "preparing", detail="Reading scenes from Stash")
        try:
            scenes = fetch_scenes(stash)
            summary = inventory(
                database_path,
                scenes,
                progress_callback=lambda processed, total: write_inventory_progress(
                    database_path, "running", processed, total, "Checking files on disk"
                ),
            )
            write_inventory_progress(database_path, "complete", summary["files"], summary["files"],
                                     "Baseline inventory complete")
        except Exception as inventory_error:
            current_progress = read_inventory_progress(database_path)
            write_inventory_progress(database_path, "failed", current_progress.get("processed", 0),
                                     current_progress.get("total", 0), str(inventory_error))
            record_activity(
                database_path, "inventory", "library inventory", "failed",
                severity="error", detail=str(inventory_error)
            )
            raise
        if mode == "build_inventory":
            message = json.dumps(summary, ensure_ascii=False)
        else:
            message = (
                f"Read-only inventory complete: {summary['scenes']} scenes containing {summary['files']} files, "
                f"{summary['present']} files present, "
                f"{summary['missing']} missing, {summary['changed_paths']} Stash path changes, "
                f"{summary['restored']} restored."
            )
    elif mode == "reconcile":
        summary, report = reconcile_missing_files(database_path)
        report_path = Path(__file__).with_name("reconciliation-report.json")
        report_path.write_text(json.dumps({"summary": summary, "matches": report}, indent=2, ensure_ascii=False), encoding="utf-8")
        message = (
            f"Read-only reconciliation complete: {summary['missing']} missing records, "
            f"{summary['matched']} candidates, {summary['ambiguous']} ambiguous, "
            f"{summary['unmatched']} unmatched, {summary['skipped_folders']} unavailable folders. "
            f"Report: {report_path}"
        )
    elif mode == "plan":
        plan = build_resolution_plan(database_path)
        plan_path = Path(__file__).with_name("resolution-plan.json")
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        message = (
            f"Read-only resolution plan complete: {plan.get('safe_redundant', 0)} redundant stale records, "
            f"{plan.get('merge_required', 0)} requiring metadata merge. No changes made. Report: {plan_path}"
        )
    elif mode == "preview_merge":
        preview = build_merge_preview(database_path)
        preview_path = Path(__file__).with_name("metadata-merge-preview.json")
        preview_path.write_text(json.dumps(preview, indent=2, ensure_ascii=False), encoding="utf-8")
        message = (
            f"Read-only metadata merge preview complete: {preview.get('ready_to_apply', 0)} ready, "
            f"{preview.get('manual_review', 0)} requiring manual review. No changes made. "
            f"Report: {preview_path}"
        )
    elif mode == "preview_filenames":
        stash = StashInterface(plugin_input["server_connection"])
        config = stash.find_plugin_config("librarymanager") or {}
        summary, filenames = preview_safe_filenames(database_path, config)
        preview_path = Path(__file__).with_name("filename-preview.json")
        preview_path.write_text(json.dumps({"summary": summary, "files": filenames}, indent=2, ensure_ascii=False), encoding="utf-8")
        message = (
            f"Read-only filename preview complete: {summary['examined']} examined, "
            f"{summary['proposed']} proposed, {summary['unchanged']} unchanged, "
            f"{summary['conflicts']} conflicts. No files renamed. Report: {preview_path}"
        )
    elif mode == "process_rename_queue":
        stash = StashInterface(plugin_input["server_connection"])
        result = process_rename_queue(stash, database_path)
        message = f"Rename queue complete: {result['renamed']} renamed, {result['skipped']} skipped, {result['failed']} failed."
    elif mode == "start_monitor":
        stash = StashInterface(plugin_input["server_connection"])
        config = stash.find_plugin_config("librarymanager") or {}
        result = start_filesystem_monitor(stash, database_path, plugin_input["server_connection"])
        message = f"{result['message']}: {len(result.get('roots', []))} roots, {len(result.get('unavailable_roots', []))} unavailable."
        unavailable = result.get("unavailable_roots", [])
        if unavailable:
            try:
                maybe_notify(config, f"{len(unavailable)} library root(s) are unavailable")
            except Exception as notify_error:
                activity_logger().error("notification failed: %s", notify_error)
    elif mode == "ensure_monitor":
        stash = StashInterface(plugin_input["server_connection"])
        config = stash.find_plugin_config("librarymanager") or {}
        result = start_filesystem_monitor(stash, database_path, plugin_input["server_connection"])
        message = result["message"]
    elif mode == "record_config_change":
        changes = plugin_input.get("args", {}).get("changes") or {}
        skip_keys = set()
        if "incomingFolders" in changes and "incomingFolder" in changes:
            skip_keys.add("incomingFolder")

        for key, val in changes.items():
            if key in skip_keys:
                continue
            if key == "automaticIncomingScan":
                status = "enabled" if val else "disabled"
                detail = f"Automatic incoming ingest was {status} by user in settings"
                audit(database_path, "config", "incoming auto-ingest", status, detail=detail)
            elif key == "incomingFolders":
                if isinstance(val, list) and len(val) > 0:
                    cleaned_val = [str(x).strip() for x in val if str(x).strip()]
                    if len(cleaned_val) == 1:
                        detail = f"Incoming folder set to {cleaned_val[0]}"
                    elif len(cleaned_val) > 1:
                        folder_list_str = ", ".join(f"[{i+1}] {f}" for i, f in enumerate(cleaned_val))
                        detail = f"Watched incoming folders updated ({len(cleaned_val)} active): {folder_list_str}"
                    else:
                        detail = "Incoming folders cleared (no active folders)"
                else:
                    detail = "Incoming folders cleared (no active folders)"
                audit(database_path, "config", "incoming folders", "updated", detail=detail)
            elif key == "automaticRenaming":
                status = "enabled" if val else "disabled"
                detail = f"Automatic Renaming was {status} by user in settings"
                audit(database_path, "config", "automatic renaming", status, detail=detail)
            elif key == "autoStartMonitor":
                status = "enabled" if val else "disabled"
                detail = f"Filesystem watcher was {status} by user in settings"
                audit(database_path, "config", "filesystem watcher", status, detail=detail)
            elif key == "automaticMoveReconciliation":
                status = "enabled" if val else "disabled"
                detail = f"External move reconciliation was {status} by user in settings"
                audit(database_path, "config", "move reconciliation", status, detail=detail)
            elif key == "generateContactSheets":
                status = "enabled" if val else "disabled"
                detail = f"Contact sheet generation was {status} by user in settings"
                audit(database_path, "config", "contact sheet generation", status, detail=detail)
            elif key == "renameSettleSeconds":
                audit(database_path, "config", "settle delay", "updated", detail=f"Metadata edit settle delay set to {val}s")
            elif key == "incomingFolder":
                audit(database_path, "config", "incoming folder", "updated", detail=f"Incoming folder set to {val or 'none'}")
            elif key == "incomingSettleMinutes":
                audit(database_path, "config", "incoming delay", "updated", detail=f"Incoming settle delay set to {val} minute(s)")
            elif key == "testSceneId":
                if val:
                    audit(database_path, "config", "test scene limit", "active", detail=f"Automatic renaming restricted to Test Scene {val}")
                else:
                    audit(database_path, "config", "test scene limit", "cleared", detail="Test scene limit removed; automatic renaming applies to all scenes")
            elif key == "stripMetadataFromTitle":
                status = "enabled" if val else "disabled"
                audit(database_path, "config", "title metadata strip", status, detail=f"Embedded performer/studio stripping was {status}")
            elif key == "autoFilingEnabled":
                status = "enabled" if val else "disabled"
                if val:
                    stash = StashInterface(plugin_input["server_connection"])
                    config = stash.find_plugin_config("librarymanager") or {}
                    incoming_folders = get_configured_incoming_folders(config)
                    snapshotted = snapshot_incoming_baseline(database_path, incoming_folders)
                    audit(database_path, "config", "automatic filing", "enabled",
                          detail=f"Automatic filing enabled; baseline snapshot captured {snapshotted} existing incoming files")
                else:
                    audit(database_path, "config", "automatic filing", "disabled", detail="Automatic filing disabled")
            elif key == "autoFilingOrganizeBy":
                audit(database_path, "config", "filing organize by", "updated", detail=f"Automatic filing organization set to {val}")
            elif key == "autoFilingDestinationRoots":
                if isinstance(val, list):
                    cleaned_roots = [str(r).strip() for r in val if str(r).strip()]
                    audit(database_path, "config", "filing destination roots", "updated", detail=f"Automatic filing destination roots set to {', '.join(cleaned_roots)}")
                else:
                    audit(database_path, "config", "filing destination roots", "updated", detail=f"Automatic filing destination roots set to {val}")
            elif key == "autoFilingDestinationRoot":
                audit(database_path, "config", "filing destination root", "updated", detail=f"Automatic filing destination root set to {val}")
            elif key == "autoFilingMatchSource":
                audit(database_path, "config", "filing match source", "updated", detail=f"Automatic filing match source set to {val}")
            elif key == "autoFilingTrigger":
                audit(database_path, "config", "filing trigger", "updated", detail=f"Automatic filing trigger set to {val}")
            elif key == "autoFilingPreserveFilename":
                status = "enabled" if val else "disabled"
                audit(database_path, "config", "filing preserve filename", status, detail=f"Automatic filing filename preservation was {status}")
            else:
                audit(database_path, "config", str(key), "updated", detail=f"Setting '{key}' updated to {val}")
        message = json.dumps({"recorded": True})
    elif mode == "reload_monitor":
        stash = StashInterface(plugin_input["server_connection"])
        reloaded = reload_monitor_runtime(stash, database_path)
        if reloaded:
            audit(database_path, "monitor", "hot-reload", "reloaded", detail="Watcher daemon hot-reloaded configuration successfully")
        message = json.dumps({"reloaded": reloaded})
    elif mode == "stop_monitor":
        result = stop_filesystem_monitor(database_path)
        message = result["message"]
    elif mode == "monitor_status":
        result = filesystem_monitor_summary(database_path)
        message = (f"Filesystem monitor: {result['state']}; PID {result.get('pid')}; "
                   f"{len(result.get('roots', []))} roots; {result.get('pending_events', 0)} recorded events; "
                   f"heartbeat {result.get('heartbeat_at')}.")
    elif mode == "monitor_health":
        stash = StashInterface(plugin_input["server_connection"]) if plugin_input.get("server_connection") else None
        config = (stash.find_plugin_config("librarymanager") if stash and hasattr(stash, "find_plugin_config") else {}) or {}
        result = filesystem_monitor_summary(database_path)
        result.pop("token", None)
        result["incoming"] = incoming_summary(database_path, config=config)
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "live_status":
        stash = StashInterface(plugin_input["server_connection"])
        config = (stash.find_plugin_config("librarymanager") if hasattr(stash, "find_plugin_config") else {}) or {}
        maybe_auto_restart_monitor(stash, database_path, plugin_input.get("server_connection") or {})
        # Auto-prune any failed incoming files that were deleted from disk
        connection = connect(database_path)
        try:
            failed_rows = connection.execute("SELECT path FROM incoming_files WHERE status='failed'").fetchall()
            for r in failed_rows:
                if not Path(r["path"]).exists():
                    connection.execute("UPDATE incoming_files SET status='gone', detail='File removed from disk' WHERE path=?", (r["path"],))
                    audit(database_path, "incoming", "pruned absent file", "gone", new_path=r["path"], detail="Failed incoming file was deleted from disk; alert cleared automatically")
            connection.commit()
        finally:
            connection.close()

        active_jobs = []
        try:
            job_data = stash.call_GQL('{ jobQueue { id description status progress } }')
            for j in (job_data.get("jobQueue") or []):
                if j and j.get("status") in ("RUNNING", "QUEUED"):
                    active_jobs.append({
                        "id": str(j.get("id")),
                        "description": j.get("description") or "Background task",
                        "status": j.get("status"),
                        "progress": j.get("progress"),
                    })
        except Exception:
            pass

        # Self-healing: clear generating_sheet records if no contact sheet job is actively running
        has_csm_job = any("contact" in (j.get("description") or "").lower() or "csm" in (j.get("description") or "").lower() for j in active_jobs)
        if not has_csm_job:
            connection = connect(database_path)
            try:
                connection.execute("DELETE FROM incoming_files WHERE status='generating_sheet'")
                connection.commit()
            finally:
                connection.close()

        monitor_summary = filesystem_monitor_summary(database_path)
        pending = pending_filesystem_events(database_path)
        is_running = monitor_summary.get("state") == "running" and not monitor_summary.get("is_stale") and monitor_summary.get("pid_alive")
        annotate_pending_events_processing_state(pending, monitor_summary.get("active_moves", []), monitor_running=is_running, database_path=database_path)

        # Reconcile proposals first so Incoming and Filing Proposals come from
        # the same authoritative filing state in this response.
        filing_proposals = get_pending_filing_proposals(database_path, stash=stash)
        incoming = incoming_summary(database_path, config=config)
        result = {
            "monitor": monitor_summary,
            "incoming": incoming,
            "active_jobs": active_jobs,
            "activity": recent_activity(database_path, 250),
            "pending_events": pending,
            "grouped_reconciliation": list_review_batches(database_path),
            "transcoder_candidates": pending_transcoder_candidates(database_path),
            "filing_proposals": filing_proposals,
            "server_time": time.time(),
            "current_scene_count": current_scene_count(stash),
        }
        result["monitor"].pop("token", None)
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "startup_status":
        message = json.dumps(macos_startup_status(), ensure_ascii=False)
    elif mode == "configure_startup":
        arguments = plugin_input.get("args") or {}
        result = configure_macos_startup(arguments.get("enabled") is True,
                                         plugin_input.get("server_connection") or {}, database_path)
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "cancel_pending_rename":
        scene_id = str((plugin_input.get("args") or {}).get("scene_id") or "")
        cancelled = cancel_pending_rename(database_path, scene_id)
        message = json.dumps({"cancelled": cancelled, "scene_id": scene_id})
    elif mode == "execute_pending_rename_now":
        scene_id = str((plugin_input.get("args") or {}).get("scene_id") or "")
        make_pending_rename_due(database_path, scene_id)
        stash = StashInterface(plugin_input["server_connection"])
        try:
            job_id = stash.run_plugin_task("librarymanager", "Process Rename Queue")
            message = json.dumps({"executed": True, "scene_id": scene_id, "job_id": job_id})
        except Exception as e:
            message = json.dumps({"executed": False, "error": str(e)})
    elif mode == "process_incoming_file_now":
        arguments = plugin_input.get("args") or {}
        path = str(arguments.get("path") or "").strip()
        result = process_incoming_file_now(database_path, path)
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "retry_incoming_file":
        arguments = plugin_input.get("args") or {}
        path = str(arguments.get("path") or "")
        if not path:
            raise ValueError("File path required to retry scan")
        connection = connect(database_path)
        try:
            connection.execute(
                "UPDATE incoming_files SET status='waiting', attempts=0, stable_since=?, detail='User requested re-scan' WHERE path=?",
                (time.time(), path)
            )
            connection.commit()
        finally:
            connection.close()
        try:
            if Path(path).is_file():
                os.utime(path, None)
        except OSError:
            pass
        audit(database_path, "incoming", "retry scan", "waiting", new_path=path, detail="User initiated manual re-scan from console")
        message = json.dumps({"status": "waiting", "path": path, "detail": "Re-scan scheduled"}, ensure_ascii=False)
    elif mode == "dismiss_incoming_file":
        arguments = plugin_input.get("args") or {}
        path = str(arguments.get("path") or "")
        if not path:
            raise ValueError("File path required to dismiss")
        connection = connect(database_path)
        try:
            connection.execute(
                "UPDATE incoming_files SET status='ignored', detail='Ignored by user' WHERE path=?",
                (path,)
            )
            connection.commit()
        finally:
            connection.close()
        audit(database_path, "incoming", "ignore item", "ignored", new_path=path, detail="User ignored incoming file from console")
        message = json.dumps({"status": "ignored", "path": path, "detail": "Item ignored by user"}, ensure_ascii=False)
    elif mode == "retry_all_incoming_files":
        connection = connect(database_path)
        try:
            cur = connection.cursor()
            cur.execute("UPDATE incoming_files SET status='waiting', attempts=0, stable_since=?, detail='Batch retry by user' WHERE status='failed'", (time.time(),))
            count = cur.rowcount
            connection.commit()
        finally:
            connection.close()
        audit(database_path, "incoming", "batch retry", "waiting", detail=f"User retried {count} failed incoming video(s)")
        message = json.dumps({"status": "waiting", "count": count, "detail": f"Re-scan scheduled for {count} video(s)"}, ensure_ascii=False)
    elif mode == "dismiss_all_incoming_files":
        connection = connect(database_path)
        try:
            cur = connection.cursor()
            cur.execute("UPDATE incoming_files SET status='dismissed', detail='Batch dismissed by user' WHERE status='failed'")
            count = cur.rowcount
            connection.commit()
        finally:
            connection.close()
        audit(database_path, "incoming", "batch dismiss", "dismissed", detail=f"User dismissed {count} failed incoming video alert(s)")
        message = json.dumps({"status": "dismissed", "count": count, "detail": f"Dismissed {count} failed incoming alert(s)"}, ensure_ascii=False)
    elif mode == "execute_grouped_reconciliation":
        arguments = plugin_input.get("args") or {}
        batch_id = arguments.get("batch_id")
        if batch_id in (None, ""):
            raise ValueError("Grouped reconciliation batch ID is required")
        stash = StashInterface(plugin_input["server_connection"])
        result = execute_grouped_move_reconciliation(
            database_path, int(batch_id), stash, owner=f"plugin-{os.getpid()}-{uuid.uuid4().hex}",
        )
        audit(database_path, "reconciliation", "grouped Stash scan", result.get("state", "review"),
              old_path=result.get("source_prefix"), new_path=result.get("destination_prefix"),
              detail=result.get("reason") or "Grouped reconciliation verification completed",
              metadata={"batch_id": result.get("id"), "tracked_count": result.get("tracked_count"),
                        "verified_count": result.get("verified_count"),
                        "uncertain_count": result.get("uncertain_count")})
        message = json.dumps({"status": result.get("state"), "batch": result}, ensure_ascii=False)
    elif mode == "dismiss_grouped_reconciliation":
        arguments = plugin_input.get("args") or {}
        batch_id = arguments.get("batch_id")
        if batch_id in (None, ""):
            raise ValueError("Grouped reconciliation batch ID is required")
        result = dismiss_review_batch(database_path, int(batch_id))
        audit(database_path, "filesystem", "grouped reconciliation", "dismissed",
              old_path=result.get("source_prefix"), new_path=result.get("destination_prefix"),
              detail="User dismissed grouped review; no filesystem or Stash action was taken",
              metadata={"batch_id": result.get("id"), "tracked_count": result.get("tracked_count")})
        message = json.dumps({"status": "dismissed", "batch": result}, ensure_ascii=False)
    elif mode == "resolve_all_filesystem_events":
        arguments = plugin_input.get("args") or {}
        resolution = str(arguments.get("resolution") or "dismiss")
        require_bulk_dismissal(resolution)
        connection = connect(database_path)
        try:
            cursor = connection.cursor()
            cursor.execute("UPDATE filesystem_events SET status='reviewed' WHERE status='pending'")
            count = cursor.rowcount
            connection.commit()
        finally:
            connection.close()
        summary, proposals = reconcile_filesystem_events(database_path)
        audit(database_path, "filesystem", "bulk dismiss", "dismissed",
              detail=f"Bulk dismissed {count} filesystem events", metadata={"count": count})
        message = json.dumps({"status": "dismissed", "count": count,
                              "detail": f"Dismissed {count} change{'s' if count != 1 else ''}"}, ensure_ascii=False)
    elif mode == "promote_transcoder_candidate":
        candidate_path = str((plugin_input.get("args") or {}).get("candidate_path") or "")
        if not candidate_path:
            raise ValueError("Candidate path required")
        promote_transcoder_candidate(database_path, candidate_path)
        audit(database_path, "filesystem", "transcoder candidate", "review",
              new_path=candidate_path,
              detail="User chose to review the encoded file as an independent new file")
        message = json.dumps({"status": "pending", "candidate_path": candidate_path}, ensure_ascii=False)
    elif mode == "resolve_filesystem_event":
        stash = StashInterface(plugin_input["server_connection"])
        arguments = plugin_input.get("args") or {}
        event_key = str(arguments.get("event_key") or "")
        resolution = str(arguments.get("resolution") or "")
        if not event_key or resolution not in ("dismiss", "remove_stash_scene", "scan_destination", "keep_both"):
            raise ValueError("Choose a valid review action")
        connection = connect(database_path)
        try:
            event = connection.execute(
                "SELECT * FROM filesystem_events WHERE event_key=? AND status='pending'", (event_key,)
            ).fetchone()
            if not event:
                raise ValueError("This change is no longer waiting for review")
            event = dict(event)
            file_row = connection.execute("SELECT * FROM files WHERE path=?", (event["source_path"],)).fetchone()
            file_row = dict(file_row) if file_row else None
        finally:
            connection.close()

        scene_id = str(file_row.get("scene_id")) if file_row and file_row.get("scene_id") else None
        detail = ""
        status = "resolved"
        if resolution == "dismiss":
            detail = "User confirmed that this filesystem change needs no Stash action"
            status = "dismissed"
        elif resolution == "remove_stash_scene":
            if not scene_id:
                raise ValueError("Watchtower cannot identify a Stash scene for this deleted file")
            try:
                scene = stash.find_scene(int(scene_id))
            except Exception as err:
                raise RuntimeError(f"Could not verify Stash scene {scene_id} status: {err}") from err

            if scene:
                assert_scene_removal_safe(scene, event["source_path"])
                try:
                    stash.destroy_scene(int(scene_id), delete_file=False)
                    detail = f"Removed stale Stash scene {scene_id}; the video file was already absent"
                except Exception as err:
                    raise RuntimeError(f"Failed to remove Stash scene {scene_id}: {err}") from err
            else:
                detail = f"Stash confirmed scene {scene_id} was already deleted"
        else:
            destination, evidence = validate_filesystem_scan_action(
                database_path, event, resolution
            )
            try:
                job_id = stash.metadata_scan(paths=[destination])
                if not stash.wait_for_job(job_id, timeout=180):
                    raise ValueError(f"Stash scan job {job_id} did not finish within 3 minutes")
                if resolution == "keep_both":
                    detail = f"User confirmed keeping both files; Stash scan job {job_id} checked {destination}"
                else:
                    detail = f"Stash scan job {job_id} checked {destination}"
            except Exception:
                # Mark the event as failed rather than leaving it permanently pending
                # (e.g. network interruption, Stash restart during the scan wait).
                _fc = connect(database_path)
                try:
                    _fc.execute("UPDATE filesystem_events SET status='failed' WHERE event_key=?", (event_key,))
                    _fc.commit()
                finally:
                    _fc.close()
                raise

        connection = connect(database_path)
        try:
            connection.execute("UPDATE filesystem_events SET status='reviewed' WHERE event_key=?", (event_key,))
            connection.commit()
        finally:
            connection.close()
        audit(database_path, "filesystem", event["event_type"], status, scene_id=scene_id,
              file_id=file_row.get("file_id") if file_row else None,
              old_path=event.get("source_path"), new_path=event.get("destination_path"), detail=detail,
              metadata={"resolution": resolution})
        message = json.dumps({"status": status, "detail": detail, "scene_id": scene_id}, ensure_ascii=False)
    elif mode == "reconcile_events":
        stash = StashInterface(plugin_input["server_connection"])
        config = stash.find_plugin_config("librarymanager") or {}
        summary, proposals = reconcile_filesystem_events(database_path)
        report_path = Path(__file__).with_name("filesystem-reconciliation-report.json")
        report_path.write_text(json.dumps({"summary": summary, "proposals": proposals}, indent=2,
                                          ensure_ascii=False), encoding="utf-8")
        message = (f"Read-only event reconciliation complete: {summary['events']} events, "
                   f"{summary['verified']} verified, {summary['review']} needing review, "
                   f"{summary['informational']} informational. No changes made. Report: {report_path}")
        for proposal in proposals:
            severity = "warning" if proposal["confidence"] in ("review", "strong") else "info"
            audit(database_path, "filesystem", proposal["event_type"], proposal["confidence"], severity=severity,
                  scene_id=proposal.get("scene_id"), file_id=proposal.get("file_id"),
                  old_path=proposal.get("old_path"), new_path=proposal.get("new_path"),
                  detail=proposal.get("reason", ""), metadata={"recommendation": proposal.get("recommendation")})
        warnings = [p for p in proposals if p["confidence"] in ("review", "strong")]
        if warnings and config.get("macNotifications"):
            try:
                maybe_notify(config, f"{len(warnings)} filesystem event(s) need review")
            except Exception as notify_error:
                activity_logger().error("notification failed: %s", notify_error)
    elif mode == "activity":
        rows = recent_activity(database_path, (plugin_input.get("args") or {}).get("limit", 250))
        report_path = Path(__file__).with_name("activity-report.json")
        report_path.write_text(json.dumps({"activity": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
        csv_path = Path(__file__).with_name("activity-report.csv")
        columns = ["recorded_at", "category", "severity", "action", "status", "scene_id", "file_id",
                   "old_path", "new_path", "detail"]
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        if rows:
            latest = rows[0]
            message = (f"Activity report contains {len(rows)} recent entries. Latest: {latest['recorded_at']} — "
                       f"{latest['action']} {latest['status']}. JSON: {report_path}; CSV: {csv_path}")
        else:
            message = f"Activity report is empty. JSON: {report_path}; CSV: {csv_path}"
    elif mode == "dashboard":
        stash = StashInterface(plugin_input["server_connection"])
        config = filing_config_with_library_roots(stash)
        roots = config["_libraryRoots"]
        payload = dashboard_data(
            database_path,
            (plugin_input.get("args") or {}).get("limit", 250),
            stash=stash,
            config=config,
        )
        payload["monitor"].pop("token", None)
        payload["library_roots"] = [{"path": root, "exists": os.path.exists(root)} for root in roots]
        payload["incoming_folder"] = incoming_folder_status(config, roots)
        payload["incoming_folders"] = incoming_folders_status(config, roots)
        payload["startup"] = macos_startup_status()
        payload["current_scene_count"] = current_scene_count(stash)
        message = json.dumps(payload, ensure_ascii=False)
    elif mode == "reports":
        message = json.dumps({"reports": dashboard_reports()}, ensure_ascii=False)
    elif mode in ("preview_manual_filename", "apply_manual_filename"):
        stash = StashInterface(plugin_input["server_connection"])
        arguments = plugin_input.get("args") or {}
        scene_id = str(arguments.get("scene_id") or "").strip()
        requested_name = str(arguments.get("filename") or "").strip()
        if not scene_id:
            raise ValueError("Enter a Scene ID")
        refresh_scene(stash, database_path, scene_id)
        if mode == "preview_manual_filename":
            result = preview_manual_filename(database_path, scene_id, requested_name)
        else:
            result = apply_manual_filename(
                database_path, scene_id, requested_name,
                lambda file_id, folder, basename: stash.move_files({"ids": [file_id], "destination_folder": folder,
                                                                    "destination_basename": basename}),
            )
            result = recover_local_rename_cache(stash, database_path, scene_id, result)
            audit(database_path, "rename", "manual filename correction", result.get("status", "unknown"),
                  severity="warning" if result.get("status") == "renamed_with_warning" else "info",
                  scene_id=scene_id, file_id=result.get("file_id"), old_path=result.get("current_path"),
                  new_path=result.get("proposed_path"), detail=result.get("reason", ""))
            if result.get("status") == "renamed" or result.get("action_performed"):
                config = stash.find_plugin_config("librarymanager") or {}
                refresh_scene_contact_sheet(database_path, result.get("proposed_path"), scene_id, config)
        message = json.dumps(result, ensure_ascii=False)
    elif mode in ("preview_test_rename", "apply_test_rename"):
        stash = StashInterface(plugin_input["server_connection"])
        config = stash.find_plugin_config("librarymanager") or {}
        arguments = plugin_input.get("args") or {}
        override_config = arguments.get("config") or arguments.get("filename_options") or {}
        active_config = {**config, **override_config}
        scene_id = str(arguments.get("scene_id") or active_config.get('testSceneId') or "").strip()
        if not scene_id:
            raise ValueError("Set Test Scene ID in the Stash Library Manager settings first")
        refresh_scene(stash, database_path, scene_id)
        if mode == "preview_test_rename":
            result = preview_scene_filename(database_path, scene_id, active_config, ignore_protection=True)
        else:
            result = apply_scene_filename(
                database_path, scene_id,
                lambda file_id, folder, basename: stash.move_files({"ids": [file_id], "destination_folder": folder,
                                                                    "destination_basename": basename}),
                active_config,
                ignore_protection=True,
            )
            result = recover_local_rename_cache(stash, database_path, scene_id, result)
            audit(database_path, "rename", "test scene rename", result.get("status", "unknown"),
                  severity="warning" if result.get("status") == "renamed_with_warning" else "info",
                  scene_id=scene_id, file_id=result.get("file_id"), old_path=result.get("current_path"),
                  new_path=result.get("proposed_path"), detail=result.get("reason", ""))
            if result.get("status") in ("renamed", "ready") or result.get("action_performed"):
                refresh_scene_contact_sheet(database_path, result.get("proposed_path"), scene_id, active_config)
        result_path = Path(__file__).with_name("test-rename-result.json")
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "filing_proposals":
        stash = StashInterface(plugin_input["server_connection"])
        result = {"proposals": get_pending_filing_proposals(database_path, stash=stash)}
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "approve_filing_proposal":
        args = plugin_input.get("args") or {}
        proposal_id = args.get("proposal_id")
        update_metadata = bool(args.get("update_metadata", False))
        target_destination_folder = args.get("target_destination_folder")
        target_entity_type = args.get("target_entity_type")
        target_entity_id = args.get("target_entity_id")
        stash = StashInterface(plugin_input["server_connection"])
        config = filing_config_with_library_roots(stash)
        result = apply_filing_proposal(
            database_path, stash, int(proposal_id),
            config=config,
            update_metadata=update_metadata,
            target_destination_folder=target_destination_folder,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id
        )
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "ignore_filing_proposal":
        proposal_id = plugin_input.get("args", {}).get("proposal_id")
        result = ignore_filing_proposal(database_path, int(proposal_id))
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "recover_filing_proposal":
        proposal_id = plugin_input.get("args", {}).get("proposal_id")
        stash = StashInterface(plugin_input["server_connection"])
        result = recover_filing_proposal(database_path, stash, int(proposal_id))
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "establish_filing_baseline":
        stash = StashInterface(plugin_input["server_connection"])
        config = stash.find_plugin_config("librarymanager") or {}
        incoming_folders = get_configured_incoming_folders(config)
        snapshotted = snapshot_incoming_baseline(database_path, incoming_folders)
        message = json.dumps({"snapshotted": snapshotted}, ensure_ascii=False)
    elif mode == "get_filing_folder_mappings":
        mappings = get_filing_folder_mappings(database_path)
        message = json.dumps({"mappings": mappings}, ensure_ascii=False)
    elif mode == "save_filing_folder_mapping":
        args = plugin_input.get("args") or {}
        entity_type = args.get("entity_type")
        entity_id = args.get("entity_id")
        entity_name = args.get("entity_name")
        folder_path = args.get("folder_path")
        stash = StashInterface(plugin_input["server_connection"])
        config = filing_config_with_library_roots(stash)
        configured_roots = get_configured_filing_destination_roots(config)
        success, msg = save_filing_folder_mapping(
            database_path, entity_type, entity_id, entity_name, folder_path,
            configured_roots=configured_roots
        )
        mappings = get_filing_folder_mappings(database_path)
        message = json.dumps({"success": success, "message": msg, "mappings": mappings}, ensure_ascii=False)
    elif mode == "delete_filing_folder_mapping":
        mapping_id = plugin_input.get("args", {}).get("mapping_id")
        success = delete_filing_folder_mapping(database_path, int(mapping_id))
        mappings = get_filing_folder_mappings(database_path)
        message = json.dumps({"success": success, "mappings": mappings}, ensure_ascii=False)
    elif mode in ("refresh_destination_roots_cache", "refresh_filing_cache"):
        stash = StashInterface(plugin_input["server_connection"])
        config = filing_config_with_library_roots(stash)
        roots = get_configured_filing_destination_roots(config)
        max_depth = int(config.get("autoFilingMaxDiscoveryDepth", 4))
        result = refresh_destination_dir_cache(database_path, roots, max_depth=max_depth)
        message = json.dumps({"success": True, "message": f"Destination folders cache refreshed ({result.get('total_folders', 0)} folders discovered across {len(result.get('scanned_roots', []))} root(s)).", **result}, ensure_ascii=False)
    elif mode == "retry_filing_proposal":
        args = plugin_input.get("args") or {}
        path = args.get("path")
        allow_baseline = bool(args.get("allow_baseline", False))
        allow_refresh = bool(args.get("allow_refresh", False))
        proposal_id = args.get("proposal_id")
        stash = StashInterface(plugin_input["server_connection"])
        config = filing_config_with_library_roots(stash)
        result = retry_filing_proposal(database_path, stash, path, config=config, allow_baseline=allow_baseline, allow_refresh=allow_refresh, proposal_id=proposal_id)
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "get_backlog_items":
        args = plugin_input.get("args") or {}
        force_refresh = args.get("force_refresh") is True or args.get("recheck") is True
        stash = StashInterface(plugin_input["server_connection"]) if "server_connection" in plugin_input else None
        config = filing_config_with_library_roots(stash) if stash else None
        result = get_backlog_items(database_path, stash, config=config, force_refresh=force_refresh)
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "acknowledge_backlog_missing":
        paths = (plugin_input.get("args") or {}).get("paths") or []
        stash = StashInterface(plugin_input["server_connection"]) if "server_connection" in plugin_input else None
        config = filing_config_with_library_roots(stash) if stash else None
        result = acknowledge_backlog_missing(database_path, paths, config=config)
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "evaluate_backlog_batch":
        args = plugin_input.get("args") or {}
        paths = args.get("paths") or []
        refresh_metadata = args.get("refresh_metadata") is True
        stash = StashInterface(plugin_input["server_connection"])
        config = filing_config_with_library_roots(stash)
        result = evaluate_backlog_batch(
            database_path, stash, paths, config=config, allow_refresh=refresh_metadata
        )
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "inspect_backlog_duplicate":
        args = plugin_input.get("args") or {}
        candidate_path = str(args.get("path") or "")
        if not candidate_path:
            raise ValueError("Duplicate candidate path required")
        stash = StashInterface(plugin_input["server_connection"])
        config = stash.find_plugin_config("librarymanager") or {}
        result = inspect_backlog_duplicate(
            database_path, stash, candidate_path, config,
            request_verification=args.get("request_verification") is True,
        )
        if not result:
            raise ValueError("Watchtower could not find a same-scene duplicate for this incoming file")
        message = json.dumps(result, ensure_ascii=False)
    elif mode == "delete_backlog_duplicate":
        args = plugin_input.get("args") or {}
        stash = StashInterface(plugin_input["server_connection"])
        config = stash.find_plugin_config("librarymanager") or {}
        result = delete_verified_backlog_duplicate(
            database_path, stash,
            candidate_path=str(args.get("path") or ""),
            config=config,
            expected_scene_id=str(args.get("scene_id") or ""),
            expected_file_id=str(args.get("file_id") or ""),
            expected_retained_file_id=str(args.get("retained_file_id") or ""),
            expected_sha256=str(args.get("sha256") or ""),
            selected_companions=args.get("companions") or [],
        )
        message = json.dumps(result, ensure_ascii=False)
    else:
        raise ValueError(f"Unsupported Library Manager mode: {mode}")
    print(json.dumps({"output": message}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        raise
