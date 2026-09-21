import json
import pytest
from pathlib import Path
from librarymanager_core import (
    connect,
    get_backlog_items,
    acknowledge_backlog_missing,
    snapshot_incoming_baseline,
    get_authoritative_unavailable_roots,
)


def test_offline_incoming_root_does_not_create_false_missing_items(tmp_path):
    db = tmp_path / "test.db"
    inc_online = tmp_path / "IncomingOnline"
    inc_offline = tmp_path / "IncomingOffline"
    inc_online.mkdir()
    inc_offline.mkdir()

    v_online = inc_online / "Video Online.mp4"
    v_online.write_bytes(b"online video content")
    c_online = inc_online / "Video Online.jpg"
    c_online.write_bytes(b"online companion content")

    v_offline = inc_offline / "Video Offline.mp4"
    v_offline.write_bytes(b"offline video content")
    c_offline = inc_offline / "Video Offline.jpg"
    c_offline.write_bytes(b"offline companion content")

    # Take baseline snapshot when both roots are online
    config = {"incomingFolders": [str(inc_online), str(inc_offline)]}
    snap_count = snapshot_incoming_baseline(db, [str(inc_online), str(inc_offline)])
    assert snap_count == 4

    # Now simulate inc_offline going offline (unmounted)
    inc_offline_backup = tmp_path / "IncomingOffline_unmounted"
    inc_offline.rename(inc_offline_backup)
    assert not inc_offline.exists()

    res = get_backlog_items(db, None, config=config)

    # 1. Unavailable roots correctly reported
    assert str(inc_offline) in res["unavailable_roots"]
    assert str(inc_online) not in res["unavailable_roots"]

    # 2. No false missing files!
    assert res["missing_count"] == 0

    # 3. Status checks per item
    items_by_path = {it["path"]: it for it in res["items"]}
    assert str(v_offline) in items_by_path
    assert items_by_path[str(v_offline)]["status"] == "root_unavailable"
    assert items_by_path[str(v_offline)]["status_label"] == "Incoming Folder Unavailable"
    assert "Incoming folder unavailable" in items_by_path[str(v_offline)]["diagnostic"]

    assert str(c_offline) in items_by_path
    assert items_by_path[str(c_offline)]["status"] == "root_unavailable"
    assert items_by_path[str(c_offline)]["is_companion"] is True

    # 4. Online files are completely unaffected and ready
    assert items_by_path[str(v_online)]["status"] == "eligible"
    assert items_by_path[str(v_online)]["eligible"] is True
    assert items_by_path[str(c_online)]["status"] == "companion"


def test_reconnect_root_and_recheck_restores_normal_classification(tmp_path):
    db = tmp_path / "test.db"
    inc_offline = tmp_path / "IncomingOffline"
    inc_offline.mkdir()

    v_offline = inc_offline / "Video Offline.mp4"
    v_offline.write_bytes(b"offline video content")

    config = {"incomingFolders": [str(inc_offline)]}
    snapshot_incoming_baseline(db, [str(inc_offline)])

    # Simulate drive disconnect
    inc_offline_backup = tmp_path / "IncomingOffline_unmounted"
    inc_offline.rename(inc_offline_backup)

    res_disconnected = get_backlog_items(db, None, config=config)
    assert res_disconnected["missing_count"] == 0
    assert str(inc_offline) in res_disconnected["unavailable_roots"]
    assert res_disconnected["items"][0]["status"] == "root_unavailable"

    # Simulate drive reconnect
    inc_offline_backup.rename(inc_offline)
    assert inc_offline.is_dir()

    # Recheck immediately recovers normal classification without new baseline
    res_reconnected = get_backlog_items(db, None, config=config)
    assert len(res_reconnected["unavailable_roots"]) == 0
    assert res_reconnected["missing_count"] == 0
    assert res_reconnected["eligible_count"] == 1
    assert res_reconnected["items"][0]["status"] == "eligible"
    assert res_reconnected["items"][0]["eligible"] is True


def test_acknowledge_backlog_missing_rejects_offline_root(tmp_path):
    db = tmp_path / "test.db"
    inc_offline = tmp_path / "IncomingOffline"
    inc_offline.mkdir()

    v_offline = inc_offline / "Video Offline.mp4"
    v_offline.write_bytes(b"offline video content")

    config = {"incomingFolders": [str(inc_offline)]}
    snapshot_incoming_baseline(db, [str(inc_offline)])

    # Simulate drive disconnect
    inc_offline_backup = tmp_path / "IncomingOffline_unmounted"
    inc_offline.rename(inc_offline_backup)

    # Attempting to acknowledge missing for an offline disk path must fail safely
    with pytest.raises(ValueError, match="incoming folder is currently unavailable"):
        acknowledge_backlog_missing(db, [str(v_offline)], config=config)

    conn = connect(db)
    ack = conn.execute("SELECT * FROM filing_baseline_acknowledgements").fetchall()
    conn.close()
    assert len(ack) == 0


def test_stale_monitor_data_does_not_prevent_recovery(tmp_path):
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()

    v = inc / "Video.mp4"
    v.write_bytes(b"video content")

    config = {"incomingFolders": [str(inc)]}
    snapshot_incoming_baseline(db, [str(inc)])

    # Set stale monitor status indicating the root is unavailable
    conn = connect(db)
    conn.execute(
        """INSERT INTO filesystem_monitor_status (
            id, state, token, started_at, heartbeat_at, roots_json, unavailable_roots_json
        ) VALUES (1, 'running', 'tok', '2026-09-21T00:00:00Z', '2026-09-21T00:00:00Z', ?, ?)
        ON CONFLICT(id) DO UPDATE SET unavailable_roots_json=excluded.unavailable_roots_json""",
        (json.dumps([str(inc)]), json.dumps([str(inc)])),
    )
    conn.commit()
    conn.close()

    # The folder is physically present on disk. The direct authoritative check must override stale monitor data
    unavail = get_authoritative_unavailable_roots(db, [str(inc)])
    assert len(unavail) == 0

    res = get_backlog_items(db, None, config=config)
    assert len(res["unavailable_roots"]) == 0
    assert res["eligible_count"] == 1
    assert res["items"][0]["status"] == "eligible"
