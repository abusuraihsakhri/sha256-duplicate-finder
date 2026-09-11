#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# Android Termux Automated Setup Script
# SHA-256 Duplicate File Finder & Safe Organizer
# ==============================================================================
set -e

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
python3 -m pip install --upgrade pip
python3 -m pip install xxhash send2trash || python3 -m pip install send2trash

# 4. Create convenient 'dup-finder' alias in user path
PREFIX_BIN="/data/data/com.termux/files/usr/bin"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sha256_duplicate_finder.py"

cat << EOF > "$PREFIX_BIN/dup-finder"
#!/data/data/com.termux/files/usr/bin/bash
exec python3 "$SCRIPT_PATH" "\$@"
EOF

chmod +x "$PREFIX_BIN/dup-finder"

echo "============================================================"
echo "[SUCCESS] Installation Complete!"
echo "You can now run 'dup-finder' from any directory in Termux."
echo ""
echo "Example Usage:"
echo "  dup-finder ~/storage/shared/Download --cli --min-size-kb 1024"
echo "  dup-finder ~/storage/shared/DCIM --cli --dry-run"
echo "============================================================"
