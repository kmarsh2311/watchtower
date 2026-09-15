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
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
