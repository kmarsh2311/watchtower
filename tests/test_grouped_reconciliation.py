"""Batch 1 tests for durable grouped file-reconciliation state."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from librarymanager_core import SCHEMA, connect, pending_filesystem_events, record_filesystem_event
from librarymanager_reconciliation import (
    RECONCILIATION_SCHEMA,
    ReconciliationCoordinator,
    add_batch_member,
    batch_snapshot,
    claim_batch,
    create_or_get_batch,
    detect_settled_operations,
    dismiss_review_batch,
    link_event_to_batch,
    list_review_batches,
    transition_batch,
)
from librarymanager_monitor import GroupedReconciliationWorker, LibraryEventHandler


def _seed_file(database, path, file_id, scene_id, size):
    connection = connect(database)
    connection.execute(
        """INSERT INTO files(
               file_id,scene_id,path,basename,size,fingerprints_json,exists_on_disk,
               first_seen_at,last_seen_at
           ) VALUES (?,?,?,?,?,'[]',1,'before','before')""",
        (str(file_id), str(scene_id), str(path), Path(path).name, int(size)),
    )
    connection.commit()
    connection.close()


def _record_event(database, event_type, source, destination=None, is_directory=False):
    record_filesystem_event(
        database, event_type, str(source), str(destination) if destination else None,
        is_directory=is_directory,
    )


def _settled_now():
    return time.time() + 60.0


def test_additive_schema_preserves_existing_inventory(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    legacy = sqlite3.connect(database)
    legacy.executescript(SCHEMA.removesuffix(RECONCILIATION_SCHEMA))
    legacy.execute(
        """INSERT INTO files(
               file_id,scene_id,path,basename,exists_on_disk,first_seen_at,last_seen_at
           ) VALUES ('file-1','scene-1','/library/a.mp4','a.mp4',1,'before','before')"""
    )
    legacy.commit()
    legacy.close()

    connection = connect(database)
    row = connection.execute("SELECT * FROM files WHERE file_id='file-1'").fetchone()
    tables = {entry[0] for entry in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    connection.close()

    assert row["scene_id"] == "scene-1"
    assert "grouped_reconciliation_batches" in tables
    assert "grouped_reconciliation_members" in tables
    assert "grouped_reconciliation_event_links" in tables


def test_batch_members_and_event_links_are_idempotent(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    connect(database).close()
    record_filesystem_event(database, "deleted", "/old/folder/a.mp4")
    connection = connect(database)
    event_key = connection.execute("SELECT event_key FROM filesystem_events").fetchone()[0]
    connection.close()

    first = create_or_get_batch(
        database,
        correlation_key="folder:/old/folder=>/new/folder",
        operation_type="folder_move",
        source_prefix="/old/folder",
        destination_prefix="/new/folder",
        evidence={"event_count": 2},
    )
    second = create_or_get_batch(
        database,
        correlation_key="folder:/old/folder=>/new/folder",
        operation_type="folder_move",
    )
    assert second["id"] == first["id"]

    for _ in range(2):
        add_batch_member(
            database,
            first["id"],
            file_id="file-1",
            scene_id="scene-1",
            old_path="/old/folder/a.mp4",
            relative_path="a.mp4",
            expected_path="/new/folder/a.mp4",
            expected_size=123,
            source_fingerprints=[{"type": "oshash", "value": "abc"}],
        )
    assert link_event_to_batch(database, first["id"], event_key) is True
    assert link_event_to_batch(database, first["id"], event_key) is False

    snapshot = batch_snapshot(database, first["id"])
    assert snapshot["tracked_count"] == 1
    assert len(snapshot["members"]) == 1
    assert snapshot["members"][0]["file_id"] == "file-1"
    assert json.loads(snapshot["members"][0]["source_fingerprints_json"])[0]["type"] == "oshash"
    assert snapshot["event_keys"] == [event_key]


def test_one_event_cannot_belong_to_two_batches(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    connect(database).close()
    record_filesystem_event(database, "created", "/new/folder/a.mp4")
    connection = connect(database)
    event_key = connection.execute("SELECT event_key FROM filesystem_events").fetchone()[0]
    connection.close()
    first = create_or_get_batch(database, correlation_key="first", operation_type="folder_move")
    second = create_or_get_batch(database, correlation_key="second", operation_type="folder_copy")

    assert link_event_to_batch(database, first["id"], event_key) is True
    with pytest.raises(ValueError, match="already belongs"):
        link_event_to_batch(database, second["id"], event_key)


def test_state_transitions_are_atomic_and_guarded(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    batch = create_or_get_batch(database, correlation_key="state-test", operation_type="folder_move")

    settling = transition_batch(
        database, batch["id"], "settling", expected_state="collecting"
    )
    assert settling["state"] == "settling"
    with pytest.raises(RuntimeError, match="changed"):
        transition_batch(
            database, batch["id"], "ready_for_review", expected_state="collecting"
        )
    with pytest.raises(ValueError, match="Invalid reconciliation transition"):
        transition_batch(database, batch["id"], "scanning", expected_state="settling")


def test_claims_are_single_owner_and_recover_after_expiry(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    batch = create_or_get_batch(database, correlation_key="lease-test", operation_type="bulk_move")

    assert claim_batch(database, batch["id"], "monitor-a", lease_seconds=30, now=100.0) is True
    assert claim_batch(database, batch["id"], "monitor-b", lease_seconds=30, now=110.0) is False

    coordinator = ReconciliationCoordinator(database, "monitor-b")
    unfinished = coordinator.recover_after_restart(now=131.0)
    assert [item["id"] for item in unfinished] == [batch["id"]]
    assert claim_batch(database, batch["id"], "monitor-b", lease_seconds=30, now=131.0) is True

    snapshot = batch_snapshot(database, batch["id"])
    assert snapshot["claimed_by"] == "monitor-b"
    assert coordinator.stop() == 1
    assert batch_snapshot(database, batch["id"])["claimed_by"] is None


def test_restart_preserves_member_identity_and_intermediate_state(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    batch = create_or_get_batch(
        database,
        correlation_key="restart-test",
        operation_type="folder_move",
        source_prefix="/source",
        destination_prefix="/destination",
    )
    add_batch_member(
        database,
        batch["id"],
        file_id="file-117",
        scene_id="scene-63",
        old_path="/source/nested/video.mp4",
        relative_path="nested/video.mp4",
        expected_path="/destination/nested/video.mp4",
    )
    transition_batch(database, batch["id"], "settling", expected_state="collecting")

    restarted = ReconciliationCoordinator(database, "new-monitor")
    recovered = restarted.recover_after_restart()
    assert len(recovered) == 1
    snapshot = batch_snapshot(database, recovered[0]["id"])
    assert snapshot["state"] == "settling"
    assert snapshot["members"][0]["file_id"] == "file-117"
    assert snapshot["members"][0]["scene_id"] == "scene-63"
    assert snapshot["members"][0]["expected_path"] == "/destination/nested/video.mp4"


def test_native_directory_move_builds_one_read_only_batch(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "Library" / "Studio"
    destination = tmp_path / "Archive" / "Studio"
    destination.mkdir(parents=True)
    for index in range(3):
        old_path = source / "Disc" / f"video-{index}.mp4"
        new_path = destination / "Disc" / f"video-{index}.mp4"
        new_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (f"video-{index}" * 20).encode()
        new_path.write_bytes(payload)
        _seed_file(database, old_path, f"f{index}", f"s{index}", len(payload))
    _record_event(database, "moved", source, destination, is_directory=True)

    batches = detect_settled_operations(database, settle_seconds=10, now=_settled_now())

    assert len(batches) == 1
    batch = batches[0]
    assert batch["operation_type"] == "folder_move"
    assert batch["source_prefix"] == str(source)
    assert batch["destination_prefix"] == str(destination)
    assert batch["tracked_count"] == 3
    assert batch["state"] == "ready_for_review"
    assert {member["relative_path"] for member in batch["members"]} == {
        "Disc/video-0.mp4", "Disc/video-1.mp4", "Disc/video-2.mp4"
    }
    assert all(member["state"] == "ready" for member in batch["members"])
    connection = connect(database)
    assert connection.execute(
        "SELECT status FROM filesystem_events"
    ).fetchone()[0] == "pending"
    connection.close()


@pytest.mark.parametrize("created_first", [True, False])
def test_cross_volume_create_delete_order_becomes_one_folder_move(tmp_path, created_first):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "VolumeA" / "Collection"
    destination = tmp_path / "VolumeB" / "Collection"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    operations = []
    for index in range(3):
        old_path = source / f"scene-{index}.mp4"
        new_path = destination / f"scene-{index}.mp4"
        payload = (f"payload-{index}" * 50).encode()
        old_path.write_bytes(payload)
        _seed_file(database, old_path, f"file-{index}", f"scene-{index}", len(payload))
        new_path.write_bytes(payload)
        old_path.unlink()
        operations.append((old_path, new_path))
    for old_path, new_path in operations:
        if created_first:
            _record_event(database, "created", new_path)
            _record_event(database, "deleted", old_path)
        else:
            _record_event(database, "deleted", old_path)
            _record_event(database, "created", new_path)

    batches = detect_settled_operations(database, settle_seconds=10, now=_settled_now())

    assert len(batches) == 1
    assert batches[0]["operation_type"] == "folder_move"
    assert batches[0]["tracked_count"] == 3
    assert len(batches[0]["event_keys"]) == 6
    assert all(member["state"] == "ready" for member in batches[0]["members"])


def test_delayed_event_waves_join_same_folder_move(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "Library" / "Old Name"
    destination = tmp_path / "Library" / "New Name"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    paths = []
    for index in range(4):
        old_path = source / f"scene-{index}.mp4"
        payload = (f"wave-{index}" * 40).encode()
        old_path.write_bytes(payload)
        _seed_file(database, old_path, f"wave-file-{index}", f"wave-scene-{index}", len(payload))
        paths.append((old_path, payload))

    for old_path, payload in paths[:2]:
        new_path = destination / old_path.name
        new_path.write_bytes(payload)
        old_path.unlink()
        _record_event(database, "created", new_path)
        _record_event(database, "deleted", old_path)
    first = detect_settled_operations(database, settle_seconds=10, now=_settled_now())
    assert len(first) == 1
    assert first[0]["tracked_count"] == 2

    for old_path, payload in paths[2:]:
        new_path = destination / old_path.name
        new_path.write_bytes(payload)
        old_path.unlink()
        _record_event(database, "created", new_path)
        _record_event(database, "deleted", old_path)
    second = detect_settled_operations(database, settle_seconds=10, now=_settled_now())

    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["tracked_count"] == 4


def test_folder_copy_is_distinguished_while_originals_remain(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "Library" / "Original"
    destination = tmp_path / "Library" / "Copy"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    for index in range(2):
        old_path = source / f"clip-{index}.mp4"
        new_path = destination / f"clip-{index}.mp4"
        payload = (f"copy-{index}" * 30).encode()
        old_path.write_bytes(payload)
        new_path.write_bytes(payload)
        _seed_file(database, old_path, f"cf{index}", f"cs{index}", len(payload))
        _record_event(database, "created", new_path)

    batches = detect_settled_operations(database, settle_seconds=10, now=_settled_now())

    assert len(batches) == 1
    assert batches[0]["operation_type"] == "folder_copy"
    assert batches[0]["state"] == "ready_for_review"
    assert all(member["state"] == "ready" for member in batches[0]["members"])


def test_selected_subset_is_a_bulk_move_and_does_not_claim_unmoved_file(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "Library" / "Mixed"
    destination = tmp_path / "Library" / "Selected"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    paths = []
    for index in range(3):
        old_path = source / f"item-{index}.mp4"
        payload = (f"bulk-{index}" * 40).encode()
        old_path.write_bytes(payload)
        _seed_file(database, old_path, f"bf{index}", f"bs{index}", len(payload))
        paths.append((old_path, payload))
    for old_path, payload in paths[:2]:
        new_path = destination / old_path.name
        new_path.write_bytes(payload)
        old_path.unlink()
        _record_event(database, "created", new_path)
        _record_event(database, "deleted", old_path)

    batches = detect_settled_operations(database, settle_seconds=10, now=_settled_now())

    assert len(batches) == 1
    assert batches[0]["operation_type"] == "bulk_move"
    assert {member["file_id"] for member in batches[0]["members"]} == {"bf0", "bf1"}
    assert "bf2" not in {member["file_id"] for member in batches[0]["members"]}


def test_ambiguous_created_candidate_is_not_grouped(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    destination = tmp_path / "Destination" / "same.mp4"
    destination.parent.mkdir()
    destination.write_bytes(b"x" * 200)
    for index in range(2):
        old_path = tmp_path / f"Source{index}" / "same.mp4"
        old_path.parent.mkdir()
        _seed_file(database, old_path, f"af{index}", f"as{index}", 200)
        _record_event(database, "deleted", old_path)
    _record_event(database, "created", destination)

    assert detect_settled_operations(
        database, settle_seconds=10, now=_settled_now()
    ) == []


def test_unsettled_or_offline_events_do_not_create_batches(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "Offline" / "Folder"
    destination = tmp_path / "Online" / "Folder"
    connect(database).close()
    _record_event(database, "moved", source, destination, is_directory=True)

    assert detect_settled_operations(
        database, settle_seconds=120, now=time.time()
    ) == []

    connection = connect(database)
    connection.execute(
        """UPDATE filesystem_monitor_status
           SET unavailable_roots_json=? WHERE id=1""",
        (json.dumps([str(source.parent)]),),
    )
    connection.commit()
    connection.close()
    assert detect_settled_operations(
        database, settle_seconds=0, now=_settled_now()
    ) == []


def test_repeated_detection_is_idempotent_and_does_not_resolve_events(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "Old"
    destination = tmp_path / "New"
    destination.mkdir()
    for index in range(2):
        old_path = source / f"video-{index}.mp4"
        new_path = destination / f"video-{index}.mp4"
        payload = bytes([index + 1]) * 300
        new_path.write_bytes(payload)
        _seed_file(database, old_path, f"rf{index}", f"rs{index}", len(payload))
        _record_event(database, "moved", old_path, new_path)

    first = detect_settled_operations(database, settle_seconds=0, now=_settled_now())
    second = detect_settled_operations(database, settle_seconds=0, now=_settled_now())
    assert len(first) == 1
    assert second == []
    connection = connect(database)
    assert connection.execute(
        "SELECT COUNT(*) FROM grouped_reconciliation_batches"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM filesystem_events WHERE status='pending'"
    ).fetchone()[0] == 2
    connection.close()


def test_directory_move_claims_related_child_events(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "Old" / "Studio"
    destination = tmp_path / "New" / "Studio"
    destination.mkdir(parents=True)
    for index in range(2):
        old_path = source / f"video-{index}.mp4"
        new_path = destination / f"video-{index}.mp4"
        payload = bytes([index + 3]) * 100
        new_path.write_bytes(payload)
        _seed_file(database, old_path, f"df{index}", f"ds{index}", len(payload))
        _record_event(database, "deleted", old_path)
        _record_event(database, "created", new_path)
    unrelated = destination / "unrelated.mp4"
    unrelated.write_bytes(b"not in the Stash inventory")
    _record_event(database, "created", unrelated)
    _record_event(database, "moved", source, destination, is_directory=True)

    batches = detect_settled_operations(database, settle_seconds=0, now=_settled_now())

    assert len(batches) == 1
    assert batches[0]["operation_type"] == "folder_move"
    assert len(batches[0]["event_keys"]) == 5
    assert detect_settled_operations(database, settle_seconds=0, now=_settled_now()) == []
    connection = connect(database)
    assert connection.execute(
        """SELECT COUNT(*) FROM filesystem_events e
           LEFT JOIN grouped_reconciliation_event_links l ON l.event_key=e.event_key
           WHERE e.source_path=? AND l.event_key IS NULL""",
        (str(unrelated),),
    ).fetchone()[0] == 1
    connection.close()


def test_settling_batch_can_resume_after_detector_restart(tmp_path, monkeypatch):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "Before"
    destination = tmp_path / "After"
    destination.mkdir()
    for index in range(2):
        old_path = source / f"clip-{index}.mp4"
        new_path = destination / f"clip-{index}.mp4"
        payload = bytes([index + 7]) * 80
        new_path.write_bytes(payload)
        _seed_file(database, old_path, f"restart-f{index}", f"restart-s{index}", len(payload))
        _record_event(database, "moved", old_path, new_path)

    import librarymanager_reconciliation as reconciliation
    original_add = reconciliation.add_batch_member
    interrupted = {"raised": False}

    def interrupt_once(*args, **kwargs):
        if not interrupted["raised"]:
            interrupted["raised"] = True
            raise RuntimeError("simulated monitor termination")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(reconciliation, "add_batch_member", interrupt_once)
    with pytest.raises(RuntimeError, match="simulated monitor termination"):
        detect_settled_operations(database, settle_seconds=0, now=_settled_now())
    monkeypatch.setattr(reconciliation, "add_batch_member", original_add)

    recovered = detect_settled_operations(database, settle_seconds=0, now=_settled_now())
    assert len(recovered) == 1
    assert recovered[0]["state"] == "ready_for_review"
    assert recovered[0]["tracked_count"] == 2


def test_batch_resumes_if_monitor_stops_after_events_are_linked(tmp_path, monkeypatch):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "LinkedBefore"
    destination = tmp_path / "LinkedAfter"
    destination.mkdir()
    for index in range(2):
        old_path = source / f"linked-{index}.mp4"
        new_path = destination / f"linked-{index}.mp4"
        payload = bytes([index + 11]) * 90
        new_path.write_bytes(payload)
        _seed_file(database, old_path, f"linked-f{index}", f"linked-s{index}", len(payload))
        _record_event(database, "moved", old_path, new_path)

    import librarymanager_reconciliation as reconciliation
    original_transition = reconciliation.transition_batch
    interrupted = {"raised": False}

    def interrupt_ready(database_path, batch_id, new_state, **kwargs):
        if new_state == "ready_for_review" and not interrupted["raised"]:
            interrupted["raised"] = True
            raise RuntimeError("simulated termination after event links")
        return original_transition(database_path, batch_id, new_state, **kwargs)

    monkeypatch.setattr(reconciliation, "transition_batch", interrupt_ready)
    with pytest.raises(RuntimeError, match="after event links"):
        detect_settled_operations(database, settle_seconds=0, now=_settled_now())
    connection = connect(database)
    assert connection.execute(
        "SELECT COUNT(*) FROM grouped_reconciliation_event_links"
    ).fetchone()[0] == 2
    connection.close()
    monkeypatch.setattr(reconciliation, "transition_batch", original_transition)

    recovered = detect_settled_operations(database, settle_seconds=0, now=_settled_now())
    assert len(recovered) == 1
    assert recovered[0]["state"] == "ready_for_review"
    assert recovered[0]["tracked_count"] == 2


def test_monitor_records_external_directory_move_for_grouping(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    connect(database).close()
    source = tmp_path / "Library" / "Old"
    destination = tmp_path / "Library" / "New"
    handler = LibraryEventHandler(database, SimpleNamespace(), False)

    handler.on_moved(SimpleNamespace(
        src_path=str(source), dest_path=str(destination), is_directory=True
    ))

    connection = connect(database)
    event = connection.execute(
        "SELECT event_type,source_path,destination_path,is_directory,status FROM filesystem_events"
    ).fetchone()
    connection.close()
    assert tuple(event) == ("moved", str(source), str(destination), 1, "pending")


def test_grouped_worker_uses_bounded_metadata_probe(tmp_path, monkeypatch):
    path = tmp_path / "Library" / "video.mp4"
    path.parent.mkdir()
    path.write_bytes(b"metadata only")
    calls = []

    class FakeLimiter:
        def run(self, root, function, args=(), timeout=0):
            calls.append((root, timeout))
            return function(*args), "ok"

    class FakeCoordinator:
        def __init__(self):
            self.arguments = None

        def detect_settled_events(self, **kwargs):
            self.arguments = kwargs
            return ["detected"]

    import librarymanager_monitor as monitor
    monkeypatch.setattr(monitor, "default_fs_limiter", FakeLimiter())
    coordinator = FakeCoordinator()
    worker = GroupedReconciliationWorker(coordinator, [path.parent])

    result = worker.process_once()
    probe_result = coordinator.arguments["path_probe"](str(path))

    assert result == ["detected"]
    assert coordinator.arguments["max_events"] == 250
    assert probe_result["size"] == len(b"metadata only")
    assert probe_result["is_file"] is True
    assert calls == [(str(path.parent), 1.0)]


def test_grouped_review_replaces_child_alerts_and_dismisses_without_file_changes(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    source = tmp_path / "Old" / "Collection"
    destination = tmp_path / "New" / "Collection"
    destination.mkdir(parents=True)
    destinations = []
    for index in range(2):
        old_path = source / f"scene-{index}.mp4"
        new_path = destination / f"scene-{index}.mp4"
        payload = bytes([index + 20]) * 256
        new_path.write_bytes(payload)
        destinations.append((new_path, payload))
        _seed_file(database, old_path, f"review-f{index}", f"review-s{index}", len(payload))
        _record_event(database, "deleted", old_path)
        _record_event(database, "created", new_path)

    detected = detect_settled_operations(database, settle_seconds=0, now=_settled_now())
    assert len(detected) == 1
    reviews = list_review_batches(database)
    assert len(reviews) == 1
    assert reviews[0]["tracked_count"] == 2
    assert {member["file_id"] for member in reviews[0]["members"]} == {"review-f0", "review-f1"}
    assert pending_filesystem_events(database) == []

    result = dismiss_review_batch(database, reviews[0]["id"])

    assert result["state"] == "dismissed"
    assert list_review_batches(database) == []
    for new_path, payload in destinations:
        assert new_path.read_bytes() == payload
    connection = connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM filesystem_events WHERE status='pending'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM filesystem_events WHERE status='reviewed'"
        ).fetchone()[0] == 4
    finally:
        connection.close()


def test_grouped_review_preserves_uncertain_member_reason(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    connect(database).close()
    batch = create_or_get_batch(
        database, correlation_key="uncertain-review", operation_type="folder_move",
        source_prefix="/old", destination_prefix="/new", state="needs_attention",
    )
    add_batch_member(
        database, batch["id"], file_id="file-9", scene_id="scene-9",
        old_path="/old/video.mp4", relative_path="video.mp4",
        expected_path="/new/video.mp4", member_state="uncertain",
        reason="Expected destination file is not currently available",
    )

    review = list_review_batches(database)[0]
    assert review["members"][0]["scene_id"] == "scene-9"
    assert review["members"][0]["expected_path"] == "/new/video.mp4"
    assert review["members"][0]["reason"] == "Expected destination file is not currently available"


def test_grouped_dismiss_rejects_non_review_state(tmp_path):
    database = tmp_path / "watchtower.sqlite3"
    batch = create_or_get_batch(
        database, correlation_key="still-collecting", operation_type="folder_move"
    )
    with pytest.raises(ValueError, match="not awaiting review"):
        dismiss_review_batch(database, batch["id"])


class _GroupedScanStash:
    def __init__(self, scene_files, owners=None, *, completed=True):
        self.scene_files = {str(key): value for key, value in scene_files.items()}
        self.owners = owners or {}
        self.completed = completed
        self.scan_calls = []
        self.wait_calls = []

    def metadata_scan(self, *, paths):
        self.scan_calls.append(list(paths))
        return "group-job-1"

    def wait_for_job(self, job_id, timeout):
        self.wait_calls.append((str(job_id), timeout))
        return self.completed

    def call_GQL(self, query, variables):
        if "findScenes(" in query:
            path = str(variables["path"])
            scene_ids = self.owners.get(path, [])
            return {"findScenes": {"scenes": [
                {"id": str(scene_id), "files": self.scene_files.get(str(scene_id), [])}
                for scene_id in scene_ids
            ]}}
        scene_id = str(variables["id"])
        if scene_id not in self.scene_files:
            return {"findScene": None}
        return {"findScene": {"id": scene_id, "files": self.scene_files[scene_id]}}


def _detected_move_batch(tmp_path, count=2):
    database = tmp_path / "phase4.sqlite3"
    source = tmp_path / "Before" / "Collection"
    destination = tmp_path / "After" / "Collection"
    destination.mkdir(parents=True)
    expected = []
    for index in range(count):
        old_path = source / f"video-{index}.mp4"
        new_path = destination / f"video-{index}.mp4"
        payload = bytes([index + 40]) * 512
        new_path.write_bytes(payload)
        _seed_file(database, old_path, f"phase4-file-{index}", f"phase4-scene-{index}", len(payload))
        _record_event(database, "deleted", old_path)
        _record_event(database, "created", new_path)
        expected.append((old_path, new_path))
    batch = detect_settled_operations(database, settle_seconds=0, now=_settled_now())[0]
    return database, source, destination, expected, batch


def _successful_grouped_stash(expected):
    scene_files = {}
    owners = {}
    for index, (_old_path, new_path) in enumerate(expected):
        scene_id = f"phase4-scene-{index}"
        scene_files[scene_id] = [{
            "id": f"phase4-file-{index}", "path": str(new_path), "basename": new_path.name,
        }]
        owners[str(new_path)] = [scene_id]
    return _GroupedScanStash(scene_files, owners)


def test_phase4_scans_destination_once_and_verifies_original_identities(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, destination, expected, batch = _detected_move_batch(tmp_path)
    stash = _successful_grouped_stash(expected)

    result = execute_grouped_move_reconciliation(
        database, batch["id"], stash, owner="phase4-test", scan_timeout=45,
    )

    assert result["state"] == "resolved"
    assert result["verified_count"] == 2
    assert result["uncertain_count"] == 0
    assert stash.scan_calls == [[str(destination)]]
    assert stash.wait_calls == [("group-job-1", 45)]
    assert all(member["observed_file_id"] == member["file_id"] for member in result["members"])
    connection = connect(database)
    try:
        inventory_paths = {
            row["file_id"]: row["path"] for row in connection.execute(
                "SELECT file_id,path FROM files WHERE file_id LIKE 'phase4-file-%'"
            )
        }
        assert inventory_paths == {
            f"phase4-file-{index}": str(new_path)
            for index, (_old_path, new_path) in enumerate(expected)
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM filesystem_events WHERE status='pending'"
        ).fetchone()[0] == 0
    finally:
        connection.close()
    assert all(new_path.is_file() for _old_path, new_path in expected)


def test_phase4_file_id_mismatch_remains_visible_as_partial(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, destination, expected, batch = _detected_move_batch(tmp_path)
    stash = _successful_grouped_stash(expected)
    second_path = str(expected[1][1])
    stash.scene_files["phase4-scene-1"] = [{
        "id": "replacement-file-id", "path": second_path, "basename": Path(second_path).name,
    }]

    result = execute_grouped_move_reconciliation(database, batch["id"], stash, owner="partial-test")

    assert result["state"] == "partially_verified"
    assert result["verified_count"] == 1
    mismatch = next(member for member in result["members"] if member["scene_id"] == "phase4-scene-1")
    assert mismatch["state"] == "uncertain"
    assert "different Stash file ID" in mismatch["reason"]
    assert list_review_batches(database)[0]["id"] == batch["id"]
    assert pending_filesystem_events(database) == []
    connection = connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM filesystem_events WHERE status='pending'"
        ).fetchone()[0] == 4
    finally:
        connection.close()
    assert stash.scan_calls == [[str(destination)]]


def test_phase4_explains_stale_same_scene_attachment_and_rechecks_without_rescan(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, destination, expected, batch = _detected_move_batch(tmp_path)
    stash = _successful_grouped_stash(expected)
    old_path, new_path = expected[1]
    size = new_path.stat().st_size
    fingerprints = [{"type": "oshash", "value": "same-video"}]
    stash.scene_files["phase4-scene-1"] = [
        {"id": "phase4-file-1", "path": str(old_path), "basename": old_path.name,
         "size": size, "fingerprints": fingerprints},
        {"id": "replacement-file-id", "path": str(new_path), "basename": new_path.name,
         "size": size, "fingerprints": fingerprints},
    ]

    result = execute_grouped_move_reconciliation(database, batch["id"], stash, owner="stale-test")
    stale = next(member for member in result["members"] if member["scene_id"] == "phase4-scene-1")
    assert result["state"] == "partially_verified"
    assert "Stash still lists file ID phase4-file-1 at the missing old path" in stale["reason"]
    assert "run Stash Clean" in stale["reason"]

    recheck_stash = _successful_grouped_stash(expected)
    recheck_stash.scene_files["phase4-scene-1"] = stash.scene_files["phase4-scene-1"]
    rechecked = execute_grouped_move_reconciliation(
        database, batch["id"], recheck_stash, owner="stale-recheck"
    )
    assert rechecked["state"] == "partially_verified"
    assert recheck_stash.scan_calls == []


def test_grouped_path_identity_normalizes_equivalent_unicode(tmp_path):
    from librarymanager_reconciliation import _normalized_filesystem_path

    composed = str(tmp_path / "Piñata Pete.mp4")
    decomposed = str(tmp_path / "Pin\u0303ata Pete.mp4")
    assert _normalized_filesystem_path(composed) == _normalized_filesystem_path(decomposed)


def test_phase4_changed_destination_blocks_scan(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, _destination, expected, batch = _detected_move_batch(tmp_path, count=2)
    changed = expected[0][1]
    original = changed.stat()
    changed.write_bytes(b"z" * original.st_size)
    changed.touch()
    stash = _successful_grouped_stash(expected)

    result = execute_grouped_move_reconciliation(database, batch["id"], stash, owner="changed-test")

    assert result["state"] == "partially_verified"
    changed_member = next(member for member in result["members"] if member["expected_path"] == str(changed))
    assert changed_member["state"] == "uncertain"
    assert "changed after grouped review" in changed_member["reason"]
    # A stable second member may still justify one bounded scan, but the changed
    # member can never be silently accepted by that scan.
    assert changed_member["observed_file_id"] is None


def test_phase4_resumes_recorded_scan_without_starting_another(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, _destination, expected, batch = _detected_move_batch(tmp_path)
    transition_batch(database, batch["id"], "scanning", expected_state="ready_for_review", stash_job_id="existing-job")
    stash = _successful_grouped_stash(expected)

    result = execute_grouped_move_reconciliation(database, batch["id"], stash, owner="resume-test", scan_timeout=33)

    assert result["state"] == "resolved"
    assert stash.scan_calls == []
    assert stash.wait_calls == [("existing-job", 33)]


def test_phase4_failed_scan_reports_attention_and_resolves_nothing(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, destination, expected, batch = _detected_move_batch(tmp_path)
    stash = _successful_grouped_stash(expected)
    stash.completed = False

    result = execute_grouped_move_reconciliation(database, batch["id"], stash, owner="failed-scan")

    assert result["state"] == "needs_attention"
    assert result["verified_count"] == 0
    assert "did not complete successfully" in result["reason"]
    assert stash.scan_calls == [[str(destination)]]


def test_phase4_copy_group_cannot_trigger_stash_scan(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database = tmp_path / "copy-phase4.sqlite3"
    batch = create_or_get_batch(
        database, correlation_key="copy-review", operation_type="folder_copy",
        source_prefix=str(tmp_path / "source"), destination_prefix=str(tmp_path / "copy"),
        state="ready_for_review",
    )
    stash = _GroupedScanStash({})
    with pytest.raises(ValueError, match="Only verified move groups"):
        execute_grouped_move_reconciliation(database, batch["id"], stash, owner="copy-test")
    assert stash.scan_calls == []


def test_phase4_stash_queries_do_not_hold_sqlite_write_lock(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, _destination, expected, batch = _detected_move_batch(tmp_path, count=2)

    class ConcurrentWriteStash(_GroupedScanStash):
        def __init__(self, base):
            super().__init__(base.scene_files, base.owners)
            self.wrote = False

        def call_GQL(self, query, variables):
            if not self.wrote:
                other = sqlite3.connect(database, timeout=0.1)
                try:
                    other.execute(
                        "UPDATE filesystem_monitor_status SET heartbeat_at='during-stash-query' WHERE id=1"
                    )
                    other.commit()
                    self.wrote = True
                finally:
                    other.close()
            return super().call_GQL(query, variables)

    stash = ConcurrentWriteStash(_successful_grouped_stash(expected))
    result = execute_grouped_move_reconciliation(database, batch["id"], stash, owner="no-lock-test")
    assert result["state"] == "resolved"
    assert stash.wrote is True


def test_phase4_large_group_uses_one_scan_and_never_hashes_video_bodies(tmp_path, monkeypatch):
    import librarymanager_reconciliation as reconciliation

    database, _source, destination, expected, batch = _detected_move_batch(tmp_path, count=25)
    stash = _successful_grouped_stash(expected)

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("Phase 4 execution must not hash video bodies")

    monkeypatch.setattr(reconciliation.hashlib, "sha256", forbidden_hash)
    result = reconciliation.execute_grouped_move_reconciliation(
        database, batch["id"], stash, owner="bounded-large-group"
    )

    assert result["state"] == "resolved"
    assert result["verified_count"] == 25
    assert stash.scan_calls == [[str(destination)]]


def _metadata_probe(path):
    try:
        result = Path(path).stat()
        return {
            "status": "ok", "exists": True,
            "is_file": Path(path).is_file(), "is_dir": Path(path).is_dir(),
            "size": int(result.st_size), "mtime_ns": int(result.st_mtime_ns),
        }
    except FileNotFoundError:
        return {"status": "missing", "exists": False}


def test_phase5_nas_unavailable_before_scan_starts(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, _destination, _expected, batch = _detected_move_batch(tmp_path)
    stash = _GroupedScanStash({})

    def unavailable_probe(path):
        if str(path).startswith(str(tmp_path / "After")):
            return {"status": "unavailable", "exists": None, "error": "NAS offline"}
        return _metadata_probe(path)

    result = execute_grouped_move_reconciliation(
        database, batch["id"], stash, owner="offline-before", path_probe=unavailable_probe,
    )
    assert result["state"] == "needs_attention"
    assert "No stable verified destinations" in result["reason"]
    assert stash.scan_calls == []


def test_phase5_nas_loss_after_scan_keeps_every_member_unverified(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, destination, expected, batch = _detected_move_batch(tmp_path)
    stash = _successful_grouped_stash(expected)
    file_probe_counts = {}

    def disconnecting_probe(path):
        path = str(path)
        if path == str(destination):
            return _metadata_probe(path)
        if path.startswith(str(destination)):
            file_probe_counts[path] = file_probe_counts.get(path, 0) + 1
            if file_probe_counts[path] > 1:
                return {"status": "unavailable", "exists": None, "error": "NAS disconnected"}
        return _metadata_probe(path)

    result = execute_grouped_move_reconciliation(
        database, batch["id"], stash, owner="offline-after", path_probe=disconnecting_probe,
    )
    assert result["state"] == "needs_attention"
    assert result["verified_count"] == 0
    assert all("unavailable during the Stash scan" in member["reason"] for member in result["members"])
    assert stash.scan_calls == [[str(destination)]]


def test_phase5_concurrent_approval_cannot_start_second_scan(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, _destination, expected, batch = _detected_move_batch(tmp_path)
    assert claim_batch(database, batch["id"], "first-operation", lease_seconds=60) is True
    stash = _successful_grouped_stash(expected)
    with pytest.raises(RuntimeError, match="already processing"):
        execute_grouped_move_reconciliation(database, batch["id"], stash, owner="second-operation")
    assert stash.scan_calls == []


def test_phase5_interrupted_verification_resumes_without_second_scan(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, destination, expected, batch = _detected_move_batch(tmp_path)

    class InterruptedStash(_GroupedScanStash):
        def call_GQL(self, query, variables):
            raise KeyboardInterrupt("simulated plugin termination")

    interrupted = InterruptedStash(_successful_grouped_stash(expected).scene_files,
                                   _successful_grouped_stash(expected).owners)
    with pytest.raises(KeyboardInterrupt, match="simulated plugin termination"):
        execute_grouped_move_reconciliation(database, batch["id"], interrupted, owner="interrupted")
    assert batch_snapshot(database, batch["id"])["state"] == "verifying"
    assert interrupted.scan_calls == [[str(destination)]]

    resumed = _successful_grouped_stash(expected)
    result = execute_grouped_move_reconciliation(database, batch["id"], resumed, owner="resumed")
    assert result["state"] == "resolved"
    assert resumed.scan_calls == []
    assert resumed.wait_calls == []


def test_phase5_scan_start_failure_is_visible_and_retryable(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, _destination, expected, batch = _detected_move_batch(tmp_path)

    class StartFailureStash(_GroupedScanStash):
        def metadata_scan(self, *, paths):
            self.scan_calls.append(list(paths))
            raise RuntimeError("Stash unavailable")

    base = _successful_grouped_stash(expected)
    stash = StartFailureStash(base.scene_files, base.owners)
    result = execute_grouped_move_reconciliation(database, batch["id"], stash, owner="start-failure")
    assert result["state"] == "needs_attention"
    assert "could not be started" in result["reason"]
    assert result["verified_count"] == 0


def test_phase5_stash_metadata_failure_never_reports_success(tmp_path):
    from librarymanager_reconciliation import execute_grouped_move_reconciliation

    database, _source, _destination, expected, batch = _detected_move_batch(tmp_path)

    class MetadataFailureStash(_GroupedScanStash):
        def call_GQL(self, query, variables):
            raise RuntimeError("GraphQL unavailable")

    base = _successful_grouped_stash(expected)
    stash = MetadataFailureStash(base.scene_files, base.owners)
    result = execute_grouped_move_reconciliation(database, batch["id"], stash, owner="metadata-failure")
    assert result["state"] == "needs_attention"
    assert result["verified_count"] == 0
    assert all("Could not verify Stash scene" in member["reason"] for member in result["members"])


def test_phase5_117_file_folder_move_is_one_scan_with_bounded_metadata_work(tmp_path, monkeypatch):
    import librarymanager_reconciliation as reconciliation

    database, _source, destination, expected, batch = _detected_move_batch(tmp_path, count=117)
    stash = _successful_grouped_stash(expected)
    monkeypatch.setattr(
        reconciliation.hashlib, "sha256",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("video hashing is forbidden")),
    )

    result = reconciliation.execute_grouped_move_reconciliation(
        database, batch["id"], stash, owner="large-folder",
    )
    assert result["state"] == "resolved"
    assert result["verified_count"] == 117
    assert stash.scan_calls == [[str(destination)]]
    assert len(stash.wait_calls) == 1
