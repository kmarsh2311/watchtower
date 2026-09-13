import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from librarymanager_core import connect, opensubtitles_hash
from librarymanager_monitor import (LibraryEventHandler, MoveWorker, likely_transcoder_replacement)


def _insert(database, path):
    con = connect(database)
    try:
        con.execute(
            """INSERT INTO files(file_id,scene_id,path,basename,title,studio,performers_json,size,duration,
                   fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at,missing_since)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ('1', '10', str(path), path.name, 'Movie', None, '[]', path.stat().st_size, 10.0,
             json.dumps([{'type': 'oshash', 'value': opensubtitles_hash(path)}]), '{}', 1,
             '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', None),
        )
        con.commit()
    finally:
        con.close()


def test_likely_transcoder_name_rules_are_conservative():
    assert likely_transcoder_replacement('/a/movie.mp4', '/a/movie encoded.mp4')
    assert likely_transcoder_replacement('/a/movie.mp4', '/a/movie-hevc.mkv')
    assert likely_transcoder_replacement('/a/movie.mp4', '/a/movie.mkv')
    assert not likely_transcoder_replacement('/a/movie.mp4', '/b/movie encoded.mp4')
    assert not likely_transcoder_replacement('/a/movie.mp4', '/a/other encoded.mp4')
    assert not likely_transcoder_replacement('/a/movie.mp4', '/a/movie final.mp4')


def test_strict_mode_rejects_changed_content_but_opt_in_accepts_candidate():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'db.sqlite3'
        old = root / 'movie.mp4'
        new = root / 'movie encoded.mp4'
        old.write_bytes(b'A' * 200000)
        _insert(db, old)
        new.write_bytes(b'B' * 100000)
        stash = MagicMock()
        strict = MoveWorker(db, stash, True, False, False)
        compat = MoveWorker(db, stash, True, False, True)
        assert strict.transcoder_compatibility is False
        assert compat.transcoder_compatibility is True


def test_delete_create_pair_is_submitted_only_when_opted_in_and_unambiguous():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'db.sqlite3'
        old = root / 'movie.mp4'
        old.write_bytes(b'A' * 200000)
        _insert(db, old)
        old.unlink()
        new = root / 'movie encoded.mp4'
        new.write_bytes(b'new encoded bytes')
        worker = MagicMock()
        worker.transcoder_compatibility = True
        handler = LibraryEventHandler(db, worker, False, incoming_worker=None)
        handler._recent_deleted_videos[str(old)] = time.monotonic()
        event = MagicMock(is_directory=False, src_path=str(new))
        handler.on_created(event)
        worker.submit.assert_called_once_with(str(old), str(new))

        worker.reset_mock()
        worker.transcoder_compatibility = False
        handler.on_created(event)
        worker.submit.assert_not_called()

def test_ambiguous_recent_deletes_do_not_auto_pair():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'db.sqlite3'
        new = root / 'movie encoded.mp4'
        new.write_bytes(b'new')
        worker = MagicMock()
        worker.transcoder_compatibility = True
        handler = LibraryEventHandler(db, worker, False, incoming_worker=None)
        now = time.monotonic()
        handler._recent_deleted_videos[str(root / 'movie.mp4')] = now
        handler._recent_deleted_videos[str(root / 'movie.mkv')] = now
        event = MagicMock(is_directory=False, src_path=str(new))
        handler.on_created(event)
        worker.submit.assert_not_called()

def test_created_output_then_deleted_original_is_submitted_when_opted_in():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'db.sqlite3'
        old = root / 'movie.mp4'
        new = root / 'movie encoded.mp4'
        old.write_bytes(b'old source')
        _insert(db, old)
        new.write_bytes(b'new encoded output')
        worker = MagicMock()
        worker.transcoder_compatibility = True
        handler = LibraryEventHandler(db, worker, False, incoming_worker=None)
        handler.on_created(MagicMock(is_directory=False, src_path=str(new)))
        worker.reset_mock()
        old.unlink()
        with patch('librarymanager_monitor.threading.Timer'):
            handler.on_deleted(MagicMock(is_directory=False, src_path=str(old)))
        worker.submit.assert_called_once_with(str(old), str(new))

def test_created_output_then_deleted_original_is_not_submitted_when_disabled():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'db.sqlite3'
        old = root / 'movie.mp4'
        new = root / 'movie encoded.mp4'
        old.write_bytes(b'old source')
        _insert(db, old)
        new.write_bytes(b'new encoded output')
        worker = MagicMock()
        worker.transcoder_compatibility = False
        handler = LibraryEventHandler(db, worker, False, incoming_worker=None)
        handler.on_created(MagicMock(is_directory=False, src_path=str(new)))
        worker.reset_mock()
        old.unlink()
        with patch('librarymanager_monitor.threading.Timer'):
            handler.on_deleted(MagicMock(is_directory=False, src_path=str(old)))
        worker.submit.assert_not_called()

def test_recent_modified_encoded_output_counts_as_replacement_candidate():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'db.sqlite3'
        old = root / 'movie.mp4'
        new = root / 'movie encoded.mp4'
        old.write_bytes(b'old source')
        _insert(db, old)
        new.write_bytes(b'partial encode')
        worker = MagicMock()
        worker.transcoder_compatibility = True
        handler = LibraryEventHandler(db, worker, False, incoming_worker=None)
        handler.on_created(MagicMock(is_directory=False, src_path=str(new)))
        handler._recent_video_candidates[str(new)] = time.monotonic() - 590
        handler.on_modified(MagicMock(is_directory=False, src_path=str(new)))
        worker.reset_mock()
        old.unlink()
        with patch('librarymanager_monitor.threading.Timer'):
            handler.on_deleted(MagicMock(is_directory=False, src_path=str(old)))
        worker.submit.assert_called_once_with(str(old), str(new))

def test_created_first_ambiguous_replacements_are_not_auto_paired():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'db.sqlite3'
        old = root / 'movie.mp4'
        first = root / 'movie encoded.mp4'
        second = root / 'movie-hevc.mkv'
        old.write_bytes(b'old source')
        _insert(db, old)
        first.write_bytes(b'a')
        second.write_bytes(b'b')
        worker = MagicMock()
        worker.transcoder_compatibility = True
        handler = LibraryEventHandler(db, worker, False, incoming_worker=None)
        handler.on_created(MagicMock(is_directory=False, src_path=str(first)))
        handler.on_created(MagicMock(is_directory=False, src_path=str(second)))
        worker.reset_mock()
        old.unlink()
        with patch('librarymanager_monitor.threading.Timer'):
            handler.on_deleted(MagicMock(is_directory=False, src_path=str(old)))
        worker.submit.assert_not_called()
