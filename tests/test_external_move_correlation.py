import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from librarymanager_core import (
    connect,
    opensubtitles_hash,
    pending_filesystem_events,
    recent_activity,
    record_filesystem_event,
    resolve_filesystem_event,
)
from librarymanager_monitor import (
    MoveWorker,
    correlate_created_destination,
    correlate_deleted_source,
    tracked_move,
)


def _init_db(database_path):
    import librarymanager_core
    connection = connect(database_path)
    connection.executescript(librarymanager_core.SCHEMA)
    connection.close()


def _insert_file(database_path, path, file_id='1', scene_id='10', exists=1):
    con = connect(database_path)
    try:
        p = Path(path)
        size = p.stat().st_size if p.is_file() else 140000
        h = opensubtitles_hash(p) if p.is_file() else 'abc123hash'
        con.execute(
            """INSERT INTO files(file_id,scene_id,path,basename,title,studio,performers_json,size,duration,
                   fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at,missing_since)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (file_id, scene_id, str(path), p.name, 'Movie', None, '[]', size, 10.0,
             json.dumps([{'type': 'oshash', 'value': h}]), '{}', int(exists),
             '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', None),
        )
        con.commit()
    finally:
        con.close()


def test_split_move_correlation_unambiguous():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / 'inventory.sqlite3'
        _init_db(db)

        source = tmp / 'tosort' / 'scene1.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'A' * 140000)
        _insert_file(db, source, file_id='101', scene_id='1')

        destination = tmp / 'actress' / 'abc' / 'scene1.mp4'
        destination.parent.mkdir(parents=True)

        # Simulate move across folders: destination created, source deleted
        destination.write_bytes(source.read_bytes())
        source.unlink()

        # Test correlation via destination
        row, reason = correlate_created_destination(db, str(destination), candidate_sources=[str(source)])
        assert row is not None
        assert row['file_id'] == '101'
        assert row['path'] == str(source)

        # Test correlation via source
        matched_dest, reason = correlate_deleted_source(db, str(source), candidate_destinations=[str(destination)])
        assert matched_dest == str(destination)


def test_ambiguous_match_never_reconnected():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / 'inventory.sqlite3'
        _init_db(db)

        # Two different files in inventory with identical size and hash (e.g. duplicates)
        content = b'DUPLICATE_DATA_' * 10000  # 150000 bytes
        source1 = tmp / 'dir1' / 'video1.mp4'
        source1.parent.mkdir(parents=True)
        source1.write_bytes(content)
        _insert_file(db, source1, file_id='dup1', scene_id='10', exists=0)

        source2 = tmp / 'dir2' / 'video2.mp4'
        source2.parent.mkdir(parents=True)
        source2.write_bytes(content)
        _insert_file(db, source2, file_id='dup2', scene_id='20', exists=0)

        # Both source files are missing on disk
        source1.unlink()
        source2.unlink()

        destination = tmp / 'dest' / 'video.mp4'
        destination.parent.mkdir(parents=True)
        destination.write_bytes(content)

        # Must NOT reconnect because there are 2 ambiguous candidates
        row, reason = correlate_created_destination(
            db, str(destination), candidate_sources=[str(source1), str(source2)]
        )
        assert row is None
        assert 'Ambiguous' in reason


def test_multi_file_scene_missing_file_remains_unresolved():
    """One scene has multiple files. Deleting one file must NEVER be resolved by the other file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / 'inventory.sqlite3'
        _init_db(db)

        scene_dir = tmp / 'scene_multi'
        scene_dir.mkdir(parents=True)

        file1 = scene_dir / 'scene_main.mp4'
        file1.write_bytes(b'FILE1_DATA_' * 14000)

        file2 = scene_dir / 'scene_bonus.mp4'
        file2.write_bytes(b'FILE2_DATA_' * 14000)

        # Both belong to scene_id '99'
        _insert_file(db, file1, file_id='f1', scene_id='99', exists=1)
        _insert_file(db, file2, file_id='f2', scene_id='99', exists=1)

        # Now File 2 is deleted from disk
        file2.unlink()
        record_filesystem_event(db, 'deleted', str(file2), is_directory=False, initial_status='pending')

        # pending_filesystem_events must show File 2 as unresolved
        # (File 1 existing for scene 99 must NOT resolve File 2)
        pending = pending_filesystem_events(db)
        assert len(pending) == 1
        assert pending[0]['source_path'] == str(file2)
        assert pending[0]['file_id'] == 'f2'

        # Now simulate File 2 being properly reconnected to a new path in Stash
        new_file2 = tmp / 'reconnected' / 'scene_bonus.mp4'
        new_file2.parent.mkdir(parents=True)
        new_file2.write_bytes(b'FILE2_DATA_' * 14000)

        con = connect(db)
        con.execute(
            """INSERT INTO activity_log(category,severity,action,status,scene_id,file_id,old_path,new_path,recorded_at)
               VALUES ('reconciliation','info','targeted Stash scan','updated','99','f2',?,?,?)""",
            (str(file2), str(new_file2), '2026-01-01T00:00:00+00:00')
        )
        con.execute(
            "UPDATE files SET path=?, exists_on_disk=1 WHERE file_id='f2'",
            (str(new_file2),)
        )
        con.commit()
        con.close()

        # Now that File 2 itself exists at its new path, it resolves
        pending_after = pending_filesystem_events(db)
        assert len(pending_after) == 0


def test_reconnected_external_move_resolves_warnings_and_preserves_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / 'inventory.sqlite3'
        _init_db(db)

        source = tmp / 'tosort' / 'scene.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'VIDEO_PAYLOAD_' * 14000)
        _insert_file(db, source, file_id='rec1', scene_id='55', exists=1)

        destination = tmp / 'actress' / 'abc' / 'scene.mp4'
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        source.unlink()

        # Both deleted and created events were recorded as pending
        record_filesystem_event(db, 'deleted', str(source), is_directory=False, initial_status='pending')
        record_filesystem_event(db, 'created', str(destination), is_directory=False, initial_status='pending')

        # Initial pending events = 2
        assert len(pending_filesystem_events(db)) == 2

        # Mock Stash scan confirming destination
        stash = MagicMock()
        stash.metadata_scan.return_value = 'job-123'
        stash.wait_for_job.return_value = True
        stash.call_GQL.return_value = {
            'findScene': {'files': [{'id': 'rec1', 'path': str(destination), 'basename': destination.name}]}
        }

        # Run MoveWorker with automaticMoveReconciliation enabled
        worker = MoveWorker(db, stash, enabled=True, notifications=False)
        worker.items.put((str(source), str(destination)))
        worker.items.put((None, None))
        worker.run()

        # Warnings are automatically resolved
        assert len(pending_filesystem_events(db)) == 0

        # Activity log preserves full audit history
        acts = recent_activity(db, limit=10)
        assert any(a['status'] == 'updated' and a['new_path'] == str(destination) for a in acts)
        assert any(a['status'] == 'verified' and a['old_path'] == str(source) for a in acts)


def test_verified_compound_companion_move_resolves_and_records_activity():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / 'inventory.sqlite3'
        _init_db(db)

        # Video exists at destination
        dest_dir = tmp / 'actress' / 'abc'
        dest_dir.mkdir(parents=True)
        video = dest_dir / 'scene.mp4'
        video.write_bytes(b'VIDEO_PAYLOAD_' * 14000)
        _insert_file(db, video, file_id='c_vid_1', scene_id='77', exists=1)

        # Companion moved to destination
        src_comp = tmp / 'tosort' / 'scene.mp4.jpg'
        src_comp.parent.mkdir(parents=True)
        dest_comp = dest_dir / 'scene.mp4.jpg'
        dest_comp.write_bytes(b'JPEG_DATA_' * 100)

        # Record companion move event
        record_filesystem_event(db, 'moved', str(src_comp), str(dest_comp), is_directory=False, initial_status='pending')

        # pending_filesystem_events must auto-resolve it because destination has active companion video
        pending = pending_filesystem_events(db)
        assert len(pending) == 0


def test_orphan_companion_move_remains_unresolved():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / 'inventory.sqlite3'
        _init_db(db)

        # Destination folder has NO matching video
        dest_dir = tmp / 'orphan_dir'
        dest_dir.mkdir(parents=True)
        src_comp = tmp / 'tosort' / 'orphan.mp4.jpg'
        src_comp.parent.mkdir(parents=True)
        dest_comp = dest_dir / 'orphan.mp4.jpg'
        dest_comp.write_bytes(b'JPEG_DATA_' * 100)

        record_filesystem_event(db, 'moved', str(src_comp), str(dest_comp), is_directory=False, initial_status='pending')

        # Must remain unresolved because there is no matching video in files
        pending = pending_filesystem_events(db)
        assert len(pending) == 1
        assert pending[0]['destination_path'] == str(dest_comp)


def test_ambiguous_companion_move_remains_unresolved():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / 'inventory.sqlite3'
        _init_db(db)

        # Destination folder has TWO matching videos with same stem
        dest_dir = tmp / 'dupe_dir'
        dest_dir.mkdir(parents=True)
        v1 = dest_dir / 'movie.mp4'
        v1.write_bytes(b'V1_' * 10000)
        v2 = dest_dir / 'movie.mkv'
        v2.write_bytes(b'V2_' * 10000)
        _insert_file(db, v1, file_id='m1', scene_id='1', exists=1)
        _insert_file(db, v2, file_id='m2', scene_id='2', exists=1)

        src_comp = tmp / 'tosort' / 'movie.jpg'
        src_comp.parent.mkdir(parents=True)
        dest_comp = dest_dir / 'movie.jpg'
        dest_comp.write_bytes(b'JPEG_' * 100)

        record_filesystem_event(db, 'moved', str(src_comp), str(dest_comp), is_directory=False, initial_status='pending')

        # Ambiguous matching stem -> must remain unresolved
        pending = pending_filesystem_events(db)
        assert len(pending) == 1


def test_failed_companion_move_remains_unresolved():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / 'inventory.sqlite3'
        _init_db(db)

        # Destination file does not exist (failed move)
        dest_comp = tmp / 'nowhere' / 'scene.mp4.jpg'
        src_comp = tmp / 'tosort' / 'scene.mp4.jpg'
        src_comp.parent.mkdir(parents=True)
        src_comp.write_bytes(b'DATA')

        record_filesystem_event(db, 'moved', str(src_comp), str(dest_comp), is_directory=False, initial_status='pending')

        pending = pending_filesystem_events(db)
        assert len(pending) == 1


from librarymanager_monitor import CompletedDownloadWorker, LibraryEventHandler


class DummyEvent:
    def __init__(self, src_path, dest_path=None, is_directory=False):
        self.src_path = str(src_path)
        self.dest_path = str(dest_path) if dest_path else None
        self.is_directory = is_directory


def test_existing_video_moved_to_incoming_folder_does_not_enter_download_queue():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        incoming_dir = tmp / "incoming"
        incoming_dir.mkdir(parents=True)
        library_dir = tmp / "library"
        library_dir.mkdir(parents=True)

        source = library_dir / "scene1.mp4"
        source.write_bytes(b"EXISTING_VIDEO_" * 10000)
        _insert_file(db, source, file_id="vid_orig", scene_id="400", exists=1)

        dest = incoming_dir / "scene1.mp4"
        dest.write_bytes(source.read_bytes())
        source.unlink()

        stash = MagicMock()
        stash.metadata_scan.return_value = "job-move-1"
        stash.wait_for_job.return_value = True
        stash.call_GQL.return_value = {
            "findScene": {"files": [{"id": "vid_orig", "path": str(dest), "basename": dest.name}]}
        }

        move_worker = MoveWorker(db, stash, enabled=True, notifications=False)
        incoming_worker = CompletedDownloadWorker(
            db, stash, incoming_folder=str(incoming_dir), enabled=True,
            settle_seconds=300, notifications=False, fallback_seconds=60,
            incoming_folders=[str(incoming_dir)]
        )

        handler = LibraryEventHandler(
            database_path=db,
            worker=move_worker,
            notifications=False,
            incoming_worker=incoming_worker
        )

        # Emit on_moved from library into incoming folder
        event = DummyEvent(src_path=source, dest_path=dest)
        handler.on_moved(event)

        # 1. Verify destination was NOT added to incoming_worker candidate queue
        assert str(dest.resolve()) not in incoming_worker.candidates
        assert len(incoming_worker.candidates) == 0

        # 2. Verify MoveWorker received the move and successfully reconciled
        move_worker.items.put((None, None))
        move_worker.run()

        # Stash scan was called only by MoveWorker (exactly 1 metadata_scan call)
        assert stash.metadata_scan.call_count == 1
        assert stash.metadata_scan.call_args[1]["paths"] == [str(dest)]

        # No pending warnings remain
        assert len(pending_filesystem_events(db)) == 0


def test_genuinely_new_download_processes_normally():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        incoming_dir = tmp / "incoming"
        incoming_dir.mkdir(parents=True)

        new_download = incoming_dir / "brand_new_scene.mp4"
        new_download.write_bytes(b"BRAND_NEW_DATA_" * 10000)

        stash = MagicMock()
        incoming_worker = CompletedDownloadWorker(
            db, stash, incoming_folder=str(incoming_dir), enabled=True,
            settle_seconds=300, notifications=False, fallback_seconds=60,
            incoming_folders=[str(incoming_dir)]
        )

        # Submit new download
        accepted = incoming_worker.submit(str(new_download))
        assert accepted is True
        assert str(new_download.resolve()) in incoming_worker.candidates
        assert incoming_worker.candidates[str(new_download.resolve())]["last_saved_status"] == "waiting"


def test_ambiguous_matches_do_not_reconnect_or_discard_new_download():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        incoming_dir = tmp / "incoming"
        incoming_dir.mkdir(parents=True)

        content = b"AMBIGUOUS_DATA_" * 10000
        src1 = tmp / "lib1" / "dup1.mp4"
        src1.parent.mkdir(parents=True)
        src1.write_bytes(content)
        _insert_file(db, src1, file_id="f_dup1", scene_id="1", exists=0)

        src2 = tmp / "lib2" / "dup2.mp4"
        src2.parent.mkdir(parents=True)
        src2.write_bytes(content)
        _insert_file(db, src2, file_id="f_dup2", scene_id="2", exists=0)

        src1.unlink()
        src2.unlink()

        # A new download arrives in incoming with identical content
        incoming_file = incoming_dir / "maybe_new.mp4"
        incoming_file.write_bytes(content)

        stash = MagicMock()
        incoming_worker = CompletedDownloadWorker(
            db, stash, incoming_folder=str(incoming_dir), enabled=True,
            settle_seconds=300, notifications=False, fallback_seconds=60,
            incoming_folders=[str(incoming_dir)]
        )

        # Because 2 missing library files match, it is ambiguous.
        # It must NOT be rejected as a verified library move, and must NOT be discarded.
        accepted = incoming_worker.submit(str(incoming_file))
        assert accepted is True
        assert str(incoming_file.resolve()) in incoming_worker.candidates
