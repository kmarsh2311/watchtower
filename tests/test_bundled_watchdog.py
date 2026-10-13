import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BundledWatchdogTests(unittest.TestCase):
    def test_bundled_watchdog_detects_a_created_file_in_isolated_python(self):
        script = textwrap.dedent(
            f"""
            import queue
            import sys
            import tempfile
            import time
            from pathlib import Path

            sys.path.insert(0, {str(ROOT)!r})

            import watchdog
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            plugin_root = Path({str(ROOT)!r}).resolve()
            assert Path(watchdog.__file__).resolve().is_relative_to(plugin_root)

            events = queue.Queue()

            class Handler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        events.put(Path(event.src_path).name)

            with tempfile.TemporaryDirectory() as directory:
                observer = Observer()
                observer.schedule(Handler(), directory, recursive=False)
                observer.start()
                try:
                    target = Path(directory) / "watchtower-smoke-test.txt"
                    target.write_text("ready", encoding="utf-8")
                    deadline = time.monotonic() + 10
                    detected = None
                    while time.monotonic() < deadline:
                        try:
                            detected = events.get(timeout=0.25)
                            break
                        except queue.Empty:
                            pass
                    assert detected == target.name, detected
                finally:
                    observer.stop()
                    observer.join(timeout=5)
                    assert not observer.is_alive()
            """
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    def test_fsevents_queue_events_does_not_stat_unnecessarily(self):
        from unittest.mock import MagicMock, patch
        try:
            from watchdog.observers.fsevents import FSEventsEmitter
            from watchdog.observers.api import ObservedWatch
        except ImportError:
            return

        queue_mock = MagicMock()
        watch = ObservedWatch("/Volumes/Vault2/Vault2", recursive=True)
        emitter = FSEventsEmitter(queue_mock, watch)

        native_event = MagicMock()
        native_event.path = "/Volumes/Vault2/Vault2"
        native_event.inode = 12345
        native_event.is_created = False
        native_event.is_removed = False
        native_event.is_renamed = False
        native_event.is_modified = False
        native_event.is_inode_meta_mod = False
        native_event.is_xattr_mod = False
        native_event.is_owner_change = False
        native_event.is_root_changed = False

        # Periodic flush / heartbeat events should never call os.stat
        with patch("os.stat") as mock_stat:
            emitter.queue_events(1.0, [native_event])
            mock_stat.assert_not_called()

        # Rename events on the watch root itself should never call os.stat
        native_event.is_renamed = True
        with patch("os.stat") as mock_stat:
            emitter.queue_events(1.0, [native_event])
            mock_stat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
