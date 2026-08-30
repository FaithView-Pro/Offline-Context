#!/bin/bash
# FaithView Pro — Remote Tauri Build Script
# Run this on a machine with good internet + Rust 1.85+
#
# Usage:
#   1. Copy faithview-tauri-build.tar.gz to this machine
#   2. tar xzf faithview-tauri-build.tar.gz
#   3. bash build.sh
#   4. Copy back target/release/faithview-pro

set -e

echo "=== FaithView Pro Tauri Build ==="
echo ""

# Check Rust
if ! command -v rustc &>/dev/null; then
    echo "Rust not found. Installing via rustup..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
fi

echo "Rust: $(rustc --version)"
echo "Cargo: $(cargo --version)"
echo ""

# Check system deps (Ubuntu/Debian)
if command -v apt-get &>/dev/null; then
    echo "Checking system dependencies..."
    MISSING=""
    for pkg in libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev libjavascriptcoregtk-4.1-dev libsoup-3.0-dev; do
        if ! dpkg -s "$pkg" &>/dev/null; then
            MISSING="$MISSING $pkg"
        fi
    done
    if [ -n "$MISSING" ]; then
        echo "Installing missing packages (requires sudo):"
        echo "  sudo apt-get install -y$MISSING"
        sudo apt-get install -y $MISSING
    fi
    echo "System deps OK"
fi

echo ""
echo "Building (release mode)..."
cd src-tauri

# Configure sparse protocol for faster downloads
mkdir -p ~/.cargo
cat > ~/.cargo/config.toml << 'EOF'
[registries.crates-io]
protocol = "sparse"
EOF

cargo build --release 2>&1

echo ""
echo "=== Build Complete ==="
echo "Binary: src-tauri/target/release/faithview-pro"
ls -lh target/release/faithview-pro 2>/dev/null || echo "Binary not found at expected path"
echo ""
echo "Copy it back with:"
echo "  scp user@this-machine:$(pwd)/target/release/faithview-pro /path/to/faithview-pro"
