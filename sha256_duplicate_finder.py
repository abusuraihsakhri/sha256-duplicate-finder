#!/usr/bin/env python3
"""
SHA-256 Duplicate File Finder & Safe Organizer
==============================================
High-performance, cross-platform duplicate file finder and safe organizer.
Supports Windows, macOS, Linux, and Android (Termux / CLI).

Key Features:
- 3-tier progressive filtering:
    1. Size match grouping (fast os.scandir)
    2. Fast partial hash filter (Head 64KB + Tail 64KB via xxhash/sha256)
    3. Full SHA-256 content verification (optimized 4MB streaming chunks)
- Multi-threaded hashing with HDD-friendly low-thrashing mode
- Dual Mode: Full modern CustomTkinter GUI + Headless Scriptable CLI
- Safe file retention policies: Keep Oldest, Keep Newest, Keep Shortest Path, Keep First
- Recycle Bin (send2trash) support + Safe Dry-Run toggle + CSV / JSON Export
- Cross-platform file manager integration (Explorer, Finder, xdg-open, termux-open)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import stat
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__version__ = "1.0.0"
__app_name__ = "SHA-256 Duplicate File Finder & Safe Organizer"

# ---------------------------------------------------------------------------
# Optional / Platform-Specific Dependencies
# ---------------------------------------------------------------------------

# Optional send2trash for Recycle Bin support
try:
    import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    send2trash = None
    HAS_SEND2TRASH = False

# Optional xxhash for ultra-fast partial hashing
try:
    import xxhash
    HAS_XXHASH = True
except ImportError:
    xxhash = None
    HAS_XXHASH = False

# Optional GUI dependencies (Tkinter / CustomTkinter)
GUI_AVAILABLE = False
GUI_ERROR_MSG = ""

# Ensure safe console encoding on Windows / legacy terminals
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import customtkinter as ctk
    GUI_AVAILABLE = True
except Exception as _gui_err:
    GUI_AVAILABLE = False
    GUI_ERROR_MSG = str(_gui_err)


# ---------------------------------------------------------------------------
# Core Utilities & Safe Deletion
# ---------------------------------------------------------------------------

def fmt_size(n: int | float) -> str:
    """Format bytes into human-readable representation."""
    n_float = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n_float < 1024.0:
            return f"{n_float:.1f} {unit}"
        n_float /= 1024.0
    return f"{n_float:.1f} PB"


def reveal_in_file_manager(file_path: Path) -> tuple[bool, str]:
    """Reveal a file or directory in the native OS file manager safely without shell injection."""
    import subprocess
    target = file_path.resolve()
    if not target.exists() and not target.is_symlink():
        return False, f"File does not exist: {target}"

    try:
        if sys.platform == "win32":
            subprocess.run(["explorer", f"/select,{str(target)}"], check=False)
            return True, "Opened in Windows Explorer"
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", str(target)], check=False)
            return True, "Opened in macOS Finder"
        else:
            # Check for Android Termux
            if "TERMUX_VERSION" in os.environ:
                try:
                    subprocess.run(["termux-open", str(target)], check=False)
                    return True, "Opened with termux-open"
                except FileNotFoundError:
                    pass

            # Linux desktop fallback
            parent = str(target.parent)
            try:
                subprocess.run(["xdg-open", parent], check=False)
                return True, f"Opened parent directory with xdg-open: {parent}"
            except FileNotFoundError:
                return False, f"xdg-open not available on this system. Path: {target}"
    except Exception as e:
        return False, f"Failed to reveal file: {e}"


def force_delete_file(p: Path) -> tuple[bool, str]:
    """
    Safely removes read-only, hidden, or system attributes before deleting files.
    Works across Windows, macOS, Linux, and Android.
    """
    try:
        if not p.exists() and not p.is_symlink():
            return True, "Already removed"

        # Protection: Never delete a directory directly via force_delete_file
        if p.is_dir() and not p.is_symlink():
            return False, "Target is a directory, not a file. Deletion refused for safety."

        # Step 1: Remove read-only attribute via chmod
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except Exception:
            pass

        # Step 2: On Windows, use Win32 API to reset file attributes to NORMAL (0x80)
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x80)
            except Exception:
                pass

        # Step 3: Perform unlink
        p.unlink(missing_ok=True)
        return True, "Success"
    except Exception as e:
        return False, str(e)


def move_to_trash(p: Path) -> tuple[bool, str]:
    """Move file to trash/recycle bin if supported on current OS/filesystem."""
    if not HAS_SEND2TRASH:
        return False, "send2trash library is not installed"

    try:
        if not p.exists() and not p.is_symlink():
            return True, "Already removed"

        if p.is_dir() and not p.is_symlink():
            return False, "Target is a directory, not a file."

        # Clear read-only flags first
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except Exception:
            pass

        send2trash.send2trash(str(p))
        return True, "Moved to Trash"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Core Duplicate Scanning & Hashing Engine
# ---------------------------------------------------------------------------

class DuplicateScannerEngine:
    """Core scanning & hashing worker decoupled from GUI and CLI."""

    def __init__(
        self,
        roots: list[Path | str],
        min_size_bytes: int = 1024,
        workers: int = 2,
        include_hidden: bool = False,
        follow_symlinks: bool = False,
        stop_event: Optional[threading.Event] = None,
        msg_queue: Optional[queue.Queue] = None,
        progress_callback: Optional[callable] = None
    ):
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self.min_size_bytes = max(0, min_size_bytes)
        self.workers = max(1, workers)
        self.include_hidden = include_hidden
        self.follow_symlinks = follow_symlinks
        self.stop_event = stop_event or threading.Event()
        self.msg_queue = msg_queue
        self.progress_callback = progress_callback

        # Results
        self.duplicate_groups: list[list[Path]] = []
        self.file_meta: dict[Path, tuple[int, float, str]] = {}  # path -> (size, mtime, digest)

    def _emit(self, msg_type: str, data):
        if self.msg_queue:
            self.msg_queue.put((msg_type, data))
        if self.progress_callback:
            self.progress_callback(msg_type, data)

    def run(self):
        try:
            # ----------------------------------------------------
            # Phase 1: Rapid Size Indexing via os.scandir
            # ----------------------------------------------------
            self._emit("phase", ("Phase 1/3: Scanning directories & indexing file sizes...", 0.0))
            size_map = defaultdict(list)
            total_files = 0
            total_bytes = 0

            def scan_dir(dir_path: Path):
                nonlocal total_files, total_bytes
                if self.stop_event.is_set():
                    return
                try:
                    with os.scandir(dir_path) as iterator:
                        for entry in iterator:
                            if self.stop_event.is_set():
                                return
                            if not self.include_hidden and entry.name.startswith("."):
                                continue
                            try:
                                if entry.is_dir(follow_symlinks=self.follow_symlinks):
                                    scan_dir(Path(entry.path))
                                elif entry.is_file(follow_symlinks=self.follow_symlinks):
                                    st = entry.stat(follow_symlinks=self.follow_symlinks)
                                    if st.st_size >= self.min_size_bytes:
                                        p = Path(entry.path)
                                        size_map[st.st_size].append(p)
                                        total_files += 1
                                        total_bytes += st.st_size
                                        if total_files % 1000 == 0:
                                            self._emit(
                                                "status_text",
                                                f"Found {total_files:,} files ({fmt_size(total_bytes)})..."
                                            )
                            except (PermissionError, OSError):
                                continue
                except (PermissionError, OSError):
                    pass

            for root in self.roots:
                if self.stop_event.is_set():
                    break
                if root.is_dir():
                    scan_dir(root)
                elif root.is_file():
                    try:
                        st = root.stat()
                        if st.st_size >= self.min_size_bytes:
                            size_map[st.st_size].append(root)
                            total_files += 1
                            total_bytes += st.st_size
                    except (PermissionError, OSError):
                        pass

            if self.stop_event.is_set():
                self._emit("done", "Scan stopped by user.")
                return

            candidates = {sz: paths for sz, paths in size_map.items() if len(paths) > 1}
            del size_map

            total_candidates = sum(len(paths) for paths in candidates.values())
            if not candidates:
                self._emit("done", f"Indexed {total_files:,} files. No duplicate sizes found.")
                return

            self._emit("status_text", f"Found {total_candidates:,} candidate files sharing identical sizes.")

            # ----------------------------------------------------
            # Phase 2: Fast Partial Hash Filtering (Head + Tail 64KB)
            # ----------------------------------------------------
            self._emit("phase", ("Phase 2/3: Quick partial hash verification...", 0.0))
            to_hash_partial = [(p, sz) for sz, paths in candidates.items() for p in paths]
            partial_map = defaultdict(list)

            def calc_partial_hash(item):
                import hashlib
                path_obj, sz = item
                try:
                    with open(path_obj, "rb") as f:
                        head = f.read(65536)
                        tail = b""
                        if sz > 131072:
                            f.seek(-65536, os.SEEK_END)
                            tail = f.read(65536)
                    data = head + tail
                    digest = xxhash.xxh64(data).hexdigest() if HAS_XXHASH else hashlib.sha256(data).hexdigest()
                    return (path_obj, sz, digest)
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futures = {ex.submit(calc_partial_hash, item): item for item in to_hash_partial}
                done_count = 0
                total_p = len(to_hash_partial)
                for fut in as_completed(futures):
                    if self.stop_event.is_set():
                        ex.shutdown(wait=False, cancel_futures=True)
                        self._emit("done", "Scan stopped by user.")
                        return
                    res = fut.result()
                    done_count += 1
                    if done_count % 100 == 0 or done_count == total_p:
                        self._emit("progress", done_count / total_p)
                    if res:
                        p, sz, ph = res
                        partial_map[(sz, ph)].append(p)

            # ----------------------------------------------------
            # Phase 3: Full SHA-256 Hashing on Confirmed Matches
            # ----------------------------------------------------
            full_candidates = [p for paths in partial_map.values() if len(paths) > 1 for p in paths]
            if not full_candidates:
                self._emit("done", "No duplicate files found after partial hash filter.")
                return

            self._emit(
                "phase",
                (f"Phase 3/3: Full SHA-256 hashing ({len(full_candidates)} candidate files)...", 0.0)
            )

            READ_BUFFER_SIZE = 4 * 1024 * 1024  # 4MB chunk buffer for optimal I/O

            def calc_full_sha256(p: Path):
                import hashlib
                try:
                    h = hashlib.sha256()
                    with open(p, "rb", buffering=READ_BUFFER_SIZE) as f:
                        while chunk := f.read(READ_BUFFER_SIZE):
                            h.update(chunk)
                    st = p.stat()
                    return (p, h.hexdigest(), st.st_size, st.st_mtime)
                except Exception:
                    return None

            hash_map = defaultdict(list)
            file_meta = {}

            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futures = {ex.submit(calc_full_sha256, p): p for p in full_candidates}
                done_count = 0
                total_f = len(full_candidates)
                for fut in as_completed(futures):
                    if self.stop_event.is_set():
                        ex.shutdown(wait=False, cancel_futures=True)
                        self._emit("done", "Scan stopped by user.")
                        return
                    res = fut.result()
                    done_count += 1
                    if done_count % 25 == 0 or done_count == total_f:
                        self._emit("progress", done_count / total_f)
                    if res:
                        p, digest, sz, mtime = res
                        hash_map[digest].append(p)
                        file_meta[p] = (sz, mtime, digest)

            final_groups = []
            for digest, paths in hash_map.items():
                if len(paths) > 1:
                    final_groups.append(sorted(paths))

            final_groups.sort(key=lambda g: -file_meta[g[0]][0])
            total_reclaimable = sum((len(g) - 1) * file_meta[g[0]][0] for g in final_groups)

            self.duplicate_groups = final_groups
            self.file_meta = file_meta

            self._emit("results", (final_groups, file_meta))
            self._emit(
                "done",
                f"Completed! Found {len(final_groups)} duplicate sets. Reclaimable space: {fmt_size(total_reclaimable)}"
            )

        except Exception as e:
            self._emit("done", f"Error during scan: {e}")

    @staticmethod
    def partition_group(
        group: list[Path],
        file_meta: dict[Path, tuple[int, float, str]],
        policy: str = "oldest"
    ) -> tuple[Path, list[Path]]:
        """
        Partition a duplicate group into (keeper, [duplicates_to_remove])
        according to the selected retention policy:
          - oldest: Keep file with earliest modification time
          - newest: Keep file with latest modification time
          - shortest: Keep file with shortest path string
          - first: Keep first file encountered
        """
        if not group:
            raise ValueError("Group cannot be empty")

        if policy == "oldest":
            sorted_paths = sorted(group, key=lambda p: file_meta.get(p, (0, 0, ""))[1])
        elif policy == "newest":
            sorted_paths = sorted(group, key=lambda p: -file_meta.get(p, (0, 0, ""))[1])
        elif policy == "shortest":
            sorted_paths = sorted(group, key=lambda p: len(str(p)))
        else:
            sorted_paths = list(group)

        return sorted_paths[0], sorted_paths[1:]


# ---------------------------------------------------------------------------
# Cross-Platform GUI Implementation (CustomTkinter)
# ---------------------------------------------------------------------------

if GUI_AVAILABLE:
    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")

    class SHA256DuplicateFinderGUI(ctk.CTk):
        def __init__(self, initial_paths: Optional[list[str]] = None):
            super().__init__()
            self.title(f"{__app_name__} v{__version__}")
            self.geometry("1200x840")
            self.minsize(1000, 700)

            self.msg_queue = queue.Queue()
            self.stop_event = threading.Event()
            self.duplicate_groups: list[list[Path]] = []
            self.file_info: dict[Path, tuple[int, float, str]] = {}
            self.scanning = False

            self._build_ui()
            if initial_paths:
                self.path_entry.insert(0, ";".join(initial_paths))

            self.after(100, self._poll_queue)

        def _get_platform_font_family(self) -> str:
            """Determine clean native font family based on host OS."""
            if sys.platform == "win32":
                return "Segoe UI"
            elif sys.platform == "darwin":
                return "SF Pro Text"
            else:
                return "Ubuntu"

        def _build_ui(self):
            font_family = self._get_platform_font_family()
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(3, weight=1)

            # 1. Header / Directory Selection Bar
            top_frame = ctk.CTkFrame(self, corner_radius=10)
            top_frame.grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 6))
            top_frame.grid_columnconfigure(1, weight=1)

            lbl_folder = ctk.CTkLabel(top_frame, text="Target Folders:", font=ctk.CTkFont(family=font_family, size=13, weight="bold"))
            lbl_folder.grid(row=0, column=0, padx=(15, 10), pady=12, sticky="w")

            self.path_entry = ctk.CTkEntry(
                top_frame,
                placeholder_text="Select directories to scan (separated by semicolon ';')",
                height=35,
                font=ctk.CTkFont(family=font_family, size=12)
            )
            self.path_entry.grid(row=0, column=1, padx=5, pady=12, sticky="ew")

            btn_browse = ctk.CTkButton(top_frame, text="📁 Browse", width=95, height=35, command=self._browse)
            btn_browse.grid(row=0, column=2, padx=5, pady=12)

            btn_clear = ctk.CTkButton(
                top_frame, text="Clear", width=70, height=35,
                fg_color="#546E7A", hover_color="#455A64",
                command=lambda: self.path_entry.delete(0, "end")
            )
            btn_clear.grid(row=0, column=3, padx=(5, 15), pady=12)

            # 2. Options & Controls Frame
            opts_frame = ctk.CTkFrame(self, corner_radius=10)
            opts_frame.grid(row=1, column=0, sticky="ew", padx=15, pady=4)

            # Min size
            ctk.CTkLabel(opts_frame, text="Min Size (KB):", font=ctk.CTkFont(family=font_family, size=12)).pack(side="left", padx=(15, 4), pady=10)
            self.min_size_var = ctk.StringVar(value="1024")
            ctk.CTkEntry(opts_frame, textvariable=self.min_size_var, width=65, height=28).pack(side="left", padx=(0, 15))

            # Workers
            ctk.CTkLabel(opts_frame, text="Threads:", font=ctk.CTkFont(family=font_family, size=12)).pack(side="left", padx=(0, 4))
            self.workers_var = ctk.StringVar(value="2")
            self.workers_entry = ctk.CTkEntry(opts_frame, textvariable=self.workers_var, width=45, height=28)
            self.workers_entry.pack(side="left", padx=(0, 15))

            # External HDD Mode Checkbox
            self.hdd_mode_var = ctk.BooleanVar(value=True)
            self.hdd_mode_cb = ctk.CTkCheckBox(
                opts_frame, text="External HDD Mode (Low Thrashing, 2 Threads)",
                variable=self.hdd_mode_var, font=ctk.CTkFont(family=font_family, size=12, weight="bold"),
                command=self._on_hdd_mode_toggle
            )
            self.hdd_mode_cb.pack(side="left", padx=10)

            # Hidden & Symlinks
            self.include_hidden = ctk.CTkCheckBox(opts_frame, text="Include Hidden", font=ctk.CTkFont(family=font_family, size=12))
            self.include_hidden.pack(side="left", padx=10)

            # Dry-Run toggle with color alert
            self.dry_run = ctk.CTkCheckBox(
                opts_frame, text="Dry-Run (Safe Mode)", font=ctk.CTkFont(family=font_family, size=12, weight="bold"),
                text_color="#FFA726", command=self._on_dry_run_toggle
            )
            self.dry_run.select()
            self.dry_run.pack(side="right", padx=15)

            # 3. Actions & Retention Policy Toolbar
            act_frame = ctk.CTkFrame(self, corner_radius=10)
            act_frame.grid(row=2, column=0, sticky="ew", padx=15, pady=4)

            self.scan_btn = ctk.CTkButton(
                act_frame, text="🔍 Start Scan", font=ctk.CTkFont(family=font_family, weight="bold"),
                fg_color="#2E7D32", hover_color="#1B5E20", width=120, height=34, command=self._start_scan
            )
            self.scan_btn.pack(side="left", padx=(15, 8), pady=8)

            self.stop_btn = ctk.CTkButton(
                act_frame, text="⏹ Stop", font=ctk.CTkFont(family=font_family, weight="bold"),
                fg_color="#C62828", hover_color="#B71C1C", width=80, height=34, state="disabled", command=self._stop_scan
            )
            self.stop_btn.pack(side="left", padx=5, pady=8)

            # Retention rule picker
            ctk.CTkLabel(act_frame, text="Retention Policy:", font=ctk.CTkFont(family=font_family, size=12, weight="bold")).pack(side="left", padx=(15, 5))
            self.retention_var = ctk.StringVar(value="Keep Oldest (Preserve Original)")
            self.retention_options = {
                "Keep Oldest (Preserve Original)": "oldest",
                "Keep Newest (Keep Latest)": "newest",
                "Keep Shortest Path": "shortest",
                "Keep First": "first"
            }
            self.retention_menu = ctk.CTkOptionMenu(
                act_frame,
                values=list(self.retention_options.keys()),
                variable=self.retention_var,
                width=240,
                height=30,
                command=lambda _: self._populate_tree() if self.duplicate_groups else None
            )
            self.retention_menu.pack(side="left", padx=5)

            # Action Buttons
            self.trash_btn = ctk.CTkButton(
                act_frame, text="🗑 Move Duplicates to Trash", font=ctk.CTkFont(family=font_family, weight="bold"),
                fg_color="#EF6C00", hover_color="#E65100", height=34, command=self._trash_selected
            )
            self.trash_btn.pack(side="left", padx=8)

            self.delete_btn = ctk.CTkButton(
                act_frame, text="❌ Permanently Delete", font=ctk.CTkFont(family=font_family, weight="bold"),
                fg_color="#D32F2F", hover_color="#B71C1C", height=34, command=self._delete_selected
            )
            self.delete_btn.pack(side="left", padx=5)

            self.export_btn = ctk.CTkButton(
                act_frame, text="📄 Export CSV", width=105, height=34,
                fg_color="#37474F", hover_color="#263238", command=self._export_csv
            )
            self.export_btn.pack(side="right", padx=(5, 15))

            # 4. Results & Treeview Display Area
            results_card = ctk.CTkFrame(self, corner_radius=10)
            results_card.grid(row=3, column=0, sticky="nsew", padx=15, pady=4)
            results_card.grid_columnconfigure(0, weight=1)
            results_card.grid_rowconfigure(0, weight=1)

            # Custom ttk Treeview Styling with OS-adaptive font
            style = ttk.Style()
            style.theme_use("clam")
            style.configure(
                "Treeview",
                background="#1E1E1E",
                foreground="#FFFFFF",
                fieldbackground="#1E1E1E",
                rowheight=26,
                font=(font_family, 10)
            )
            style.configure(
                "Treeview.Heading",
                background="#2D2D2D",
                foreground="#00E5FF",
                font=(font_family, 10, "bold"),
                relief="flat"
            )
            style.map("Treeview", background=[("selected", "#0D47A1")], foreground=[("selected", "#FFFFFF")])

            columns = ("group", "size", "count", "digest_path")
            self.tree = ttk.Treeview(results_card, columns=columns, show="tree headings", selectmode="extended")
            self.tree.heading("#0", text="Group / Status")
            self.tree.heading("group", text="#")
            self.tree.heading("size", text="File Size")
            self.tree.heading("count", text="Copies")
            self.tree.heading("digest_path", text="SHA-256 Digest / Full File Path")

            self.tree.column("#0", width=150, anchor="w")
            self.tree.column("group", width=50, anchor="center")
            self.tree.column("size", width=110, anchor="e")
            self.tree.column("count", width=65, anchor="center")
            self.tree.column("digest_path", width=750, anchor="w")

            self.tree.bind("<Double-1>", self._on_tree_double_click)

            vsb = ttk.Scrollbar(results_card, orient="vertical", command=self.tree.yview)
            hsb = ttk.Scrollbar(results_card, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

            self.tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(10, 0))
            vsb.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=(10, 0))
            hsb.grid(row=1, column=0, sticky="ew", padx=(10, 0), pady=(0, 10))

            # 5. Progress, Status & Log Footer
            bottom_frame = ctk.CTkFrame(self, corner_radius=10)
            bottom_frame.grid(row=4, column=0, sticky="ew", padx=15, pady=(4, 15))
            bottom_frame.grid_columnconfigure(0, weight=1)

            self.progress = ctk.CTkProgressBar(bottom_frame, height=12)
            self.progress.grid(row=0, column=0, sticky="ew", padx=15, pady=(10, 4))
            self.progress.set(0)

            self.status_label = ctk.CTkLabel(
                bottom_frame,
                text="💡 Tip: Keep HDD Mode active (2 threads) for mechanical hard drives. Uncheck 'Dry-Run' when ready to delete.",
                anchor="w",
                font=ctk.CTkFont(family=font_family, size=12)
            )
            self.status_label.grid(row=1, column=0, sticky="ew", padx=15, pady=(2, 6))

            mono_font = "Consolas" if sys.platform == "win32" else ("Menlo" if sys.platform == "darwin" else "Monospace")
            self.log_box = ctk.CTkTextbox(bottom_frame, height=90, font=ctk.CTkFont(family=mono_font, size=11))
            self.log_box.grid(row=2, column=0, sticky="ew", padx=15, pady=(0, 10))

        def _on_hdd_mode_toggle(self):
            if self.hdd_mode_var.get():
                self.workers_var.set("2")
                self._log("HDD Mode Enabled: Set to 2 threads to prevent mechanical drive head thrashing.")
            else:
                self.workers_var.set(str(max(4, os.cpu_count() or 4)))
                self._log(f"SSD / Multi-core Mode: Set to {self.workers_var.get()} threads.")

        def _on_dry_run_toggle(self):
            if self.dry_run.get():
                self._log("Dry-Run mode is ON: File operations will be simulated (no files deleted).")
            else:
                self._log("⚠️ Dry-Run mode is OFF: Delete actions will PERMANENTLY remove files.")

        def _log(self, msg: str):
            timestamp = time.strftime('%H:%M:%S')
            self.log_box.insert("end", f"[{timestamp}] {msg}\n")
            self.log_box.see("end")

        def _browse(self):
            folder = filedialog.askdirectory(mustexist=True, title="Select Directory to Scan")
            if folder:
                cur = self.path_entry.get().strip()
                if cur:
                    self.path_entry.insert("end", f";{folder}")
                else:
                    self.path_entry.insert(0, folder)

        def _on_tree_double_click(self, event):
            item_id = self.tree.focus()
            if not item_id:
                return
            vals = self.tree.item(item_id, "values")
            if vals and len(vals) >= 4:
                potential_path = Path(vals[3])
                if potential_path.is_file():
                    success, msg = reveal_in_file_manager(potential_path)
                    if not success:
                        self._log(f"⚠️ {msg}")

        def _start_scan(self):
            raw_paths = self.path_entry.get().strip()
            if not raw_paths:
                messagebox.showwarning("Missing Folder", "Please select at least one folder to scan.")
                return

            roots = [p.strip() for p in raw_paths.split(";") if p.strip()]
            for r in roots:
                if not Path(r).exists():
                    messagebox.showerror("Invalid Directory", f"The target does not exist:\n{r}")
                    return

            try:
                min_kb = float(self.min_size_var.get())
                workers = int(self.workers_var.get())
            except ValueError:
                messagebox.showerror("Invalid Parameters", "Min Size and Thread count must be valid numbers.")
                return

            self.scanning = True
            self.stop_event.clear()
            self.scan_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.tree.delete(*self.tree.get_children())
            self.duplicate_groups.clear()
            self.file_info.clear()
            self.progress.set(0)
            self._log(f"Starting duplicate scan on {len(roots)} target folder(s) with {workers} thread(s)...")

            engine = DuplicateScannerEngine(
                roots=roots,
                min_size_bytes=int(min_kb * 1024),
                workers=workers,
                include_hidden=bool(self.include_hidden.get()),
                follow_symlinks=False,
                stop_event=self.stop_event,
                msg_queue=self.msg_queue
            )

            threading.Thread(target=engine.run, daemon=True).start()

        def _stop_scan(self):
            self.stop_event.set()
            self._log("Cancellation requested. Halting threads...")

        def _poll_queue(self):
            try:
                while True:
                    msg_type, data = self.msg_queue.get_nowait()
                    if msg_type == "phase":
                        text, prog = data
                        self.status_label.configure(text=text)
                        self._log(text)
                        if prog >= 0:
                            self.progress.set(prog)
                    elif msg_type == "status_text":
                        self.status_label.configure(text=data)
                    elif msg_type == "progress":
                        self.progress.set(data)
                    elif msg_type == "results":
                        groups, meta = data
                        self.duplicate_groups = groups
                        self.file_info = meta
                        self._populate_tree()
                    elif msg_type == "done":
                        self.status_label.configure(text=data)
                        self._log(data)
                        self.scanning = False
                        self.scan_btn.configure(state="normal")
                        self.stop_btn.configure(state="disabled")
                        self.progress.set(1.0)
            except queue.Empty:
                pass
            self.after(100, self._poll_queue)

        def _populate_tree(self):
            self.tree.delete(*self.tree.get_children())
            policy_key = self.retention_options.get(self.retention_var.get(), "oldest")

            for idx, group in enumerate(self.duplicate_groups):
                size = self.file_info[group[0]][0]
                digest = self.file_info[group[0]][2]
                parent_id = f"group_{idx}"

                self.tree.insert(
                    "", "end", iid=parent_id, text=f"📂 Group {idx+1}", open=True,
                    values=(idx + 1, fmt_size(size), len(group), f"SHA-256: {digest}")
                )

                to_keep, to_remove = DuplicateScannerEngine.partition_group(group, self.file_info, policy=policy_key)

                # Keeper
                mtime_keep = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.file_info[to_keep][1]))
                self.tree.insert(
                    parent_id, "end", iid=f"keep_{to_keep}", text="  🔒 [KEEP ORIGINAL]",
                    values=("", mtime_keep, "", str(to_keep))
                )

                # Duplicates targeted for removal
                for fpath in to_remove:
                    mtime_dup = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.file_info[fpath][1]))
                    self.tree.insert(
                        parent_id, "end", iid=f"dup_{fpath}", text="  ❌ [DUPLICATE]",
                        values=("", mtime_dup, "", str(fpath))
                    )

        def _get_files_to_remove(self) -> list[Path]:
            selected_items = self.tree.selection()
            to_delete = []
            policy_key = self.retention_options.get(self.retention_var.get(), "oldest")

            if selected_items:
                selected_groups_indices = set()
                for item_id in selected_items:
                    if item_id.startswith("group_"):
                        selected_groups_indices.add(int(item_id.split("_")[1]))
                    elif item_id.startswith("dup_"):
                        file_path_str = item_id[4:]
                        to_delete.append(Path(file_path_str))
                    elif item_id.startswith("keep_"):
                        pass

                for g_idx in selected_groups_indices:
                    if g_idx < len(self.duplicate_groups):
                        _, dups = DuplicateScannerEngine.partition_group(
                            self.duplicate_groups[g_idx], self.file_info, policy=policy_key
                        )
                        to_delete.extend(dups)

            if not to_delete and self.duplicate_groups:
                if messagebox.askyesno(
                    "Process All Duplicates?",
                    f"No specific rows selected.\n\nDo you want to process ALL {sum(len(g)-1 for g in self.duplicate_groups)} duplicate files across {len(self.duplicate_groups)} groups?"
                ):
                    for g in self.duplicate_groups:
                        _, dups = DuplicateScannerEngine.partition_group(g, self.file_info, policy=policy_key)
                        to_delete.extend(dups)

            # Deduplicate while preserving order
            seen = set()
            unique_to_delete = []
            for p in to_delete:
                if p not in seen:
                    seen.add(p)
                    unique_to_delete.append(p)

            return unique_to_delete

        def _delete_selected(self):
            to_delete = self._get_files_to_remove()
            if not to_delete:
                return

            if self.dry_run.get():
                messagebox.showwarning(
                    "Dry-Run Mode Active",
                    f"⚠️ [DRY-RUN IS ENABLED - NO FILES TOUCHED]\n\n"
                    f"Would permanently delete {len(to_delete)} duplicate files.\n\n"
                    f"To ACTUALLY delete files:\n"
                    f"1. Uncheck the 'Dry-Run (Safe Mode)' checkbox at top right.\n"
                    f"2. Click 'Permanently Delete' again."
                )
                self._log(f"[DRY-RUN] Simulated permanent deletion of {len(to_delete)} files.")
                return

            if not messagebox.askyesno(
                "Confirm Permanent Deletion",
                f"Are you sure you want to PERMANENTLY DELETE {len(to_delete)} duplicate files?\n\n"
                f"This will remove the files directly from the drive.\n\n"
                f"This action cannot be undone!",
                icon="warning"
            ):
                return

            deleted_count = 0
            failed_count = 0
            deleted_paths = []

            for p in to_delete:
                success, msg = force_delete_file(p)
                if success:
                    deleted_count += 1
                    deleted_paths.append(p)
                else:
                    failed_count += 1
                    self._log(f"❌ Failed to delete {p}: {msg}")

            self._log(f"Permanently deleted {deleted_count} files. (Failed: {failed_count})")
            if failed_count > 0:
                messagebox.showwarning(
                    "Deletion Completed with Errors",
                    f"Deleted {deleted_count} files.\n\n{failed_count} file(s) could not be deleted (check log for details or ensure files are not open in another program)."
                )
            else:
                messagebox.showinfo("Done", f"Successfully deleted {deleted_count} duplicate files.")

            self._refresh_after_removal(deleted_paths)

        def _trash_selected(self):
            if not HAS_SEND2TRASH:
                messagebox.showerror(
                    "Dependency Missing",
                    "The 'send2trash' package is required for Recycle Bin operations.\n\n"
                    "Install with: pip install send2trash\n\n"
                    "Or use 'Permanently Delete' directly."
                )
                return

            to_trash = self._get_files_to_remove()
            if not to_trash:
                return

            if self.dry_run.get():
                messagebox.showwarning(
                    "Dry-Run Mode Active",
                    f"⚠️ [DRY-RUN IS ENABLED - NO FILES TOUCHED]\n\n"
                    f"Would move {len(to_trash)} duplicate files to the Recycle Bin / Trash.\n\n"
                    f"To ACTUALLY move files to Recycle Bin:\n"
                    f"1. Uncheck the 'Dry-Run (Safe Mode)' checkbox at top right.\n"
                    f"2. Click 'Move Duplicates to Trash' again."
                )
                self._log(f"[DRY-RUN] Simulated moving {len(to_trash)} files to Recycle Bin / Trash.")
                return

            if not messagebox.askyesno(
                "Confirm Move to Trash",
                f"Move {len(to_trash)} duplicate files to the Recycle Bin / Trash?"
            ):
                return

            trashed_count = 0
            failed_count = 0
            trashed_paths = []

            for p in to_trash:
                success, msg = move_to_trash(p)
                if success:
                    trashed_count += 1
                    trashed_paths.append(p)
                else:
                    failed_count += 1
                    self._log(f"❌ Failed to trash {p}: {msg}")

            self._log(f"Moved {trashed_count} duplicate files to Trash. (Failed: {failed_count})")
            if failed_count > 0:
                messagebox.showwarning(
                    "Recycle Bin Notice",
                    f"Moved {trashed_count} files to Trash.\n\n"
                    f"{failed_count} file(s) could not be moved to Trash.\n"
                    f"(Note: Many external drives do not support Recycle Bin. Use 'Permanently Delete' if needed)."
                )
            else:
                messagebox.showinfo("Done", f"Successfully moved {trashed_count} duplicate files to Trash.")

            self._refresh_after_removal(trashed_paths)

        def _refresh_after_removal(self, removed_paths: list[Path]):
            removed_set = set(removed_paths)
            updated_groups = []
            for g in self.duplicate_groups:
                rem = [p for p in g if p not in removed_set]
                if len(rem) > 1:
                    updated_groups.append(rem)
            self.duplicate_groups = updated_groups
            self._populate_tree()

        def _export_csv(self):
            if not self.duplicate_groups:
                messagebox.showinfo("No Data", "No duplicates available to export.")
                return

            save_path = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
                title="Export Duplicate Report"
            )
            if not save_path:
                return

            policy_key = self.retention_options.get(self.retention_var.get(), "oldest")
            try:
                export_csv_report(save_path, self.duplicate_groups, self.file_info, policy=policy_key)
                self._log(f"Exported duplicate report to: {save_path}")
                messagebox.showinfo("Export Successful", f"Report saved successfully to:\n{save_path}")
            except Exception as e:
                messagebox.showerror("Export Error", f"Failed to export CSV: {e}")


# ---------------------------------------------------------------------------
# Report Exporters (CSV / JSON)
# ---------------------------------------------------------------------------

def export_csv_report(
    save_path: str | Path,
    duplicate_groups: list[list[Path]],
    file_meta: dict[Path, tuple[int, float, str]],
    policy: str = "oldest"
):
    """Export duplicate analysis report to CSV format."""
    with open(save_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Group", "Status", "SHA-256", "Size (Bytes)", "Modified Date", "File Path"])
        for idx, group in enumerate(duplicate_groups, 1):
            to_keep, to_remove = DuplicateScannerEngine.partition_group(group, file_meta, policy=policy)
            size = file_meta[to_keep][0]
            digest = file_meta[to_keep][2]

            mtime_k = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(file_meta[to_keep][1]))
            writer.writerow([idx, "KEEP_ORIGINAL", digest, size, mtime_k, str(to_keep)])

            for d in to_remove:
                mtime_d = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(file_meta[d][1]))
                writer.writerow([idx, "DUPLICATE", digest, size, mtime_d, str(d)])


def export_json_report(
    save_path: str | Path,
    duplicate_groups: list[list[Path]],
    file_meta: dict[Path, tuple[int, float, str]],
    policy: str = "oldest"
):
    """Export duplicate analysis report to JSON format."""
    report_data = {
        "generator": f"{__app_name__} v{__version__}",
        "generated_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "retention_policy": policy,
        "total_groups": len(duplicate_groups),
        "total_duplicates": sum(len(g) - 1 for g in duplicate_groups),
        "reclaimable_bytes": sum((len(g) - 1) * file_meta[g[0]][0] for g in duplicate_groups),
        "groups": []
    }

    for idx, group in enumerate(duplicate_groups, 1):
        to_keep, to_remove = DuplicateScannerEngine.partition_group(group, file_meta, policy=policy)
        size = file_meta[to_keep][0]
        digest = file_meta[to_keep][2]

        group_obj = {
            "group_id": idx,
            "sha256": digest,
            "size_bytes": size,
            "keeper": {
                "path": str(to_keep),
                "modified": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(file_meta[to_keep][1]))
            },
            "duplicates": [
                {
                    "path": str(d),
                    "modified": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(file_meta[d][1]))
                }
                for d in to_remove
            ]
        }
        report_data["groups"].append(group_obj)

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)


# ---------------------------------------------------------------------------
# CLI Implementation (for Android Termux, Headless Servers, and Terminal Users)
# ---------------------------------------------------------------------------

def run_cli(args: argparse.Namespace) -> int:
    """Run duplicate scanner in terminal / headless CLI mode."""
    print(f"===========================================================")
    print(f" {__app_name__} v{__version__}")
    print(f" Progressive 3-Tier Scanning Engine (Size -> Partial -> SHA-256)")
    print(f"===========================================================\n")

    targets = [Path(p).expanduser().resolve() for p in args.paths]
    for t in targets:
        if not t.exists():
            print(f"[ERROR] Target path does not exist: {t}", file=sys.stderr)
            return 1

    workers = 2 if args.hdd_mode else (args.workers or max(4, os.cpu_count() or 4))
    min_size_bytes = int(args.min_size_kb * 1024)

    print(f"[*] Target Directories : {', '.join(str(t) for t in targets)}")
    print(f"[*] Minimum File Size  : {args.min_size_kb} KB ({min_size_bytes:,} bytes)")
    print(f"[*] Workers            : {workers} threads ({'HDD Low-Thrashing Mode' if args.hdd_mode else 'High-Performance Multi-Core'})")
    print(f"[*] Retention Policy   : {args.retention}")
    print(f"[*] Include Hidden     : {args.include_hidden}")
    print(f"[*] Dry-Run Mode       : {args.dry_run}")
    print("-" * 59)

    def on_progress(msg_type: str, data):
        if msg_type == "phase":
            text, _ = data
            print(f"\n[+] {text}")
        elif msg_type == "status_text":
            print(f"    -> {data}")
        elif msg_type == "progress":
            pct = int(data * 100)
            sys.stdout.write(f"\r    Progress: {pct}%")
            sys.stdout.flush()
        elif msg_type == "done":
            print(f"\n[*] {data}")

    engine = DuplicateScannerEngine(
        roots=targets,
        min_size_bytes=min_size_bytes,
        workers=workers,
        include_hidden=args.include_hidden,
        follow_symlinks=args.follow_symlinks,
        progress_callback=on_progress
    )

    engine.run()

    if not engine.duplicate_groups:
        print("\n[SUCCESS] No duplicates found. Your storage is clean!")
        return 0

    print(f"\n{'='*59}")
    print(f" DUPLICATE ANALYSIS RESULTS ({len(engine.duplicate_groups)} Groups Found)")
    print(f"{'='*59}")

    total_dups = sum(len(g) - 1 for g in engine.duplicate_groups)
    reclaimable_bytes = sum((len(g) - 1) * engine.file_meta[g[0]][0] for g in engine.duplicate_groups)
    print(f"Total duplicate files to remove : {total_dups:,}")
    print(f"Total reclaimable disk space    : {fmt_size(reclaimable_bytes)}\n")

    # Print summary of top groups
    for idx, group in enumerate(engine.duplicate_groups[:15], 1):
        to_keep, to_remove = DuplicateScannerEngine.partition_group(group, engine.file_meta, policy=args.retention)
        sz = engine.file_meta[to_keep][0]
        digest = engine.file_meta[to_keep][2]
        print(f"Group #{idx} [{fmt_size(sz)} each | SHA-256: {digest[:16]}...]")
        print(f"  [KEEP]      {to_keep}")
        for d in to_remove:
            print(f"  [DUPLICATE] {d}")
        print()

    if len(engine.duplicate_groups) > 15:
        print(f"  ... and {len(engine.duplicate_groups) - 15} more groups (export to CSV/JSON to view all).\n")

    # Export CSV if requested
    if args.export_csv:
        csv_path = Path(args.export_csv).resolve()
        export_csv_report(csv_path, engine.duplicate_groups, engine.file_meta, policy=args.retention)
        print(f"[SUCCESS] CSV report saved to: {csv_path}")

    # Export JSON if requested
    if args.export_json:
        json_path = Path(args.export_json).resolve()
        export_json_report(json_path, engine.duplicate_groups, engine.file_meta, policy=args.retention)
        print(f"[SUCCESS] JSON report saved to: {json_path}")

    # Process deletions / trash if specified
    if args.delete or args.trash:
        all_to_remove: list[Path] = []
        for group in engine.duplicate_groups:
            _, dups = DuplicateScannerEngine.partition_group(group, engine.file_meta, policy=args.retention)
            all_to_remove.extend(dups)

        if args.dry_run:
            action_name = "Trash" if args.trash else "Permanent Deletion"
            print(f"\n[SAFETY ALERT - DRY RUN ACTIVE]")
            print(f"Simulated {action_name} of {len(all_to_remove)} duplicate files.")
            print("Pass '--no-dry-run' and '--yes' to execute real deletion.")
            return 0

        # Confirmation required unless --yes passed
        if not args.yes:
            action = "trash" if args.trash else "PERMANENTLY DELETE"
            confirm = input(f"\nAre you sure you want to {action} {len(all_to_remove)} duplicate files? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("[!] Operation aborted by user.")
                return 0

        success_count = 0
        failed_count = 0
        for p in all_to_remove:
            if args.trash:
                ok, err = move_to_trash(p)
            else:
                ok, err = force_delete_file(p)

            if ok:
                success_count += 1
            else:
                failed_count += 1
                print(f"[FAIL] {p}: {err}", file=sys.stderr)

        verb = "moved to trash" if args.trash else "permanently deleted"
        print(f"\n[SUCCESS] Successfully {verb} {success_count:,} duplicate files. (Failed: {failed_count})")

    return 0


# ---------------------------------------------------------------------------
# Argument Parser & Entry Point
# ---------------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sha256_duplicate_finder",
        description=f"{__app_name__} (v{__version__}): Cross-platform duplicate file finder with 3-tier progressive filtering and safe retention policies."
    )
    parser.add_argument("paths", nargs="*", default=[], help="Directories or folders to scan for duplicate files.")
    parser.add_argument("--cli", action="store_true", help="Force headless CLI mode even if GUI display is available.")
    parser.add_argument("--gui", action="store_true", help="Force GUI mode (fails if display is unavailable).")
    parser.add_argument("--min-size-kb", type=float, default=1024.0, help="Minimum file size to index in KB (default: 1024 KB = 1 MB).")
    parser.add_argument("--workers", type=int, default=None, help="Number of worker threads (default: 2 for HDD mode, auto-detect for SSD).")
    parser.add_argument("--hdd-mode", action=argparse.BooleanOptionalAction, default=True, help="Enable low-thrashing mode (2 threads) for mechanical external HDDs.")
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden files (starting with dot or hidden attribute).")
    parser.add_argument("--follow-symlinks", action="store_true", help="Follow directory symlinks (caution: may cause infinite loops).")
    parser.add_argument("--retention", choices=["oldest", "newest", "shortest", "first"], default="oldest", help="Retention policy for choosing original file to keep (default: oldest).")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True, help="Dry-run simulation mode. Enabled by default for safety.")
    parser.add_argument("--trash", action="store_true", help="Move duplicate files to system Trash / Recycle Bin.")
    parser.add_argument("--delete", action="store_true", help="Permanently delete duplicate files.")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt for automated CLI operations.")
    parser.add_argument("--export-csv", type=str, default="", help="Path to export CSV duplicate analysis report.")
    parser.add_argument("--export-json", type=str, default="", help="Path to export JSON duplicate analysis report.")
    parser.add_argument("--version", "-v", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args()


def main():
    args = parse_arguments()

    # Determine whether to run in CLI mode:
    # 1. User explicitly passed --cli
    # 2. CLI action requested (--delete, --trash, --export-csv, --export-json)
    # 3. GUI is requested but not available
    # 4. GUI is not available (e.g., Android Termux, headless Linux / SSH)
    force_cli = args.cli or bool(args.delete or args.trash or args.export_csv or args.export_json)

    if not force_cli and not args.gui and not GUI_AVAILABLE:
        # If GUI is missing, inform user and fall back cleanly to CLI
        print(f"[*] Note: Graphical interface (Tkinter/CustomTkinter) is not available ({GUI_ERROR_MSG or 'No DISPLAY found'}).")
        print(f"[*] Switching automatically to Headless CLI Mode.\n")
        force_cli = True

    if force_cli:
        if not args.paths:
            print("[ERROR] Please provide at least one target directory to scan in CLI mode.\n")
            print("Usage: python sha256_duplicate_finder.py /path/to/scan [options]")
            print("Run with --help for all available options.")
            sys.exit(1)
        sys.exit(run_cli(args))
    else:
        if not GUI_AVAILABLE:
            print(f"[FATAL] Cannot launch GUI: {GUI_ERROR_MSG}", file=sys.stderr)
            print("Install GUI dependencies: pip install customtkinter", file=sys.stderr)
            sys.exit(1)

        initial_paths = [str(Path(p).resolve()) for p in args.paths] if args.paths else None
        app = SHA256DuplicateFinderGUI(initial_paths=initial_paths)
        app.mainloop()


if __name__ == "__main__":
    main()
