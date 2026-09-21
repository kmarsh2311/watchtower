import pytest
from pathlib import Path
from librarymanager_core import (
    connect,
    snapshot_incoming_baseline,
    is_filing_baseline_established,
    get_backlog_items,
)


def test_interrupted_snapshot_preserves_previous_good_baseline(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()

    v1 = inc / "Video 1.mp4"
    v2 = inc / "Video 2.mp4"
    v1.write_bytes(b"video 1 content")
    v2.write_bytes(b"video 2 content")

    # 1. Establish initial valid baseline
    count = snapshot_incoming_baseline(db, [str(inc)])
    assert count == 2
    ok, msg = is_filing_baseline_established(db, [str(inc)])
    assert ok is True

    # 2. Add a new file and attempt a replacement snapshot that is interrupted midway
    v3 = inc / "Video 3.mp4"
    v3.write_bytes(b"video 3 content")

    real_stat = Path.stat

    def failing_stat(path_obj, *args, **kwargs):
        if path_obj.name == "Video 3.mp4":
            raise OSError("Simulated I/O interruption reading disk")
        return real_stat(path_obj, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)

    with pytest.raises(RuntimeError, match="Simulated I/O interruption"):
        snapshot_incoming_baseline(db, [str(inc)])

    # 3. Verify filing is safely blocked
    ok_after, err_msg = is_filing_baseline_established(db, [str(inc)])
    assert ok_after is False
    assert "failed" in err_msg.lower() or "interrupted" in err_msg.lower()

    # 4. Verify previous valid baseline was NOT destroyed/emptied
    conn = connect(db)
    baseline_rows = conn.execute("SELECT path FROM filing_incoming_baseline").fetchall()
    staging_rows = conn.execute("SELECT path FROM filing_incoming_baseline_staging").fetchall()
    conn.close()

    assert len(baseline_rows) == 2  # v1 and v2 are still preserved!
    assert len(staging_rows) == 0   # staging was cleaned up

    # 5. Restore stat and re-snapshot succeeds completely
    monkeypatch.setattr(Path, "stat", real_stat)
    count_recovered = snapshot_incoming_baseline(db, [str(inc)])
    assert count_recovered == 3

    ok_recovered, _ = is_filing_baseline_established(db, [str(inc)])
    assert ok_recovered is True


def test_new_cycle_after_incoming_emptied_and_refilled(tmp_path):
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()

    # First cycle
    v1 = inc / "Old Cycle.mp4"
    v1.write_bytes(b"old")
    snapshot_incoming_baseline(db, [str(inc)])

    # Empty incoming
    v1.unlink()

    # Refill incoming with new batch
    v2 = inc / "New Batch 1.mp4"
    v3 = inc / "New Batch 2.mp4"
    v2.write_bytes(b"new 1")
    v3.write_bytes(b"new 2")

    # Take replacement snapshot
    count = snapshot_incoming_baseline(db, [str(inc)])
    assert count == 2

    res = get_backlog_items(db, None, config={"incomingFolders": [str(inc)]})
    assert res["total_count"] == 2
    assert res["baseline_total"] == 2
    assert res["missing_count"] == 0
    assert res["needs_attention_count"] == 0
