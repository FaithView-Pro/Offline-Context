#!/bin/bash
# FaithView Pro — Receive Built Binary
# Run this after copying the binary from the remote build machine
#
# Usage:
#   bash receive_binary.sh /path/to/faithview-pro

set -e

BINARY="${1:-faithview-pro}"
DEST="desktop/src-tauri/target/release/faithview-pro"

if [ ! -f "$BINARY" ]; then
    echo "Error: $BINARY not found"
    echo "Usage: bash receive_binary.sh /path/to/faithview-pro"
    exit 1
fi

mkdir -p "$(dirname "$DEST")"
cp "$BINARY" "$DEST"
chmod +x "$DEST"

echo "Binary installed: $DEST"
ls -lh "$DEST"
echo ""
echo "Test with: cd desktop && npx tauri dev"
