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


def test_rapid_restart_produces_both_stop_and_start_events():
    """Verify that a genuine rapid stop/start within milliseconds produces both events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.sqlite3"
        _init_db(db)

        # 1. Monitor starts initially
        record_monitor_lifecycle(
            db, "MONITOR STARTED", "running",
            detail="MONITOR STARTED — watching 2 library root(s) (PID 1001)",
            metadata={"pid": 1001, "roots": ["/vol1", "/vol2"]}
        )

        # 2. Rapid genuine restart (STOP then START immediately, with no sleep delay)
        record_monitor_lifecycle(
            db, "MONITOR STOPPED", "stopped",
            detail="MONITOR STOPPED — filesystem watcher stopped (PID 1001)",
            metadata={"pid": 1001}
        )
        record_monitor_lifecycle(
            db, "MONITOR STARTED", "running",
            detail="MONITOR STARTED — watching 2 library root(s) (PID 1002)",
            metadata={"pid": 1002, "roots": ["/vol1", "/vol2"]}
        )

        # Both events must be recorded regardless of elapsed time
        activities = recent_activity(db)
        assert len(activities) == 3
        actions = [a["action"] for a in activities]
        assert actions == ["MONITOR STARTED", "MONITOR STOPPED", "MONITOR STARTED"]

        # All events are informational only (no warnings, no Needs Attention)
        assert len(pending_filesystem_events(db)) == 0
        dash = dashboard_data(db)
        problems = [r for r in dash.get("activity", []) if r.get("severity") in ("error", "warning")]
        assert len(problems) == 0

        for a in activities:
            assert a["category"] == "monitor"
            assert a["severity"] == "info"
            assert a["recorded_at"] is not None


def test_plugin_reload_while_monitor_already_running_creates_no_duplicate_startup():
    """Verify that reloading the plugin while the monitor is already running creates NO duplicate event."""
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

        # Simulate plugin reload: Stash loads plugin and invokes ensure_monitor / start_filesystem_monitor.
        # The monitor process is already alive and running with PID 9999.
        stash = MagicMock()
        stash.find_plugin_config.return_value = {"autoStartMonitor": True}
        running_summary = {"state": "running", "pid": 9999, "is_stale": False, "raw_state": "running"}

        with patch("librarymanager.filesystem_monitor_summary", return_value=running_summary), \
             patch("librarymanager._is_pid_alive", return_value=True):
            res = start_filesystem_monitor(stash, db)

        # Probing confirms process is alive; no new process spawned
        assert res.get("message") == "Filesystem monitor is already running or starting"

        # Activity log must still have exactly 2 rows (no duplicate start event added)
        acts = recent_activity(db)
        assert len(acts) == 2
        assert acts[0]["action"] == "MONITOR STARTED"
        assert acts[1]["action"] == "targeted Stash scan"


def test_full_restart_lifecycle_preserves_history():
    """Verify full restart sequence preserves prior history intact."""
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
