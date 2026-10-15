"""Regression tests for Phase 2: Filesystem Safety & Atomic Moves.

Verifies:
1. Safe, no-overwrite file moves for both same-filesystem and cross-filesystem operations.
2. Atomic destination collision handling, even when destination appears after initial check.
3. Partial copy cleanup and source preservation.
4. Source deletion failure resilience (destination preserved, source intact, error reported).
5. Metadata preservation failure tolerance.
6. Hardened relocate_companions_transactionally with state verification and accurate rollback reporting.
7. Companion rollback failures (missing destination, source collision, move failure).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from librarymanager.services.filing import _move_file, _safe_cross_device_move
from librarymanager_monitor import relocate_companions_transactionally


# ==============================================================================
# 1. _move_file and Cross-Device Move Tests
# ==============================================================================

def test_safe_move_same_filesystem_success(tmp_path: Path):
    """Verifies standard safe move on the same filesystem."""
    src = tmp_path / "source.txt"
    src.write_text("hello world payload", encoding="utf-8")
    dst = tmp_path / "target.txt"

    _move_file(src, dst)

    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == "hello world payload"
    assert not src.exists()


def test_safe_move_cross_device_success(tmp_path: Path):
    """Verifies safe copy-and-verify cross-device move."""
    src_dir = tmp_path / "src_mount"
    dst_dir = tmp_path / "dst_mount"
    src_dir.mkdir()
    dst_dir.mkdir()

    src = src_dir / "video.mp4"
    src.write_bytes(b"DATA" * 1024)
    dst = dst_dir / "video.mp4"

    _safe_cross_device_move(src, dst)

    assert dst.is_file()
    assert dst.stat().st_size == len(b"DATA" * 1024)
    assert not src.exists()


def test_safe_move_destination_collision_preflight(tmp_path: Path):
    """Verifies that an existing destination is never overwritten and raises FileExistsError."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"NEW_DATA")
    dst = tmp_path / "dst.mp4"
    dst.write_bytes(b"EXISTING_DATA")

    with pytest.raises(FileExistsError, match="Destination already exists"):
        _move_file(src, dst)

    # Destination MUST remain unchanged
    assert dst.read_bytes() == b"EXISTING_DATA"
    # Source MUST remain intact
    assert src.read_bytes() == b"NEW_DATA"


def test_safe_move_destination_collision_during_transfer(tmp_path: Path, monkeypatch):
    """Verifies that if destination appears after initial collision check, it is NEVER overwritten."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"SOURCE_PAYLOAD")
    dst = tmp_path / "dst.mp4"

    # Simulate destination appearing right after initial check / during copy
    orig_copystat = shutil.copystat
    def inject_collision(s, d, *args, **kwargs):
        # Create dst right before promotion!
        if not dst.exists():
            dst.write_bytes(b"CONCURRENT_FILE")
        return orig_copystat(s, d, *args, **kwargs)

    monkeypatch.setattr(shutil, "copystat", inject_collision)

    with pytest.raises(FileExistsError, match="Destination already exists"):
        _safe_cross_device_move(src, dst)

    # The concurrently created file must NOT be overwritten!
    assert dst.read_bytes() == b"CONCURRENT_FILE"
    # The source file must remain completely intact!
    assert src.read_bytes() == b"SOURCE_PAYLOAD"


def test_safe_move_partial_copy_failure_cleanup(tmp_path: Path, monkeypatch):
    """Verifies that an interrupted or truncated copy cleans up staging and leaves source intact."""
    src = tmp_path / "source.mp4"
    src.write_bytes(b"0" * 10000)
    dst = tmp_path / "dest.mp4"

    # Simulate copy failure / truncated write
    def failing_copyfileobj(fsrc, fdst, length=None):
        fdst.write(b"PARTIAL")
        raise IOError("Disk full or stream interrupted mid-transfer")

    monkeypatch.setattr(shutil, "copyfileobj", failing_copyfileobj)

    with pytest.raises(IOError, match="mid-transfer"):
        _safe_cross_device_move(src, dst)

    # Source must be 100% intact
    assert src.is_file()
    assert src.stat().st_size == 10000
    # Destination must NOT exist
    assert not dst.exists()
    # No temporary staging files left in target directory
    tmp_files = [f for f in tmp_path.iterdir() if f.name.startswith(".")]
    assert len(tmp_files) == 0, f"Staging file was not cleaned up: {tmp_files}"


def test_safe_move_source_deletion_failure_preserves_both(tmp_path: Path, monkeypatch):
    """Verifies that if source deletion fails after verified copy, destination is preserved and source intact."""
    src = tmp_path / "source.mp4"
    src.write_bytes(b"IMPORTANT_DATA_123")
    dst = tmp_path / "dest.mp4"

    # Simulate source unlink failure (e.g. read-only folder permission)
    orig_unlink = Path.unlink
    def failing_unlink(self, *args, **kwargs):
        if self == src:
            raise PermissionError("Permission denied: cannot delete source file")
        return orig_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(OSError, match="source deletion failed"):
        _safe_cross_device_move(src, dst)

    # Destination MUST be preserved because it is verified valid!
    assert dst.is_file()
    assert dst.read_bytes() == b"IMPORTANT_DATA_123"
    # Source MUST remain intact because deletion failed
    assert src.is_file()
    assert src.read_bytes() == b"IMPORTANT_DATA_123"


def test_safe_move_metadata_preservation_failure_does_not_destroy_copy(tmp_path: Path, monkeypatch):
    """Verifies that failure in copystat does not abort the move or destroy the verified copy."""
    src = tmp_path / "source.mp4"
    src.write_bytes(b"DATA_WITH_METADATA")
    dst = tmp_path / "dest.mp4"

    def failing_copystat(s, d, *args, **kwargs):
        raise OSError("Filesystem does not support extended attributes or timestamps")

    monkeypatch.setattr(shutil, "copystat", failing_copystat)

    # Move must still succeed!
    _safe_cross_device_move(src, dst)

    assert dst.is_file()
    assert dst.read_bytes() == b"DATA_WITH_METADATA"
    assert not src.exists()


# ==============================================================================
# 2. relocate_companions_transactionally Tests
# ==============================================================================

def _setup_companion_env(tmp_path: Path):
    """Helper to set up a video file and companion files."""
    db = tmp_path / "db.sqlite3"
    sqlite3.connect(db).close()

    old_dir = tmp_path / "OldFolder"
    new_dir = tmp_path / "NewFolder"
    old_dir.mkdir()
    new_dir.mkdir()

    v_src = old_dir / "Scene.mp4"
    v_src.write_text("video")
    v_dst = new_dir / "Renamed.mp4"

    c_jpg = old_dir / "Scene.jpg"
    c_jpg.write_text("cover")
    c_srt = old_dir / "Scene.srt"
    c_srt.write_text("subtitles")

    return db, v_src, v_dst, c_jpg, c_srt, old_dir, new_dir


def test_relocate_companions_destination_collision_preflight(tmp_path: Path):
    """Verifies that an existing companion at destination prevents moves and raises FileExistsError."""
    db, v_src, v_dst, c_jpg, c_srt, old_dir, new_dir = _setup_companion_env(tmp_path)

    # Pre-create colliding companion at destination
    (new_dir / "Renamed.srt").write_text("existing subtitles")

    with pytest.raises(FileExistsError, match="Companion destination already exists"):
        relocate_companions_transactionally(db, str(v_src), str(v_dst))

    # All source companions must remain untouched
    assert c_jpg.exists()
    assert c_srt.exists()
    # Destination companion must not be overwritten
    assert (new_dir / "Renamed.srt").read_text() == "existing subtitles"
    assert not (new_dir / "Renamed.jpg").exists()


def test_relocate_companions_move_failure_clean_rollback(tmp_path: Path, monkeypatch):
    """Verifies that a failure during forward companion move triggers clean verified rollback."""
    db, v_src, v_dst, c_jpg, c_srt, old_dir, new_dir = _setup_companion_env(tmp_path)

    # Inject failure on moving the second companion
    from librarymanager.services import filing as filing_mod
    orig_move = filing_mod._move_file

    def failing_move(s, t):
        if Path(s) == c_srt:
            raise OSError("Injected disk failure on second companion move")
        return orig_move(s, t)

    monkeypatch.setattr(filing_mod, "_move_file", failing_move)

    with pytest.raises(RuntimeError) as exc_info:
        relocate_companions_transactionally(db, str(v_src), str(v_dst))

    err_msg = str(exc_info.value)
    # Must report completed moves were restored
    assert "completed moves were restored" in err_msg

    # Verify final state: all companions restored to source, destination empty
    assert c_jpg.is_file()
    assert c_srt.is_file()
    assert not (new_dir / "Renamed.jpg").exists()
    assert not (new_dir / "Renamed.srt").exists()


def test_relocate_companions_missing_companion_before_move(tmp_path: Path, monkeypatch):
    """Verifies that if a companion disappears before its move, earlier moves roll back."""
    db, v_src, v_dst, c_jpg, c_srt, old_dir, new_dir = _setup_companion_env(tmp_path)

    # Delete c_srt before move loop processes it
    from librarymanager.services import filing as filing_mod
    orig_move = filing_mod._move_file

    def delete_on_first(s, t):
        if Path(s) == c_jpg:
            c_srt.unlink()
        return orig_move(s, t)

    monkeypatch.setattr(filing_mod, "_move_file", delete_on_first)

    with pytest.raises(RuntimeError) as exc_info:
        relocate_companions_transactionally(db, str(v_src), str(v_dst))

    assert "Companion missing before move" in str(exc_info.value)
    # c_jpg must be restored to source
    assert c_jpg.is_file()
    assert not (new_dir / "Renamed.jpg").exists()


def test_relocate_companions_destination_missing_during_rollback(tmp_path: Path, monkeypatch):
    """Verifies that if destination companion disappears before rollback, it reports rollback failure."""
    db, v_src, v_dst, c_jpg, c_srt, old_dir, new_dir = _setup_companion_env(tmp_path)

    from librarymanager.services import filing as filing_mod
    orig_move = filing_mod._move_file

    def fail_and_vanish(s, t):
        if Path(s) == c_srt:
            # Delete destination jpg that was already moved
            (new_dir / "Renamed.jpg").unlink()
            raise OSError("Injected second companion failure")
        return orig_move(s, t)

    monkeypatch.setattr(filing_mod, "_move_file", fail_and_vanish)

    with pytest.raises(RuntimeError) as exc_info:
        relocate_companions_transactionally(db, str(v_src), str(v_dst))

    err_msg = str(exc_info.value)
    # Must NEVER claim successful restoration!
    assert "completed moves were restored" not in err_msg
    # Must explicitly state destination companion missing
    assert "destination companion is missing, cannot restore to source" in err_msg
    assert "rollback failed" in err_msg


def test_relocate_companions_source_collision_during_rollback(tmp_path: Path, monkeypatch):
    """Verifies that if source companion path is re-created before rollback, it reports rollback failure."""
    db, v_src, v_dst, c_jpg, c_srt, old_dir, new_dir = _setup_companion_env(tmp_path)

    from librarymanager.services import filing as filing_mod
    orig_move = filing_mod._move_file

    def fail_and_collide(s, t):
        if Path(s) == c_srt:
            # Recreate c_jpg at source location to simulate collision
            c_jpg.write_text("COLLIDING_SOURCE")
            raise OSError("Injected second companion failure")
        return orig_move(s, t)

    monkeypatch.setattr(filing_mod, "_move_file", fail_and_collide)

    with pytest.raises(RuntimeError) as exc_info:
        relocate_companions_transactionally(db, str(v_src), str(v_dst))

    err_msg = str(exc_info.value)
    # Must NEVER claim successful restoration!
    assert "completed moves were restored" not in err_msg
    # Must explicitly report source collision
    assert "source collision already exists, cannot restore" in err_msg
    assert "rollback failed" in err_msg

    # Colliding file at source was NOT overwritten
    assert c_jpg.read_text() == "COLLIDING_SOURCE"
    # Moved file at destination is preserved
    assert (new_dir / "Renamed.jpg").is_file()


def test_relocate_companions_rollback_move_failure(tmp_path: Path, monkeypatch):
    """Verifies that an exception during rollback move is recorded and reported accurately."""
    db, v_src, v_dst, c_jpg, c_srt, old_dir, new_dir = _setup_companion_env(tmp_path)

    from librarymanager.services import filing as filing_mod
    orig_move = filing_mod._move_file

    c_jpg_moved = False
    def fail_on_c_srt_forward_and_c_jpg_rollback(s, t):
        nonlocal c_jpg_moved
        # Forward move of c_jpg succeeds
        if Path(s) == c_jpg and Path(t) == (new_dir / "Renamed.jpg"):
            c_jpg_moved = True
            return orig_move(s, t)
        # Forward move of c_srt fails
        if Path(s) == c_srt and Path(t) == (new_dir / "Renamed.srt"):
            raise OSError("Injected c_srt forward failure")
        # Rollback of c_jpg fails
        if Path(s) == (new_dir / "Renamed.jpg") and Path(t) == c_jpg:
            raise PermissionError("Injected permission error during c_jpg rollback")
        return orig_move(s, t)

    monkeypatch.setattr(filing_mod, "_move_file", fail_on_c_srt_forward_and_c_jpg_rollback)

    with pytest.raises(RuntimeError) as exc_info:
        relocate_companions_transactionally(db, str(v_src), str(v_dst))

    err_msg = str(exc_info.value)
    # Must NEVER claim successful restoration!
    assert "completed moves were restored" not in err_msg
    # Must report rollback failed with the specific error
    assert "rollback failed for" in err_msg
    assert "Injected permission error during c_jpg rollback" in err_msg

    # Destination file that could not be restored back to source is preserved!
    assert (new_dir / "Renamed.jpg").is_file()

