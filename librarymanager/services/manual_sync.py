"""Manual synchronization and unindexed media ingestion service for Watchtower.

This module is responsible for:
- Discovering unindexed media files on monitored library roots
- Ingesting unindexed video files into Stash with full metadata scans and previews
- Managing ignored unindexed media files (marking, clearing, and resetting)
- Dispatching cross-platform native desktop notifications
- Verifying file existence and maintaining database consistency for unindexed records
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from librarymanager.database import create_connection, ensure_schema
from librarymanager.integrations.stash_graphql import (
    ensure_stash_supports_extension,
    find_scene_by_path,
)
from librarymanager.scanning.filesystem import (
    _is_subpath_of,
    expect_filesystem_create,
    get_companion_files,
)

logger = logging.getLogger("librarymanager.services.manual_sync")

INCOMING_SCAN_FLAGS: Dict[str, bool] = {
    "scanGenerateCovers": True,
    "scanGeneratePreviews": True,
    "scanGenerateSprites": True,
    "scanGeneratePhashes": True,
    "scanGenerateThumbnails": True,
}


def connect(database_path: Path | str) -> sqlite3.Connection:
    """Create a configured SQLite connection and ensure schema is applied."""
    conn = create_connection(database_path)
    ensure_schema(conn, database_path)
    return conn


def _get_record_activity_fn() -> Optional[Callable]:
    """Dynamically resolve record_activity to avoid circular dependencies."""
    mod = sys.modules.get("librarymanager_core")
    if mod and hasattr(mod, "record_activity"):
        return getattr(mod, "record_activity")
    try:
        import librarymanager_core as core
        return getattr(core, "record_activity", None)
    except ImportError:
        return None


def _get_refresh_scene_inventory_fn() -> Optional[Callable]:
    """Dynamically resolve refresh_scene_inventory without circular imports."""
    mod = sys.modules.get("librarymanager_core")
    if mod and hasattr(mod, "refresh_scene_inventory"):
        return getattr(mod, "refresh_scene_inventory")
    try:
        import librarymanager_core as core
        return getattr(core, "refresh_scene_inventory", None)
    except ImportError:
        return None


def _get_generate_video_contact_sheet_fn() -> Optional[Callable]:
    """Dynamically resolve generate_video_contact_sheet without circular imports."""
    mod = sys.modules.get("librarymanager_core")
    if mod and hasattr(mod, "generate_video_contact_sheet"):
        return getattr(mod, "generate_video_contact_sheet")
    try:
        import librarymanager_core as core
        return getattr(core, "generate_video_contact_sheet", None)
    except ImportError:
        return None


def _get_evaluate_filing_proposal_fn() -> Optional[Callable]:
    """Dynamically resolve evaluate_filing_proposal without circular imports."""
    mod = sys.modules.get("librarymanager_core")
    if mod and hasattr(mod, "evaluate_filing_proposal"):
        return getattr(mod, "evaluate_filing_proposal")
    try:
        import librarymanager_core as core
        return getattr(core, "evaluate_filing_proposal", None)
    except ImportError:
        return None


def _ensure_stash_supports_extension(stash: Any, path: str) -> None:
    """Ensure Stash's configured videoExtensions include this file's extension so the scanner accepts it."""
    ensure_stash_supports_extension(stash, path)


def notify_desktop(message: str, title: str = "Stash Library Manager") -> None:
    """Send native desktop notification across macOS, Windows, Linux."""
    try:
        if sys.platform == "darwin":
            subprocess.run([
                "/usr/bin/osascript", "-e", "on run argv", "-e",
                "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e", "end run", "--", str(message), str(title),
            ], capture_output=True, text=True, timeout=10, check=False)
        elif sys.platform == "win32":
            import base64
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
        logger.debug("Desktop notification failed: %s", exc)


_ORIGINAL_NOTIFY_DESKTOP = notify_desktop


def get_notify_desktop_fn() -> Callable:
    """Return active notify_desktop function, prioritizing any monkeypatch applied to
    either librarymanager.services.manual_sync or librarymanager_core.
    """
    canon = sys.modules.get(__name__, None)
    canon_fn = getattr(canon, "notify_desktop", None) if canon else notify_desktop
    if canon_fn is not None and canon_fn is not _ORIGINAL_NOTIFY_DESKTOP:
        return canon_fn

    core_mod = sys.modules.get("librarymanager_core")
    if core_mod is not None:
        core_notify = getattr(core_mod, "notify_desktop", None)
        if core_notify is not None and not getattr(core_notify, "_is_watchtower_wrapper", False):
            return core_notify

    return canon_fn or _ORIGINAL_NOTIFY_DESKTOP


def get_unindexed_external_media(database_path: Path) -> list[dict]:
    """Return all non-ignored unindexed video files found on disk."""
    connection = connect(database_path)
    try:
        connection.execute(
            "DELETE FROM unindexed_external_media WHERE path IN (SELECT path FROM files)"
        )
        connection.commit()
        rows = connection.execute(
            "SELECT path, basename, size, discovered_at FROM unindexed_external_media WHERE ignored = 0 AND path NOT IN (SELECT path FROM files) ORDER BY discovered_at DESC"
        ).fetchall()
        result = []
        to_clean = []
        for r in rows:
            p = r["path"]
            try:
                if os.path.isfile(p):
                    result.append(dict(r))
                else:
                    to_clean.append(p)
            except OSError:
                to_clean.append(p)
        if to_clean:
            connection.executemany("DELETE FROM unindexed_external_media WHERE path = ?", [(p,) for p in to_clean])
            connection.commit()
        return result
    finally:
        connection.close()


def add_unindexed_media_file(database_path: Path, stash: Any, path: str, config: dict | None = None) -> dict:
    """Trigger a full Stash ingestion scan for an unindexed video file, including previews, sprites, companions, and contact sheets."""
    if not os.path.isfile(path):
        return {"status": "error", "message": f"File not found on disk: {path}"}
    if config is None and stash:
        try:
            cfg = stash.find_plugin_config("librarymanager") or {}
        except Exception:
            cfg = {}
    else:
        cfg = config or {}
    scene = None
    if stash:
        _ensure_stash_supports_extension(stash, path)
        try:
            scan_job_id = stash.metadata_scan(paths=[path], flags=INCOMING_SCAN_FLAGS)
            stash.wait_for_job(scan_job_id, timeout=180)
        except Exception as scan_err:
            logger.warning("Targeted scan failed for %s: %s", path, scan_err)

        # Look up the newly created scene in Stash
        try:
            scene = find_scene_by_path(stash, path)
            if scene:
                refresh_fn = _get_refresh_scene_inventory_fn()
                if refresh_fn:
                    refresh_fn(database_path, scene)
        except Exception as query_err:
            logger.debug("Failed querying scene by path after scan for %s: %s", path, query_err)

        # Trigger metadata generation to guarantee previews/covers/sprites/phashes are generated
        if scene and scene.get("id"):
            try:
                gen_job = stash.call_GQL(
                    """mutation GenerateForScene($input: GenerateMetadataInput!) {
                      metadataGenerate(input: $input)
                    }""",
                    {
                        "input": {
                            "sceneIDs": [str(scene["id"])],
                            "covers": True,
                            "previews": True,
                            "sprites": True,
                            "phashes": True,
                            "imagePreviews": True,
                        }
                    }
                )
                if isinstance(gen_job, dict) and gen_job.get("metadataGenerate"):
                    stash.wait_for_job(gen_job["metadataGenerate"], timeout=180)
            except Exception as gen_err:
                logger.debug("Failed generating previews for scene %s: %s", scene.get("id"), gen_err)

    record_activity_fn = _get_record_activity_fn()
    desktop_notify = get_notify_desktop_fn()

    # Relocate or associate any companion files existing alongside the video
    if scene and scene.get("id"):
        try:
            companions = get_companion_files(path)
            for comp in companions:
                comp_path = str(Path(path).parent / comp)
                if record_activity_fn:
                    record_activity_fn(
                        database_path, "companion", "auto-paired companion", "recorded",
                        scene_id=str(scene["id"]),
                        old_path=comp_path,
                        new_path=comp_path,
                        detail=f"Paired companion {comp} with newly ingested scene {scene['id']}"
                    )
        except Exception as comp_err:
            logger.debug("Failed pairing companions for %s: %s", path, comp_err)

    # Generate visual contact sheet if enabled
    if cfg.get("generateContactSheets") is True or cfg.get("generate_contact_sheets") is True:
        try:
            sheet_p = f"{path}.jpg"
            expect_filesystem_create(database_path, sheet_p)
            csm_fn = _get_generate_video_contact_sheet_fn()
            csm_res = csm_fn(
                path,
                grid=cfg.get("contactSheetGrid") or cfg.get("contact_sheet_grid") or "4x4",
                include_banner=cfg.get("contactSheetBanner") is not False and cfg.get("contact_sheet_banner") is not False,
                adjust_vertical=cfg.get("contactSheetAdjustVertical") is not False and cfg.get("contact_sheet_adjust_vertical") is not False,
                custom_script=cfg.get("contactSheetScript") or cfg.get("contact_sheet_script") or "",
                allow_custom_script=cfg.get("allowCustomContactSheetScript") is True or cfg.get("allow_custom_contact_sheet_script") is True,
            ) if csm_fn else {}
            if csm_res.get("status") == "generated":
                if record_activity_fn:
                    record_activity_fn(
                        database_path, "companion", "contact sheet generated", "recorded",
                        scene_id=str(scene["id"]) if (scene and scene.get("id")) else None,
                        new_path=sheet_p,
                        detail=f"Generated {csm_res.get('grid', '4x4')} contact sheet for visual file browsing"
                    )
                if cfg.get("macNotifications") is not False and cfg.get("mac_notifications") is not False:
                    desktop_notify(f"Contact sheet created & paired: {Path(sheet_p).name} → Scene {scene['id']}")
        except Exception as csm_err:
            logger.debug("Failed generating contact sheet for %s: %s", path, csm_err)

    # Evaluate automatic filing if enabled
    if scene and (cfg.get("autoFilingEnabled") or cfg.get("auto_filing_enabled")):
        try:
            trigger = (cfg.get("autoFilingTrigger") or cfg.get("auto_filing_trigger") or "import").strip().lower()
            if trigger in ("import", "scan"):
                filing_fn = _get_evaluate_filing_proposal_fn()
                if filing_fn:
                    filing_fn(database_path, stash, path, scene, cfg)
        except Exception as filing_err:
            logger.debug("Automatic filing evaluation failed for %s: %s", path, filing_err)

    connection = connect(database_path)
    try:
        connection.execute("DELETE FROM unindexed_external_media WHERE path = ?", (path,))
        connection.commit()
    finally:
        connection.close()

    scene_id = str(scene["id"]) if (scene and scene.get("id")) else None
    if scene_id:
        if record_activity_fn:
            record_activity_fn(
                database_path, "incoming", "add external video", "imported",
                scene_id=scene_id,
                new_path=path, detail=f"Added external video {Path(path).name} to Stash with previews & contact sheet"
            )
        if cfg.get("macNotifications") is not False and cfg.get("mac_notifications") is not False:
            desktop_notify(f"Added to Stash: {Path(path).name} → Scene {scene_id}")
        return {"status": "ok", "scene_id": scene_id, "message": f"Successfully added {Path(path).name} as Scene {scene_id}!"}
    else:
        if record_activity_fn:
            record_activity_fn(
                database_path, "incoming", "add external video", "failed",
                severity="warning",
                new_path=path, detail=f"Stash scan did not produce a scene record for {Path(path).name}"
            )
        return {"status": "warning", "message": f"Stash scan did not produce a scene record for {Path(path).name}."}


def add_all_unindexed_media_files(database_path: Path, stash: Any, config: dict | None = None, roots: list | None = None) -> dict:
    """Trigger full Stash ingestion for all discovered unindexed video files."""
    items = get_unindexed_external_media(database_path)
    if roots:
        items = [i for i in items if any(_is_subpath_of(i["path"], r) for r in roots)]
    paths = [i["path"] for i in items if os.path.isfile(i["path"])]
    if not paths:
        return {"status": "ok", "count": 0, "message": "No unindexed media to add."}
    added_count = 0
    for p in paths:
        res = add_unindexed_media_file(database_path, stash, p, config=config)
        if res.get("status") == "ok":
            added_count += 1
    return {"status": "ok", "count": added_count, "message": f"Successfully added {added_count} video(s) to Stash with previews & contact sheets!"}


def ignore_unindexed_media_file(database_path: Path, path: str) -> dict:
    """Mark an unindexed video file as ignored so it does not prompt again."""
    connection = connect(database_path)
    try:
        connection.execute("UPDATE unindexed_external_media SET ignored = 1 WHERE path = ?", (path,))
        connection.commit()
    finally:
        connection.close()
    return {"status": "ok"}


def ignore_all_unindexed_media_files(database_path: Path) -> dict:
    """Mark all currently discovered unindexed video files as ignored."""
    connection = connect(database_path)
    try:
        connection.execute("UPDATE unindexed_external_media SET ignored = 1 WHERE ignored = 0")
        connection.commit()
    finally:
        connection.close()
    return {"status": "ok"}


def reset_ignored_unindexed_media_files(database_path: Path) -> dict:
    """Un-ignore all previously ignored external media files so they immediately reappear in active review."""
    connection = connect(database_path)
    try:
        count_row = connection.execute("SELECT count(*) FROM unindexed_external_media WHERE ignored = 1").fetchone()
        count = int(count_row[0]) if count_row else 0
        connection.execute("UPDATE unindexed_external_media SET ignored = 0 WHERE ignored = 1")
        # Clean up any that are already in files or no longer on disk
        connection.execute("DELETE FROM unindexed_external_media WHERE path IN (SELECT path FROM files)")
        connection.commit()
        record_activity_fn = _get_record_activity_fn()
        if record_activity_fn:
            record_activity_fn(
                database_path, "reconciliation", "reset ignored media", "cleared",
                severity="info",
                detail=f"Restored {count} ignored media file(s) to active review"
            )
        return {
            "status": "ok",
            "count": count,
            "message": f"Restored {count} ignored file(s) to active review."
        }
    finally:
        connection.close()


__all__ = [
    "INCOMING_SCAN_FLAGS",
    "_ensure_stash_supports_extension",
    "add_all_unindexed_media_files",
    "add_unindexed_media_file",
    "connect",
    "get_notify_desktop_fn",
    "get_unindexed_external_media",
    "ignore_all_unindexed_media_files",
    "ignore_unindexed_media_file",
    "notify_desktop",
    "reset_ignored_unindexed_media_files",
]
