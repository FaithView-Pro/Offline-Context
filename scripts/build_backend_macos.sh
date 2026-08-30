#!/bin/bash
# FaithView Pro — Build Python sidecar for macOS
# Requires: Python 3.11+, PyInstaller

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")/backend"

echo "[1/3] Installing PyInstaller..."
pip install pyinstaller -q

echo "[2/3] Building sidecar executable..."
cd "$BACKEND_DIR"
pyinstaller faithview_sidecar.spec --clean --noconfirm

echo "[3/3] Build complete!"
echo "Output: $BACKEND_DIR/dist/sidecar/sidecar"
if [ -f "$BACKEND_DIR/dist/sidecar/sidecar" ]; then
    echo "SUCCESS"
else
    echo "FAILED — check output above"
    exit 1
fi
