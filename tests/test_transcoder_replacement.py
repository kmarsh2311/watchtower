import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import librarymanager_monitor
from librarymanager_core import (connect, opensubtitles_hash, pending_filesystem_events,
                                 pending_transcoder_candidates, promote_transcoder_candidate,
                                 recent_activity, resolve_filesystem_event)
from librarymanager_monitor import (LibraryEventHandler, MoveWorker,
                                    TRANSCODER_DECISION_WINDOW_SECONDS,
                                    likely_transcoder_replacement,
                                    transcoder_replacement_decision)


def _insert(database, path, file_id='1', scene_id='10'):
    con = connect(database)
    try:
        con.execute(
            """INSERT INTO files(file_id,scene_id,path,basename,title,studio,performers_json,size,duration,
                   fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at,missing_since)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (file_id, scene_id, str(path), path.name, 'Movie', None, '[]', path.stat().st_size, 10.0,
             json.dumps([{'type': 'oshash', 'value': opensubtitles_hash(path)}]), '{}', 1,
             '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', None),
        )
        con.commit()
    finally:
        con.close()


def _run_one(worker, source, destination):
    worker.items.put((str(source), str(destination)))
    worker.items.put((None, None))
    worker.run()


def _decision_callback(timer_mock):
    calls = [call for call in timer_mock.call_args_list
             if call.args and call.args[0] == TRANSCODER_DECISION_WINDOW_SECONDS]
    assert len(calls) == 1
    return calls[0].args[1]


def _successful_stash(destination, scene_id='10'):
    stash = MagicMock()
    stash.metadata_scan.return_value = 'job-1'
    stash.wait_for_job.return_value = True
    stash.graphql_responses = [
        {'findScene': {'files': [{'id': 'replacement', 'path': str(destination),
                                  'basename': destination.name}]}},
        {'findScenes': {'scenes': [{'id': scene_id,
                                    'files': [{'id': 'replacement', 'path': str(destination)}]}]}},
    ]
    stash.call_GQL.side_effect = stash.graphql_responses
    return stash


def _source_and_destination(root, destination_name='movie encoded.mp4'):
    database = root / 'db.sqlite3'
    source = root / 'movie.mp4'
    destination = root / destination_name
    source.write_bytes(b'old source')
    _insert(database, source)
    source.unlink()
    destination.write_bytes(b'new encoded output')
    return database, source, destination


def _inventory_path(database, file_id='1'):
    con = connect(database)
    try:
        return con.execute("SELECT path FROM files WHERE file_id=?", (file_id,)).fetchone()['path']
    finally:
        con.close()


def test_likely_transcoder_name_rules_are_conservative():
    assert likely_transcoder_replacement('/a/movie.mp4', '/a/movie encoded.mp4')
    assert likely_transcoder_replacement('/a/movie.mp4', '/a/movie-hevc.mkv')
    assert likely_transcoder_replacement('/a/movie.mp4', '/a/movie.mkv')
    assert not likely_transcoder_replacement('/a/movie.mp4', '/b/movie encoded.mp4')
    assert not likely_transcoder_replacement('/a/movie.mp4', '/a/other encoded.mp4')
    assert not likely_transcoder_replacement('/a/movie.mp4', '/a/movie final.mp4')


def test_compatibility_remains_explicitly_opt_in():
    stash = MagicMock()
    with tempfile.TemporaryDirectory() as td:
        database = Path(td) / 'db.sqlite3'
        assert MoveWorker(database, stash, True, False, False).transcoder_compatibility is False
        assert MoveWorker(database, stash, True, False, True).transcoder_compatibility is True


def test_delete_first_waits_for_decision_window_then_submits_one_candidate():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database = root / 'db.sqlite3'
        source = root / 'movie.mp4'
        source.write_bytes(b'old')
        _insert(database, source)
        worker = MagicMock(transcoder_compatibility=True)
        handler = LibraryEventHandler(database, worker, False)
        source.unlink()
        with patch('librarymanager_monitor.threading.Timer') as timer:
            handler.on_deleted(MagicMock(is_directory=False, src_path=str(source)))
            destination = root / 'movie encoded.mp4'
            destination.write_bytes(b'new')
            handler.on_created(MagicMock(is_directory=False, src_path=str(destination)))
            worker.submit.assert_not_called()
            _decision_callback(timer)()
        worker.submit.assert_called_once_with(str(source), str(destination))


def test_dismissing_review_does_not_cancel_scheduled_transcoder_replacement():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database = root / 'db.sqlite3'
        source = root / 'movie.mp4'
        destination = root / 'movie encoded.mp4'
        source.write_bytes(b'old')
        _insert(database, source)
        destination.write_bytes(b'new')
        worker = MagicMock(transcoder_compatibility=True)
        handler = LibraryEventHandler(database, worker, False)
        source.unlink()
        with patch('librarymanager_monitor.threading.Timer') as timer:
            handler.on_deleted(MagicMock(is_directory=False, src_path=str(source)))
            resolve_filesystem_event(database, 'deleted', str(source))
            _decision_callback(timer)()
        worker.submit.assert_called_once_with(str(source), str(destination))


def test_delete_first_multiple_sequential_candidates_during_window_do_not_submit():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database = root / 'db.sqlite3'
        source = root / 'movie.mp4'
        source.write_bytes(b'old')
        _insert(database, source)
        worker = MagicMock(transcoder_compatibility=True)
        handler = LibraryEventHandler(database, worker, False)
        source.unlink()
        with patch('librarymanager_monitor.threading.Timer') as timer:
            handler.on_deleted(MagicMock(is_directory=False, src_path=str(source)))
            for name in ('movie encoded.mp4', 'movie-hevc.mkv'):
                candidate = root / name
                candidate.write_bytes(b'new')
                handler.on_created(MagicMock(is_directory=False, src_path=str(candidate)))
            _decision_callback(timer)()
        worker.submit.assert_not_called()
        assert 'Multiple plausible' in recent_activity(database, 1)[0]['detail']


def test_created_first_delete_later_submits_after_decision_window():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database = root / 'db.sqlite3'
        source = root / 'movie.mp4'
        destination = root / 'movie encoded.mp4'
        source.write_bytes(b'old')
        _insert(database, source)
        destination.write_bytes(b'new')
        worker = MagicMock(transcoder_compatibility=True)
        handler = LibraryEventHandler(database, worker, False)
        handler.on_created(MagicMock(is_directory=False, src_path=str(destination)))
        source.unlink()
        with patch('librarymanager_monitor.threading.Timer') as timer:
            handler.on_deleted(MagicMock(is_directory=False, src_path=str(source)))
            _decision_callback(timer)()
        worker.submit.assert_called_once_with(str(source), str(destination))


def test_created_first_candidate_is_persistent_and_not_an_amber_problem():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); database = root / 'db.sqlite3'
        source = root / 'movie.mp4'; destination = root / 'movie hevc.mkv'
        source.write_bytes(b'old'); _insert(database, source); destination.write_bytes(b'new')
        handler = LibraryEventHandler(database, MagicMock(transcoder_compatibility=True), False)
        handler.on_created(MagicMock(is_directory=False, src_path=str(destination)))
        assert pending_transcoder_candidates(database)[0]['source_path'] == str(source)
        assert pending_filesystem_events(database) == []


def test_multiple_batch_candidates_are_each_persisted():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); database = root / 'db.sqlite3'
        handler = LibraryEventHandler(database, MagicMock(transcoder_compatibility=True), False)
        for number in (1, 2):
            source = root / f'movie {number}.mp4'; candidate = root / f'movie {number} encoded.mp4'
            source.write_bytes(b'old'); _insert(database, source, str(number), str(number + 10))
            candidate.write_bytes(b'new')
            handler.on_created(MagicMock(is_directory=False, src_path=str(candidate)))
        assert len(pending_transcoder_candidates(database)) == 2


def test_candidate_deletion_clears_persistent_waiting_state():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); database = root / 'db.sqlite3'
        source = root / 'movie.mp4'; candidate = root / 'movie encoded.mp4'
        source.write_bytes(b'old'); _insert(database, source); candidate.write_bytes(b'new')
        handler = LibraryEventHandler(database, MagicMock(transcoder_compatibility=True), False)
        handler.on_created(MagicMock(is_directory=False, src_path=str(candidate)))
        candidate.unlink(); handler.on_deleted(MagicMock(is_directory=False, src_path=str(candidate)))
        assert pending_transcoder_candidates(database) == []
        assert pending_filesystem_events(database) == []


def test_restart_restores_missing_source_decision_from_persistent_candidate():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); database = root / 'db.sqlite3'
        source = root / 'movie.mp4'; candidate = root / 'movie encoded.mp4'
        source.write_bytes(b'old'); _insert(database, source); candidate.write_bytes(b'new')
        LibraryEventHandler(database, MagicMock(transcoder_compatibility=True), False).on_created(
            MagicMock(is_directory=False, src_path=str(candidate)))
        source.unlink()
        worker = MagicMock(transcoder_compatibility=True)
        restored = LibraryEventHandler(database, worker, False)
        with patch('librarymanager_monitor.threading.Timer') as timer:
            restored.restore_transcoder_candidates(); _decision_callback(timer)()
        worker.submit.assert_called_once_with(str(source), str(candidate))


def test_promoted_independent_candidate_is_not_reclassified_by_duplicate_event():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); database = root / 'db.sqlite3'
        source = root / 'movie.mp4'; candidate = root / 'movie encoded.mp4'
        source.write_bytes(b'old'); _insert(database, source); candidate.write_bytes(b'new')
        handler = LibraryEventHandler(database, MagicMock(transcoder_compatibility=True), False)
        event = MagicMock(is_directory=False, src_path=str(candidate))
        handler.on_created(event); promote_transcoder_candidate(database, str(candidate)); handler.on_created(event)
        assert pending_transcoder_candidates(database) == []
        assert pending_filesystem_events(database)[0]['source_path'] == str(candidate)


def test_moved_into_place_encoder_output_becomes_neutral_candidate():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); database = root / 'db.sqlite3'
        source = root / 'movie.mp4'; candidate = root / 'movie encoded.mp4'
        source.write_bytes(b'old'); _insert(database, source); candidate.write_bytes(b'new')
        worker = MagicMock(transcoder_compatibility=True)
        handler = LibraryEventHandler(database, worker, False)
        handler.on_moved(MagicMock(is_directory=False, src_path=str(root / 'encoder.tmp'), dest_path=str(candidate)))
        assert pending_transcoder_candidates(database)[0]['candidate_path'] == str(candidate)
        assert pending_filesystem_events(database) == []
        worker.submit.assert_not_called()


def test_disabled_compatibility_does_not_schedule_or_submit():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database = root / 'db.sqlite3'
        source = root / 'movie.mp4'
        destination = root / 'movie encoded.mp4'
        source.write_bytes(b'old')
        _insert(database, source)
        destination.write_bytes(b'new')
        worker = MagicMock(transcoder_compatibility=False)
        handler = LibraryEventHandler(database, worker, False)
        source.unlink()
        with patch('librarymanager_monitor.threading.Timer') as timer:
            handler.on_deleted(MagicMock(is_directory=False, src_path=str(source)))
        assert not [call for call in timer.call_args_list
                    if call.args and call.args[0] == TRANSCODER_DECISION_WINDOW_SECONDS]
        worker.submit.assert_not_called()


def test_preexisting_plausible_file_is_included_in_ambiguity_without_companions():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database, source, _destination = _source_and_destination(root)
        (root / 'movie.mkv').write_bytes(b'pre-existing candidate')
        selected, reason = transcoder_replacement_decision(
            database, source, {'file_id': '1', 'scene_id': '10'}
        )
        assert selected is None
        assert 'Multiple plausible' in reason


def test_destination_owned_by_another_scene_is_rejected_before_scan_without_companions():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database, source, destination = _source_and_destination(root, 'movie.mkv')
        _insert(database, destination, file_id='2', scene_id='20')
        stash = MagicMock()
        _run_one(MoveWorker(database, stash, True, False, True), source, destination)
        stash.metadata_scan.assert_not_called()
        assert 'already tracked' in recent_activity(database, 1)[0]['detail']


def test_destination_owned_by_another_file_same_scene_is_rejected_before_scan():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database, source, destination = _source_and_destination(root, 'movie.mkv')
        _insert(database, destination, file_id='2', scene_id='10')
        stash = MagicMock()
        _run_one(MoveWorker(database, stash, True, False, True), source, destination)
        stash.metadata_scan.assert_not_called()


def test_worker_revalidates_ambiguity_immediately_before_scan():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database, source, destination = _source_and_destination(root)
        (root / 'movie-hevc.mkv').write_bytes(b'another')
        stash = MagicMock()
        _run_one(MoveWorker(database, stash, True, False, True), source, destination)
        stash.metadata_scan.assert_not_called()


def test_worker_stops_if_candidate_becomes_ambiguous_during_scan():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database, source, destination = _source_and_destination(root)
        stash = _successful_stash(destination)
        def add_candidate(*_args, **_kwargs):
            (root / 'movie-hevc.mkv').write_bytes(b'racing candidate')
            return True
        stash.wait_for_job.side_effect = add_candidate
        _run_one(MoveWorker(database, stash, True, False, True), source, destination)
        assert _inventory_path(database) == str(source)


def test_worker_stops_if_local_ownership_changes_during_scan():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database, source, destination = _source_and_destination(root)
        stash = _successful_stash(destination)
        def add_owner(*_args, **_kwargs):
            _insert(database, destination, file_id='2', scene_id='20')
            return True
        stash.wait_for_job.side_effect = add_owner
        _run_one(MoveWorker(database, stash, True, False, True), source, destination)
        assert _inventory_path(database) == str(source)


def test_worker_stops_if_stash_reports_another_scene_after_scan():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database, source, destination = _source_and_destination(root)
        stash = _successful_stash(destination)
        stash.graphql_responses[1]['findScenes']['scenes'].append(
            {'id': '20', 'files': [{'id': 'other', 'path': str(destination)}]}
        )
        _run_one(MoveWorker(database, stash, True, False, True), source, destination)
        assert _inventory_path(database) == str(source)


def test_worker_rechecks_local_ownership_inside_final_inventory_transaction():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database, source, destination = _source_and_destination(root)
        stash = _successful_stash(destination)
        real_check = librarymanager_monitor.destination_inventory_conflict
        calls = 0
        transactional_calls = 0

        def conflict_at_transaction(*args, **kwargs):
            nonlocal calls, transactional_calls
            calls += 1
            if kwargs.get('connection') is not None:
                transactional_calls += 1
                return 'Destination ownership changed immediately before inventory update'
            return real_check(*args, **kwargs)

        with patch('librarymanager_monitor.destination_inventory_conflict',
                   side_effect=conflict_at_transaction):
            _run_one(MoveWorker(database, stash, True, False, True), source, destination)
        assert calls >= 3
        assert transactional_calls == 1
        stash.metadata_scan.assert_called_once()
        assert _inventory_path(database) == str(source)


def test_exactly_one_unowned_candidate_completes_reconnection_without_companions():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        database, source, destination = _source_and_destination(root)
        stash = _successful_stash(destination)
        _run_one(MoveWorker(database, stash, True, False, True), source, destination)
        stash.metadata_scan.assert_called_once_with(paths=[str(destination)])
        assert _inventory_path(database) == str(destination)


def test_successful_reconnection_clears_candidate_and_deferred_events():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); database = root / 'db.sqlite3'
        source = root / 'movie.mp4'; destination = root / 'movie encoded.mp4'
        source.write_bytes(b'old'); _insert(database, source); destination.write_bytes(b'new')
        handler = LibraryEventHandler(database, MagicMock(transcoder_compatibility=True), False)
        handler.on_created(MagicMock(is_directory=False, src_path=str(destination)))
        source.unlink(); handler.on_deleted(MagicMock(is_directory=False, src_path=str(source)))
        _run_one(MoveWorker(database, _successful_stash(destination), True, False, True), source, destination)
        assert pending_transcoder_candidates(database) == []
        assert pending_filesystem_events(database) == []
