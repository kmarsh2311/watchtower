"""High-level domain services for Watchtower."""
from __future__ import annotations

from librarymanager.services.contact_sheets import (
    ffmpeg_supports_drawtext,
    find_csm_font,
    format_csm_duration,
    generate_ffmpeg_contact_sheet_fallback,
    generate_video_contact_sheet,
)
from librarymanager.services.manual_sync import (
    INCOMING_SCAN_FLAGS,
    _ensure_stash_supports_extension,
    add_all_unindexed_media_files,
    add_unindexed_media_file,
    get_unindexed_external_media,
    ignore_all_unindexed_media_files,
    ignore_unindexed_media_file,
    notify_desktop,
    reset_ignored_unindexed_media_files,
)
from librarymanager.services.renaming import (
    apply_manual_filename,
    apply_scene_filename,
    cancel_pending_rename,
    claim_due_rename,
    enqueue_rename,
    fail_queued_rename,
    finish_queued_rename,
    make_pending_rename_due,
    release_worker_schedule,
    rename_lock,
)

__all__ = [
    "INCOMING_SCAN_FLAGS",
    "_ensure_stash_supports_extension",
    "add_all_unindexed_media_files",
    "add_unindexed_media_file",
    "apply_manual_filename",
    "apply_scene_filename",
    "cancel_pending_rename",
    "claim_due_rename",
    "enqueue_rename",
    "fail_queued_rename",
    "ffmpeg_supports_drawtext",
    "find_csm_font",
    "finish_queued_rename",
    "format_csm_duration",
    "generate_ffmpeg_contact_sheet_fallback",
    "generate_video_contact_sheet",
    "get_unindexed_external_media",
    "ignore_all_unindexed_media_files",
    "ignore_unindexed_media_file",
    "make_pending_rename_due",
    "notify_desktop",
    "release_worker_schedule",
    "rename_lock",
    "reset_ignored_unindexed_media_files",
]


