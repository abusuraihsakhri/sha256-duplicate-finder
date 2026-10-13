# SHA-256 Duplicate File Finder

### [Download releases →](https://github.com/abusuraihsakhri/sha256-duplicate-finder/releases)

[![Continuous Integration](https://github.com/abusuraihsakhri/sha256-duplicate-finder/actions/workflows/ci.yml/badge.svg)](https://github.com/abusuraihsakhri/sha256-duplicate-finder/actions/workflows/ci.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A local, cross-platform duplicate-file scanner with a **CustomTkinter desktop GUI** and a **headless Python CLI**. It indexes eligible files by size, filters candidates using a partial hash, and confirms matches using full SHA-256 hashing. No files are uploaded.

## Features

- Recursive scanning of one or more folders, with an optional file-size threshold and limited HDD-concurrency mode.
- SHA-256 verification, retention by oldest/newest modified time, shortest path, or first match.
- Dry-run by default; explicit confirmation and re-verification of both file copies before trashing or deleting.
- CSV/JSON reports, GUI file-manager shortcuts, and native Trash/Recycle Bin support where available.
- Protection against overlapping scan roots, hard-link double-counting and symbolic-link directory cycles.

**Safety limitations:** Do not use deletion on active or untrusted directories. Files can still change between final verification and removal; there is no atomic filesystem transaction. Symbolic-link files are excluded from destructive operations. Read-protected files may have permissions adjusted for permanent removal. Back up irreplaceable data first.

## Install

Requires **Python 3.9 or later**. Linux desktop GUI additionally requires Tk/X11; for headless servers and Android use the CLI.

```bash
git clone https://github.com/abusuraihsakhri/sha256-duplicate-finder.git
cd sha256-duplicate-finder
python -m pip install -e .
```

On Linux and macOS you can also launch with `./run_finder.sh`. On Windows, use `Run_SHA256_Duplicate_Finder.bat` or the released executable. The packaged Windows/macOS GUI executables use windowed mode; use the Python CLI for scriptable terminal output.

### GUI

```bash
python sha256_duplicate_finder.py
```

Select directories, configure minimum size and thread count, and press **Start Scan**. Review each group and its retained original. Dry-run is enabled initially. Selecting **Move Duplicates to Trash** or **Permanently Delete** requires disabling dry-run and confirming the action. Only files that still match their scanned SHA-256 and retained original are eligible for removal.

### CLI

```bash
# Scan all file sizes without modifying anything
sha256-dup-finder /path/to/photos --cli --min-size-kb 0

# Save a report when duplicates are found
sha256-dup-finder /path/to/photos --cli --export-json duplicates.json

# Move duplicates to Trash after confirmation (where supported)
sha256-dup-finder /path/to/photos --cli --trash --no-dry-run

# Permanent removal: only after explicit opt-in and confirmation
sha256-dup-finder /path/to/photos --cli --delete --no-dry-run

# See all options
sha256-dup-finder --help
```

The default minimum size is **1024 KB** (1 MiB). The default retention policy keeps the earliest **modification timestamp**, not necessarily the creation-time original. The CLI uses two workers in HDD mode (default), and `--no-hdd-mode --workers N` selects higher concurrency. `--include-hidden` includes dot-prefixed names. `--follow-symlinks` follows directory links with cycle protection; destructive operations still refuse symlink paths. Failed removal operations produce a nonzero CLI exit code.

### Android (Termux)

Clone the repository within Termux, then run the checked-out installer:

```bash
pkg install git
git clone https://github.com/abusuraihsakhri/sha256-duplicate-finder.git
cd sha256-duplicate-finder
bash termux_installer.sh
dup-finder ~/storage/shared/Download --cli
```

The Termux installer configures a CLI launcher and installs an optional fast-hash library. Native system Trash integration is not configured on Termux; review reports before using `--delete`. The desktop GUI is not supported on Android.

## Reports and privacy

Reports include the scanned local paths, timestamps, SHA-256 digests, selected keeper and reclaimable-size estimates. All file processing is local; no remote API is required. CSV/JSON report files may expose sensitive filenames and should be stored appropriately. Unreadable or changing files may be skipped; a scan is not a backup or proof that every directory entry was inspected.

## Development

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e .
python -m ruff check --select E4,E7,E9,F63,F7,F82 .
python -m pytest -v
python -m build
```

GitHub Actions executes tests on Windows, macOS and Linux across Python 3.9–3.13, with critical-error linting and distribution-build checks. Tagged releases build standalone desktop artifacts and Python/Termux packages.

**GitHub Pages:** This is a native filesystem tool, not a browser application. GitHub Pages cannot safely provide its recursive disk scanning and native deletion workflows.

## License

[MIT](LICENSE).
