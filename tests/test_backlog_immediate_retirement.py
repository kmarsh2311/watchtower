from unittest.mock import MagicMock
import json
import pytest
from pathlib import Path
from librarymanager_core import (
    connect,
    snapshot_incoming_baseline,
    apply_filing_proposal,
    get_backlog_items,
    prune_resolved_filing_baseline,
    utc_now,
)


def test_successful_filing_immediately_retires_baseline_and_increments_aggregate(tmp_path):
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    dest_root = tmp_path / "Library"
    dest_folder = dest_root / "Studio Name"
    inc.mkdir()
    dest_folder.mkdir(parents=True)

    v = inc / "Scene 1.mp4"
    v.write_bytes(b"video content")
    c = inc / "Scene 1.jpg"
    c.write_bytes(b"companion content")

    snapshot_incoming_baseline(db, [str(inc)])

    conn = connect(db)
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f1', 's1', ?, 'Scene 1.mp4', 1, ?, ?)""",
        (str(v), utc_now(), utc_now()),
    )
    prop_id = conn.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, destination_filename,
            destination_folder, proposed_path, organize_by, matched_entity_id, matched_entity_name,
            match_source, reason, status, created_at, updated_at
        ) VALUES ('f1', 's1', ?, 'Scene 1.mp4', ?, ?, 'studio', '1', 'Studio Name', 'filename', 'test', 'pending', ?, ?)""",
        (str(v), str(dest_folder), str(dest_folder / "Scene 1.mp4"), utc_now(), utc_now()),
    ).lastrowid
    conn.commit()
    conn.close()

    dest_video = dest_folder / "Scene 1.mp4"
    current_vid = [v]
    mock_stash = MagicMock()
    mock_stash.find_plugin_config.return_value = {
        "incomingFolders": [str(inc)],
        "autoFilingDestinationRoots": [str(dest_root)]
    }
    def mock_gql(query, variables=None):
        if "findScene" in query:
            return {"findScene": {"id": "s1", "files": [{"id": "f1", "path": str(current_vid[0])}]}}
        return {}
    mock_stash.call_GQL.side_effect = mock_gql
    def mock_move(payload):
        target = Path(payload["destination_folder"]) / payload["destination_basename"]
        curr = current_vid[0]
        curr.rename(target)
        current_vid[0] = target
        return True
    mock_stash.move_files.side_effect = mock_move

    config = {
        "incomingFolders": [str(inc)],
        "autoFilingDestinationRoots": [str(dest_root)]
    }

    res = apply_filing_proposal(db, mock_stash, prop_id, config=config)
    assert res["status"] == "completed"

    # Verify baseline row is retired immediately from filing_incoming_baseline table
    conn = connect(db)
    remaining_rows = conn.execute("SELECT * FROM filing_incoming_baseline").fetchall()
    summary = conn.execute("SELECT * FROM filing_baseline_summary WHERE id=1").fetchone()
    conn.close()

    assert len(remaining_rows) == 0
    assert summary["filed_count"] == 2  # video + companion retired to aggregate
    assert summary["remaining_count"] == 0


def test_idempotent_retirement_does_not_double_count(tmp_path):
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()
    v = inc / "Scene 1.mp4"
    v.write_bytes(b"video content")

    snapshot_incoming_baseline(db, [str(inc)])

    conn = connect(db)
    conn.execute("INSERT INTO filing_baseline_acknowledgements(path, reason, acknowledged_at) VALUES (?, 'test', ?)", (str(v), utc_now()))
    conn.commit()
    conn.close()

    res1 = prune_resolved_filing_baseline(db)
    assert res1["retired"]["acknowledged"] == 1

    # Second run is idempotent
    res2 = prune_resolved_filing_baseline(db)
    assert res2["retired"]["acknowledged"] == 0

    conn = connect(db)
    summary = conn.execute("SELECT * FROM filing_baseline_summary WHERE id=1").fetchone()
    conn.close()
    assert summary["acknowledged_count"] == 1
