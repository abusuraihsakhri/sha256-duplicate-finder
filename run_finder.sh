#!/usr/bin/env bash
# ==============================================================================
# Cross-Platform Launcher for Linux & macOS
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 is required but not installed."
    echo "Please install Python 3.8+ from https://www.python.org/ or your system package manager."
    exit 1
fi

# Ensure required libraries are installed
if ! python3 -c "import customtkinter, send2trash, xxhash" &>/dev/null; then
    echo "[*] Installing required packages (customtkinter, send2trash, xxhash)..."
    python3 -m pip install -r requirements.txt --quiet
fi

# Run application (passes through any CLI args if supplied)
exec python3 sha256_duplicate_finder.py "$@"
