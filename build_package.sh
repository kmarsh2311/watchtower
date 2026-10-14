#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Watchtower Packaging & Verification Script
# ==============================================================================
# Builds dist/librarymanager.zip and performs automated extraction & import
# verification in an isolated sandbox to prevent missing files/dependencies.
# ==============================================================================

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="${PROJECT_ROOT}/dist"
ZIP_PATH="${DIST_DIR}/librarymanager.zip"

echo "=================================================="
echo "📦 Building Watchtower Distribution Package"
echo "=================================================="

mkdir -p "${DIST_DIR}"
rm -f "${ZIP_PATH}"

# Gather core plugin files dynamically
FILES_TO_PACK=(
    "CHANGELOG.md"
    "README.md"
    "requirements.txt"
    "librarymanager.yml"
    "librarymanager.css"
    "librarymanager.js"
)

# Include optional documentation if present
if [[ -f "${PROJECT_ROOT}/DISCOURSE_POST.md" ]]; then
    FILES_TO_PACK+=("DISCOURSE_POST.md")
fi

# Dynamically find all official librarymanager Python modules (excluding backups / copies)
while IFS= read -r py_file; do
    filename="$(basename "$py_file")"
    if [[ ! "$filename" =~ (copy|\.bak|\.backup|test) ]]; then
        FILES_TO_PACK+=("$filename")
    fi
done < <(find "${PROJECT_ROOT}" -maxdepth 1 -name "librarymanager*.py" | sort)

# Required assets & vendor directories
DIRS_TO_PACK=(
    "assets"
    "vendor-licenses"
    "watchdog"
)
if [[ -d "${PROJECT_ROOT}/docs/images" ]]; then
    DIRS_TO_PACK+=("docs/images")
fi
if [[ -d "${PROJECT_ROOT}/librarymanager" ]]; then
    DIRS_TO_PACK+=("librarymanager")
fi

echo "Discovered files to pack:"
for f in "${FILES_TO_PACK[@]}"; do
    echo "  📄 $f"
done
for d in "${DIRS_TO_PACK[@]}"; do
    echo "  📁 $d/"
done

# Create the archive
cd "${PROJECT_ROOT}"
zip -r "${ZIP_PATH}" "${FILES_TO_PACK[@]}" "${DIRS_TO_PACK[@]}" \
    -x "*.DS_Store" \
    -x "*/__pycache__/*" \
    -x "*.pyc" \
    -x "*.bak*" \
    -x "*copy*" > /dev/null

echo "✅ Created: ${ZIP_PATH}"

# Calculate SHA256
SHA256=$(shasum -a 256 "${ZIP_PATH}" | awk '{print $1}')
echo "🔒 SHA256: ${SHA256}"

# ==============================================================================
# Automated Sandbox Verification
# ==============================================================================
echo ""
echo "🔍 Verifying package integrity in isolated sandbox..."

TMP_TEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/watchtower_pkg_test.XXXXXX")
trap 'rm -rf "${TMP_TEST_DIR}"' EXIT

unzip -q "${ZIP_PATH}" -d "${TMP_TEST_DIR}"

# 1. Verify all expected core files exist
REQUIRED_FILES=(
    "librarymanager.yml"
    "librarymanager.js"
    "librarymanager.css"
    "librarymanager.py"
    "librarymanager_bootstrap.py"
    "librarymanager_core.py"
    "librarymanager_monitor.py"
    "librarymanager_startup.py"
    "librarymanager/database/connection.py"
    "librarymanager/scanning/__init__.py"
    "librarymanager/scanning/filesystem.py"
    "librarymanager/scanning/naming_rules.py"
    "librarymanager/scanning/reconciliation.py"
    "librarymanager/services/__init__.py"
    "librarymanager/services/contact_sheets.py"
    "librarymanager/services/filing.py"
    "librarymanager/services/filing_rules.py"
    "librarymanager/services/inventory.py"
    "librarymanager/services/manual_sync.py"
    "librarymanager/services/renaming.py"
)

for req in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "${TMP_TEST_DIR}/${req}" ]]; then
        echo "❌ FATAL: Missing required file in zip: ${req}" >&2
        rm -f "${ZIP_PATH}"
        exit 1
    fi
done

# 2. Test import in isolated sandbox with clean PYTHONPATH
cd "${TMP_TEST_DIR}"

python3 -c "
import sys
# Remove watchtower repo from sys.path so only files inside the extracted zip are resolved
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

print('✅ Python module import sanity check passed!')
"

echo "=================================================="
echo "🎉 Package built and verified successfully!"
echo "   File:   ${ZIP_PATH}"
echo "   SHA256: ${SHA256}"
echo "=================================================="
