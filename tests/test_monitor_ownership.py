import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from librarymanager import start_filesystem_monitor
from librarymanager_core import SCHEMA, connect, utc_now
from librarymanager_monitor import claim_monitor_ownership


class MonitorOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.sqlite3"
        conn = connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_claims_ownership_when_no_monitor_running(self):
        claimed = claim_monitor_ownership(self.db_path, "token-1", 1001, ["/data"], [])
        self.assertTrue(claimed)

        conn = connect(self.db_path)
        row = conn.execute("SELECT token, pid, state FROM filesystem_monitor_status WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(row["token"], "token-1")
        self.assertEqual(row["pid"], 1001)
        self.assertEqual(row["state"], "starting")

    def test_rejects_duplicate_when_existing_monitor_is_healthy_and_active(self):
        # Seed an active, healthy monitor with recent heartbeat
        fresh_hb = utc_now()
        conn = connect(self.db_path)
        conn.execute(
            """UPDATE filesystem_monitor_status
               SET token='token-old', pid=9999, state='running', heartbeat_at=?
               WHERE id=1""",
            (fresh_hb,),
        )
        conn.commit()
        conn.close()

        with patch("librarymanager_monitor._is_pid_alive", return_value=True), \
             patch("librarymanager_monitor._pid_matches_monitor", return_value=True):
            claimed = claim_monitor_ownership(self.db_path, "token-new", 2002, ["/data"], [])
            self.assertFalse(claimed, "Should reject when another monitor is genuinely running")

    def test_evicts_stale_monitor_when_recycled_pid_does_not_match_watchtower(self):
        # Seed a monitor where PID is alive on system, but command line is NOT Watchtower (Linux PID reuse)
        conn = connect(self.db_path)
        conn.execute(
            """UPDATE filesystem_monitor_status
               SET token='token-old', pid=5555, state='running', heartbeat_at=?
               WHERE id=1""",
            ((datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),),
        )
        conn.commit()
        conn.close()

        with patch("librarymanager_monitor._is_pid_alive", return_value=True), \
             patch("librarymanager_monitor._pid_matches_monitor", return_value=False):
            claimed = claim_monitor_ownership(self.db_path, "token-new", 2002, ["/data"], [])
            self.assertTrue(claimed, "Should evict stale monitor if PID does not match Watchtower")

        conn = connect(self.db_path)
        row = conn.execute("SELECT token, pid FROM filesystem_monitor_status WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(row["token"], "token-new")
        self.assertEqual(row["pid"], 2002)

    def test_evicts_stale_monitor_when_heartbeat_is_dead(self):
        # Seed a monitor where PID is alive, but heartbeat is > 45 seconds old
        stale_hb = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        conn = connect(self.db_path)
        conn.execute(
            """UPDATE filesystem_monitor_status
               SET token='token-old', pid=7777, state='running', heartbeat_at=?
               WHERE id=1""",
            (stale_hb,),
        )
        conn.commit()
        conn.close()

        with patch("librarymanager_monitor._is_pid_alive", return_value=True), \
             patch("librarymanager_monitor._pid_matches_monitor", return_value=True):
            claimed = claim_monitor_ownership(self.db_path, "token-new", 3003, ["/data"], [])
            self.assertTrue(claimed, "Should evict stale monitor when heartbeat is dead (>45s)")

        conn = connect(self.db_path)
        row = conn.execute("SELECT token, pid FROM filesystem_monitor_status WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(row["token"], "token-new")
        self.assertEqual(row["pid"], 3003)


class DuplicateStartupRaceTests(unittest.TestCase):
    """Two startups (Stash startup hook + a UI/operation call) can race. The loser's monitor
    exits cleanly with code 0 because the winner already owns the database. That must not
    be reported to the user as a failed start."""

    def _run_start(self, summaries, returncode=0):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = MagicMock()
            process.poll.return_value = returncode
            process.returncode = returncode
            stash = MagicMock()
            stash.find_plugin_config.return_value = {}
            with patch("librarymanager.fetch_library_roots", return_value=[str(root)]), \
                    patch("librarymanager.filesystem_monitor_summary", side_effect=summaries), \
                    patch("librarymanager.subprocess.Popen", return_value=process), \
                    patch("librarymanager.time.sleep"):
                return start_filesystem_monitor(stash, root / "db.sqlite3", {})

    def test_duplicate_startup_waits_for_the_winning_monitor(self):
        stopped = {"state": "stopped", "pid": None}
        winner_starting = {"state": "starting", "pid": 4321, "pid_alive": True, "token": "winner"}
        winner_running = {"state": "running", "pid": 4321, "pid_alive": True, "token": "winner"}
        result = self._run_start([stopped, winner_starting, winner_starting, winner_starting, winner_running])
        self.assertEqual(result["state"], "running")

    def test_clean_exit_with_no_other_owner_is_still_an_error(self):
        stopped = {"state": "stopped", "pid": None}
        with self.assertRaisesRegex(RuntimeError, "exited during startup"):
            self._run_start([stopped] + [stopped] * 400)

    def test_crash_exit_is_still_an_error_even_if_a_stale_owner_is_listed(self):
        stopped = {"state": "stopped", "pid": None}
        stale = {"state": "starting", "pid": 4321, "pid_alive": True, "token": "winner"}
        with self.assertRaisesRegex(RuntimeError, "exited during startup"):
            self._run_start([stopped, stale], returncode=1)


if __name__ == "__main__":
    unittest.main()
