#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# Android Termux Automated Setup Script
# SHA-256 Duplicate File Finder & Safe Organizer
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/sha256_duplicate_finder.py"
if [ ! -f "$SCRIPT_PATH" ]; then
    echo "[ERROR] sha256_duplicate_finder.py not found beside installer."
    echo "Clone the repository and run bash termux_installer.sh from that checkout."
    exit 1
fi

echo "============================================================"
echo " SHA-256 Duplicate File Finder - Android Termux Setup"
echo "============================================================"

# 1. Request Android Storage Access if needed
if [ ! -d "$HOME/storage" ]; then
    echo "[*] Requesting Android storage permission..."
    termux-setup-storage
    sleep 2
fi

# 2. Update packages and install python
echo "[*] Ensuring Python and build tools are installed..."
pkg update -y
pkg install -y python clang libffi

# 3. Install dependencies
echo "[*] Installing dependencies..."
python3 -m pip install xxhash || echo "[WARN] xxhash is unavailable; the built-in SHA-256 fallback remains usable."

# 4. Create convenient 'dup-finder' alias in user path
PREFIX_BIN="/data/data/com.termux/files/usr/bin"
# Quote the script path as a shell argument, including spaces and metacharacters.
printf -v quoted_script '%q' "$SCRIPT_PATH"
printf '#!/data/data/com.termux/files/usr/bin/bash\nexec python3 %s "$@"\n' "$quoted_script" > "$PREFIX_BIN/dup-finder"

chmod +x "$PREFIX_BIN/dup-finder"

echo "============================================================"
echo "[SUCCESS] Installation Complete!"
echo "You can now run 'dup-finder' from any directory in Termux."
echo ""
echo "Note: system Trash support is not included in this Termux installer."
echo "Example Usage:"
echo "  dup-finder ~/storage/shared/Download --cli --min-size-kb 1024"
echo "  dup-finder ~/storage/shared/DCIM --cli --dry-run"
echo "============================================================"
