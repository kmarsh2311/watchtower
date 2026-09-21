import pytest
from pathlib import Path
from librarymanager_core import (
    connect,
    prune_operational_records,
    utc_now,
)


def test_pending_records_survive_while_old_resolved_are_pruned(tmp_path):
    db = tmp_path / "test.db"
    conn = connect(db)

    now = utc_now()

    # 1. Insert 2,050 resolved filesystem events and 10 pending events
    for i in range(2050):
        conn.execute(
            """INSERT INTO filesystem_events (
                event_type, source_path, first_seen_at, last_seen_at, status
            ) VALUES ('created', ?, ?, ?, 'resolved')""",
            (f"/path/resolved_{i}.mp4", now, now),
        )
    for i in range(10):
        conn.execute(
            """INSERT INTO filesystem_events (
                event_type, source_path, first_seen_at, last_seen_at, status
            ) VALUES ('created', ?, ?, ?, 'pending')""",
            (f"/path/pending_{i}.mp4", now, now),
        )

    # 2. Insert 1,050 completed proposals and 5 pending proposals
    for i in range(1050):
        conn.execute(
            """INSERT INTO filing_proposals (
                file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
                organize_by, matched_entity_id, matched_entity_name, match_source, reason, status, created_at, updated_at
            ) VALUES (?, ?, ?, '/dst.mp4', '/dst', 'dst.mp4', 'studio', '1', 'Studio', 'name', 'test', 'completed', ?, ?)""",
            (f"f_c_{i}", f"s_c_{i}", f"/src/completed_{i}.mp4", now, now),
        )
    for i in range(5):
        conn.execute(
            """INSERT INTO filing_proposals (
                file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
                organize_by, matched_entity_id, matched_entity_name, match_source, reason, status, created_at, updated_at
            ) VALUES (?, ?, ?, '/dst.mp4', '/dst', 'dst.mp4', 'studio', '1', 'Studio', 'name', 'test', 'pending', ?, ?)""",
            (f"f_p_{i}", f"s_p_{i}", f"/src/pending_{i}.mp4", now, now),
        )

    # 3. Insert active incoming files and 1,050 imported incoming files
    for i in range(1050):
        conn.execute(
            """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status)
               VALUES (?, ?, ?, 'imported')""",
            (f"/inc/imported_{i}.mp4", now, now),
        )
    for i in range(8):
        conn.execute(
            """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status)
               VALUES (?, ?, ?, 'waiting')""",
            (f"/inc/waiting_{i}.mp4", now, now),
        )

    conn.commit()
    conn.close()

    # Execute pruning
    pruned = prune_operational_records(db)

    conn = connect(db)
    # Check filesystem events
    pending_events = conn.execute("SELECT COUNT(*) AS count FROM filesystem_events WHERE status='pending'").fetchone()["count"]
    resolved_events = conn.execute("SELECT COUNT(*) AS count FROM filesystem_events WHERE status='resolved'").fetchone()["count"]
    assert pending_events == 10
    assert resolved_events == 2000
    assert pruned["filesystem_events"] == 50

    # Check filing proposals
    pending_props = conn.execute("SELECT COUNT(*) AS count FROM filing_proposals WHERE status='pending'").fetchone()["count"]
    completed_props = conn.execute("SELECT COUNT(*) AS count FROM filing_proposals WHERE status='completed'").fetchone()["count"]
    assert pending_props == 5
    assert completed_props == 1000
    assert pruned["filing_proposals"] == 50

    # Check incoming files
    waiting_inc = conn.execute("SELECT COUNT(*) AS count FROM incoming_files WHERE status='waiting'").fetchone()["count"]
    imported_inc = conn.execute("SELECT COUNT(*) AS count FROM incoming_files WHERE status='imported'").fetchone()["count"]
    assert waiting_inc == 8
    assert imported_inc == 1000
    assert pruned["incoming_files"] == 50
    conn.close()
