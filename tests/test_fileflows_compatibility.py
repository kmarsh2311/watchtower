import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from librarymanager_core import connect, opensubtitles_hash
from librarymanager_monitor import LibraryEventHandler, tracked_move


def _insert_tracked_file(database, path, *, file_id='1', scene_id='1'):
    fingerprint = opensubtitles_hash(path)
    con = connect(database)
    try:
        con.execute(
            '''INSERT INTO files(file_id,scene_id,path,basename,title,studio,performers_json,size,duration,
                   fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at,missing_since)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (file_id, scene_id, str(path), path.name, 'Test', None, '[]', path.stat().st_size, 10.0,
             json.dumps([{'type': 'oshash', 'value': fingerprint}]) if fingerprint else '[]', '{}', 1,
             '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', None),
        )
        con.commit()
    finally:
        con.close()


def test_transcode_with_different_size_is_not_treated_as_rename():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'watchtower.sqlite3'
        original = root / 'movie.mp4'
        transcoded = root / 'movie.mkv'
        original.write_bytes((b'A' * 65536) + (b'B' * 65536) + b'old')
        _insert_tracked_file(db, original)
        transcoded.write_bytes(b'new-hevc-output' * 10000)
        row, reason = tracked_move(db, str(original), str(transcoded))
        assert row is None
        assert 'size differs' in reason.lower()


def test_same_size_transcode_with_different_hash_is_not_treated_as_rename():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'watchtower.sqlite3'
        original = root / 'movie.mp4'
        transcoded = root / 'movie.mkv'
        original.write_bytes(b'A' * 200000)
        _insert_tracked_file(db, original)
        transcoded.write_bytes(b'B' * 200000)
        row, reason = tracked_move(db, str(original), str(transcoded))
        assert row is None
        assert 'oshash differs' in reason.lower()


def test_true_external_rename_with_identical_bytes_is_still_verified():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'watchtower.sqlite3'
        original = root / 'movie.mp4'
        renamed = root / 'renamed.mp4'
        payload = b'A' * 200000
        original.write_bytes(payload)
        _insert_tracked_file(db, original)
        renamed.write_bytes(payload)
        row, reason = tracked_move(db, str(original), str(renamed))
        assert row is not None
        assert row['scene_id'] == '1'
        assert 'verified' in reason.lower()


def test_in_place_transcode_modification_does_not_trigger_move_reconciliation():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'watchtower.sqlite3'
        video = root / 'movie.mp4'
        video.write_bytes(b'changed contents')
        worker = MagicMock()
        handler = LibraryEventHandler(db, worker, False, incoming_worker=None)
        event = MagicMock(is_directory=False, src_path=str(video))
        handler.on_modified(event)
        worker.submit.assert_not_called()


def test_delete_create_replacement_does_not_trigger_move_reconciliation():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'watchtower.sqlite3'
        video = root / 'movie.mp4'
        video.write_bytes(b'old')
        worker = MagicMock()
        handler = LibraryEventHandler(db, worker, False, incoming_worker=None)
        deleted = MagicMock(is_directory=False, src_path=str(video))
        created = MagicMock(is_directory=False, src_path=str(video))
        with patch('librarymanager_monitor.threading.Timer'):
            handler.on_deleted(deleted)
            video.write_bytes(b'new transcoded content')
            handler.on_created(created)
        worker.submit.assert_not_called()


def test_extension_changing_transcode_leaves_old_companions_untouched_when_verification_fails():
    """A real transcode is not a verified move, so Watchtower must not rename its old sidecars."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'watchtower.sqlite3'
        original = root / 'movie.mp4'
        transcoded = root / 'movie.mkv'
        compound_cover = root / 'movie.mp4.jpg'
        subtitle = root / 'movie.srt'
        funscript = root / 'movie.funscript'

        original.write_bytes(b'old-h264' * 30000)
        compound_cover.write_bytes(b'cover')
        subtitle.write_text('subtitle', encoding='utf-8')
        funscript.write_text('{"actions": []}', encoding='utf-8')
        _insert_tracked_file(db, original)

        # Simulate FileFlows producing a new HEVC file with a changed container/content.
        transcoded.write_bytes(b'new-h265-encoded-output' * 12000)
        row, reason = tracked_move(db, str(original), str(transcoded))

        assert row is None
        assert 'differs' in reason.lower()
        assert compound_cover.exists()
        assert subtitle.exists()
        assert funscript.exists()
        assert not (root / 'movie.mkv.jpg').exists()
        assert not (root / 'movie.mkv.srt').exists()
        assert not (root / 'movie.mkv.funscript').exists()
