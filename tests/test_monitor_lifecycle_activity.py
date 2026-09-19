import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from librarymanager_core import (
    SCHEMA,
    connect,
    dashboard_data,
    pending_filesystem_events,
    recent_activity,
    record_activity,
    record_monitor_lifecycle,
)
from librarymanager import start_filesystem_monitor, stop_filesystem_monitor


def _init_db(database_path):
    connection = connect(database_path)
    connection.executescript(SCHEMA)
    connection.close()


def test_record_monitor_lifecycle_informational_and_deduplication():
    """Verify that monitor lifecycle events are recorded with info severity and deduplicated."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.sqlite3"
        _init_db(db)

        # 1. Record monitor started
        recorded = record_monitor_lifecycle(
            db, "MONITOR STARTED", "running",
            detail="MONITOR STARTED — watching 2 library root(s) (PID 12345)",
            metadata={"pid": 12345, "roots": ["/vol1", "/vol2"]}
        )
        assert recorded is True

        # Check recorded fields
        activities = recent_activity(db)
        assert len(activities) == 1
        entry = activities[0]
        assert entry["category"] == "monitor"
        assert entry["action"] == "MONITOR STARTED"
        assert entry["status"] == "running"
        assert entry["severity"] == "info"
        assert "2026-" in entry["recorded_at"]
        assert entry["metadata"]["pid"] == 12345
        assert "MONITOR STARTED" in entry["detail"]

        # Informational only: Needs Attention and problems must be zero
        assert len(pending_filesystem_events(db)) == 0
        dash = dashboard_data(db)
        problems = [r for r in dash.get("activity", []) if r.get("severity") in ("error", "warning")]
        assert len(problems) == 0

        # 2. Duplicate call within 3 seconds must be skipped
        dup = record_monitor_lifecycle(
            db, "MONITOR STARTED", "running",
            detail="MONITOR STARTED — duplicate call",
            metadata={"pid": 12345}
        )
        assert dup is False
        assert len(recent_activity(db)) == 1

        # 3. Different action (STOPPED) records immediately
        stopped = record_monitor_lifecycle(
            db, "MONITOR STOPPED", "stopped",
            detail="MONITOR STOPPED — filesystem watcher stopped (PID 12345)",
            metadata={"pid": 12345}
        )
        assert stopped is True
        activities = recent_activity(db)
        assert len(activities) == 2
        assert activities[0]["action"] == "MONITOR STOPPED"
        assert activities[1]["action"] == "MONITOR STARTED"


def test_plugin_reload_and_ensure_monitor_avoids_duplicate_events():
    """Verify that plugin reloads (ensure_monitor on already running daemon) do not add duplicates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.sqlite3"
        _init_db(db)

        # Pre-seed existing history
        record_activity(db, "reconciliation", "targeted Stash scan", "updated",
                        detail="Stash scan confirmed the new path")
        record_monitor_lifecycle(
            db, "MONITOR STARTED", "running",
            detail="MONITOR STARTED — watching 1 root (PID 9999)"
        )
        assert len(recent_activity(db)) == 2

        # Simulate plugin reload / Stash UI loading ensure_monitor:
        # Monitor is already running with PID 9999
        stash = MagicMock()
        stash.find_plugin_config.return_value = {"autoStartMonitor": True}
        running_summary = {"state": "running", "pid": 9999, "is_stale": False, "raw_state": "running"}

        with patch("librarymanager.filesystem_monitor_summary", return_value=running_summary), \
             patch("librarymanager.os.kill"):
            res = start_filesystem_monitor(stash, db)

        assert res.get("message") == "Filesystem monitor is already running or starting"

        # Activity log must still have exactly 2 rows (no duplicate start event added)
        acts = recent_activity(db)
        assert len(acts) == 2
        assert acts[0]["action"] == "MONITOR STARTED"
        assert acts[1]["action"] == "targeted Stash scan"


def test_full_restart_lifecycle_preserves_history():
    """Verify full restart sequence: START -> STOP -> RESTART preserves prior history."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.sqlite3"
        _init_db(db)

        # 1. Historical media events exist
        for i in range(3):
            record_activity(db, "reconciliation", "targeted Stash scan", "updated",
                            detail=f"Reconnected scene {i+1}")

        # 2. Initial monitor start
        record_monitor_lifecycle(
            db, "MONITOR STARTED", "running",
            detail="MONITOR STARTED — watching 2 roots (PID 1001)",
            metadata={"pid": 1001}
        )

        # 3. Monitor stops
        record_monitor_lifecycle(
            db, "MONITOR STOPPED", "stopped",
            detail="MONITOR STOPPED — filesystem watcher stopped (PID 1001)",
            metadata={"pid": 1001}
        )

        # 4. Monitor restarts with new PID
        record_monitor_lifecycle(
            db, "MONITOR STARTED", "running",
            detail="MONITOR STARTED — watching 2 roots (PID 1002)",
            metadata={"pid": 1002}
        )

        activities = recent_activity(db)
        # 3 historical media events + 3 monitor lifecycle events = 6 total
        assert len(activities) == 6

        actions = [a["action"] for a in activities]
        assert actions[:3] == ["MONITOR STARTED", "MONITOR STOPPED", "MONITOR STARTED"]
        assert actions[3:] == ["targeted Stash scan", "targeted Stash scan", "targeted Stash scan"]

        # All events have timestamps and severity='info'
        for a in activities[:3]:
            assert a["category"] == "monitor"
            assert a["severity"] == "info"
            assert a["recorded_at"] is not None
