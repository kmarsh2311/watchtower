import os
import subprocess
import sys
import tempfile
import zipfile
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BUILD_SCRIPT = os.path.join(REPO_ROOT, "build_package.sh")
ZIP_PATH = os.path.join(REPO_ROOT, "dist", "librarymanager.zip")


def test_build_package_script_exists_and_executable():
    assert os.path.isfile(BUILD_SCRIPT), "build_package.sh must exist at repo root"
    assert os.access(BUILD_SCRIPT, os.X_OK), "build_package.sh must be executable"


def test_build_package_runs_and_verifies_integrity():
    """Runs build_package.sh and confirms exit code 0 and valid zip output."""
    res = subprocess.run([BUILD_SCRIPT], cwd=REPO_ROOT, capture_output=True, text=True)
    assert res.returncode == 0, f"build_package.sh failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert os.path.isfile(ZIP_PATH), "dist/librarymanager.zip was not created"


def test_zip_contains_all_critical_modules():
    """Verifies that all required Python modules and assets are present inside the zip."""
    assert os.path.isfile(ZIP_PATH), "Run build_package.sh before testing zip contents"

    with zipfile.ZipFile(ZIP_PATH, "r") as z:
        names = set(z.namelist())

    critical_files = [
        "librarymanager.yml",
        "librarymanager.js",
        "librarymanager.css",
        "librarymanager.py",
        "librarymanager_bootstrap.py",
        "librarymanager_core.py",
        "librarymanager_monitor.py",
        "librarymanager_reconciliation.py",
        "librarymanager_startup.py",
        "librarymanager/scanning/filesystem.py",
        "librarymanager/scanning/naming_rules.py",
        "librarymanager/scanning/reconciliation.py",
        "librarymanager/services/__init__.py",
        "librarymanager/services/contact_sheets.py",
        "librarymanager/services/filing.py",
        "librarymanager/services/filing_rules.py",
        "librarymanager/services/inventory.py",
        "librarymanager/services/manual_sync.py",
        "librarymanager/services/renaming.py",
        "requirements.txt",
    ]

    for filename in critical_files:
        assert filename in names, f"Critical file {filename} is missing from dist/librarymanager.zip"


def test_extracted_package_import_sandbox():
    """Extracts the zip to an isolated temp directory and runs an import test in a clean subprocess."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(ZIP_PATH, "r") as z:
            z.extractall(tmpdir)

        test_code = """
import sys
# Strip repo root from sys.path to simulate a pure Stash plugin installation
sys.path = [p for p in sys.path if 'Developer/watchtower' not in p]
if '.' not in sys.path:
    sys.path.insert(0, '.')

import librarymanager_bootstrap
import librarymanager_startup
import librarymanager
import librarymanager.scanning.filesystem
import librarymanager.scanning.naming_rules
import librarymanager.scanning.reconciliation
import librarymanager.services.contact_sheets
import librarymanager.services.filing
import librarymanager.services.filing_rules
import librarymanager.services.inventory
import librarymanager.services.manual_sync
import librarymanager.services.renaming

print('SUCCESS')
"""
        cmd = [sys.executable, "-c", test_code]
        proc = subprocess.run(cmd, cwd=tmpdir, capture_output=True, text=True)
        assert proc.returncode == 0, f"Import test in clean sandbox failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        assert "SUCCESS" in proc.stdout
