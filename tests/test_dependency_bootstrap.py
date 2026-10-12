import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DependencyBootstrapTests(unittest.TestCase):
    def test_ensure_dependencies_succeeds_when_already_available(self):
        from librarymanager_bootstrap import ensure_dependencies
        # Should return without exception
        ensure_dependencies()

    def test_ensure_dependencies_bootstraps_in_isolated_python(self):
        """Simulate an isolated Python environment where site-packages is stripped."""
        script = textwrap.dedent(
            f"""
            import sys
            import tempfile
            from pathlib import Path

            sys.path.insert(0, {str(ROOT)!r})

            # Strip all existing site-packages directories from sys.path
            sys.path = [p for p in sys.path if 'site-packages' not in p]

            with tempfile.TemporaryDirectory() as tmp_target:
                from librarymanager_bootstrap import ensure_dependencies
                ensure_dependencies(target_dir=tmp_target)

                from stashapi.stashapp import StashInterface
                assert StashInterface is not None
                print("BOOTSTRAP_OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}")
        self.assertIn("BOOTSTRAP_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
