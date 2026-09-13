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
