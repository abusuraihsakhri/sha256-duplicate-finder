SHA-256 Duplicate File Finder & Safe Organizer
=================================================

A high-performance cross-platform duplicate file finder and safe storage
organizer supporting Windows, macOS, Linux, and Android (Termux).

QUICK START (WINDOWS)
---------------------
1. Make sure Python 3.8+ is installed (https://www.python.org/downloads/)
   IMPORTANT: Check "Add Python to PATH" during install.

2. Double-click "Run_SHA256_Duplicate_Finder.bat"
   or "Launch_Finder_Silent.vbs" directly.
   - Runs silently without keeping any black CMD console window open.
   - Automatically checks & installs required packages on initial launch.

3. Standalone Windows Executable:
   Download the precompiled single-file executable from GitHub Releases:
   https://github.com/abusuraihsakhri/sha256-duplicate-finder/releases

QUICK START (LINUX / macOS)
---------------------------
chmod +x run_finder.sh
./run_finder.sh

QUICK START (ANDROID / TERMUX)
------------------------------
Run in Termux:
curl -sSL https://raw.githubusercontent.com/abusuraihsakhri/sha256-duplicate-finder/main/termux_installer.sh | bash

CLI MODE
--------
python sha256_duplicate_finder.py /path/to/scan --cli --dry-run
python sha256_duplicate_finder.py --help

For detailed documentation, architecture diagrams, and full feature guides,
see README.md or visit:
https://github.com/abusuraihsakhri/sha256-duplicate-finder
