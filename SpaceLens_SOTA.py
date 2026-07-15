"""
SpaceLens SOTA - a free TreeSize-inspired disk usage explorer built from research-backed scanning ideas.

Run:
    python SpaceLens_SOTA.py

No third-party packages required. Built with tkinter.
Python 3.10+ recommended.

Highlights:
- Background folder scanning so the UI stays responsive
- Lazy-loaded folder tree for large scans
- Dark / light mode
- Quick scan buttons for common folders
- Folder/file size tree, type breakdown, largest files
- Duplicate candidate finder with optional SHA-256 verification
- Cleanup suggestions for old huge files, archives, installers, cache folders, etc.
- Snapshot save and snapshot comparison
- CSV / JSON / HTML exports
- Safer open / reveal / copy path / recycle-bin actions
"""

from __future__ import annotations

import csv
import ctypes
import datetime as _dt
import gzip
import hashlib
import heapq
import itertools
import multiprocessing
import html
import json
import os
import platform
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Dict, Iterable, List, Optional, Tuple

APP_NAME = "SpaceLens SOTA"
APP_VERSION = "3.0"
MAX_LARGEST_FILES = 2500
MAX_TYPE_ROWS = 700
MAX_SEARCH_RESULTS = 3000
MAX_DUPLICATE_GROUPS = 600
MAX_CLEANUP_ROWS = 1200
MAX_COMPARE_ROWS = 2000
MAX_CHILDREN_PER_FOLDER = 7000
MAX_FILE_CHILDREN_PER_FOLDER = 300  # file-leaf compression: keep the tree fast but still track every file in tables
HASH_CHUNK_SIZE = 1024 * 1024

INSTALLER_EXTS = {".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".iso", ".appx", ".msix"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".heic"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".txt", ".md"}
CACHE_NAMES = {"cache", "caches", "temp", "tmp", "__pycache__", ".cache"}
DEV_HEAVY_NAMES = {"node_modules", ".venv", "venv", "target", "dist", "build", ".gradle"}


@dataclass
class ScanStats:
    scanned_items: int = 0
    permission_errors: int = 0
    other_errors: int = 0
    skipped_links: int = 0
    skipped_reparse_or_seen: int = 0
    start_time: float = field(default_factory=time.time)


@dataclass
class FileRecord:
    path: str
    size: int
    modified: float
    created: float
    ext: str


@dataclass
class Node:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    files: int = 0
    folders: int = 0
    modified: float = 0.0
    created: float = 0.0
    error: Optional[str] = None
    parent: Optional["Node"] = None
    children: List["Node"] = field(default_factory=list)

    def sort_deep(self) -> None:
        self.children.sort(key=lambda n: (not n.is_dir, -n.size, n.name.lower()))
        for child in self.children:
            if child.is_dir:
                child.sort_deep()


@dataclass
class CleanupSuggestion:
    reason: str
    size: int
    modified: float
    path: str
    kind: str


def unique_cleanup_suggestions(
    suggestions: Iterable[CleanupSuggestion], limit: int = MAX_CLEANUP_ROWS
) -> List[CleanupSuggestion]:
    """Return the largest suggestion for each path without double counting it."""
    unique: List[CleanupSuggestion] = []
    seen: set[str] = set()
    for suggestion in sorted(suggestions, key=lambda item: item.size, reverse=True):
        if suggestion.path in seen:
            continue
        seen.add(suggestion.path)
        unique.append(suggestion)
        if len(unique) >= limit:
            break
    return unique


class Scanner:
    """Fast disk scanner using SpaceLens' custom Hot-Zone Sprint method.

    This is intentionally different from a plain recursive os.walk clone:
    - Uses os.scandir / DirEntry metadata so each entry needs fewer OS metadata calls.
    - On Windows, tries FindFirstFileExW with FIND_FIRST_EX_LARGE_FETCH for larger directory batches.
    - Scans direct files first, then visits likely space-heavy folders earlier with a hot-zone priority order.
    - Keeps the app responsive with progress micro-batches instead of updating the UI per file.

    It does not read the NTFS MFT directly. MFT/USN mode would be the next aggressive Windows-only
    step, but this version stays dependency-free and safer for normal users.
    """

    HOT_FOLDER_NAMES = {
        "downloads": 120,
        "desktop": 95,
        "videos": 90,
        "captures": 88,
        "recordings": 88,
        "steamapps": 90,
        "epic games": 80,
        "riot games": 75,
        "appdata": 72,
        "local": 68,
        "temp": 66,
        "tmp": 66,
        "cache": 66,
        "caches": 66,
        "node_modules": 64,
        ".gradle": 60,
        "target": 58,
        "build": 52,
        "dist": 52,
    }

    def __init__(self, root_path: str, out_queue: queue.Queue, cancel_event: threading.Event, size_hints: Optional[Dict[str, int]] = None):
        self.root_path = os.path.abspath(root_path)
        self.out_queue = out_queue
        self.cancel_event = cancel_event
        self.stats = ScanStats()
        self.ext_sizes: Dict[str, Tuple[int, int]] = {}
        self.largest_files: List[Tuple[int, str, float]] = []
        self.all_files: List[FileRecord] = []
        self._progress_last = 0.0
        self._seen_dirs: set[Tuple[int, int]] = set()
        self._use_win32_large_fetch = platform.system().lower() == "windows"
        self.size_hints = size_hints or {}
        self.backend_name = "adaptive-scandir+win32-large-fetch" if self._use_win32_large_fetch else "adaptive-scandir"

    def scan(self) -> None:
        try:
            root = self._scan_dir(self.root_path, None, depth=0, known_times=None)
            if root:
                root.sort_deep()
            self.out_queue.put(("scan_done", root, self.stats, self.ext_sizes, self._top_largest_files(), self.all_files))
        except Exception as exc:
            self.out_queue.put(("fatal", str(exc)))

    def _scan_dir(self, path: str, parent: Optional[Node], depth: int, known_times: Optional[Tuple[float, float]]) -> Optional[Node]:
        if self.cancel_event.is_set():
            return None

        name = os.path.basename(path.rstrip(os.sep)) or path
        try:
            st = os.stat(path, follow_symlinks=False)
            modified = float(st.st_mtime)
            created = float(st.st_ctime)
            key = (getattr(st, "st_dev", 0), getattr(st, "st_ino", 0))
            if key != (0, 0):
                if key in self._seen_dirs:
                    self.stats.skipped_reparse_or_seen += 1
                    return Node(name=name, path=path, is_dir=True, error="already scanned", parent=parent)
                self._seen_dirs.add(key)
        except PermissionError:
            self.stats.permission_errors += 1
            return Node(name=name, path=path, is_dir=True, error="permission denied", parent=parent)
        except OSError as exc:
            self.stats.other_errors += 1
            return Node(name=name, path=path, is_dir=True, error=str(exc), parent=parent)

        if known_times:
            # The directory listing already gave us these timestamps; keep the freshest values.
            modified = max(modified, float(known_times[0]))
            created = created or float(known_times[1])

        node = Node(name=name, path=path, is_dir=True, modified=modified, created=created, parent=parent)
        self.stats.scanned_items += 1
        self._send_progress(path)

        child_dirs: List[Tuple[int, str, str, float, float]] = []
        file_child_candidates: List[Tuple[int, str, str, float, float]] = []
        try:
            iterator = self._iter_entries_win32_large_fetch(path) if self._use_win32_large_fetch else self._iter_entries_scandir(path)
            for entry_name, child_path, is_dir, is_reparse, size, child_modified, child_created in iterator:
                if self.cancel_event.is_set():
                    break
                if is_reparse:
                    self.stats.skipped_reparse_or_seen += 1
                    continue
                node.modified = max(node.modified, child_modified)
                if is_dir:
                    priority = self._priority_for_dir(entry_name, child_path, depth + 1)
                    child_dirs.append((priority, entry_name.lower(), child_path, child_modified, child_created))
                else:
                    size = max(int(size), 0)
                    node.size += size
                    node.files += 1
                    self.stats.scanned_items += 1
                    self._record_file(child_path, size, child_modified, child_created)
                    # File-leaf compression: keep only the largest direct files as tree nodes.
                    # Every file is still recorded for largest-file view, duplicate detection, exports, and cleanup.
                    candidate = (size, entry_name, child_path, child_modified, child_created)
                    if len(file_child_candidates) < MAX_FILE_CHILDREN_PER_FOLDER:
                        heapq.heappush(file_child_candidates, candidate)
                    elif size > file_child_candidates[0][0]:
                        heapq.heapreplace(file_child_candidates, candidate)
                    self._send_progress(child_path)
        except PermissionError:
            node.error = "permission denied"
            self.stats.permission_errors += 1
        except OSError as exc:
            node.error = str(exc)
            self.stats.other_errors += 1

        # Add compressed file leaves after scanning the folder. This avoids creating huge numbers
        # of tiny tree nodes in folders with thousands of files.
        for size, entry_name, child_path, child_modified, child_created in sorted(file_child_candidates, key=lambda row: (-row[0], row[1].lower())):
            node.children.append(Node(
                name=entry_name, path=child_path, is_dir=False, size=size, files=1, folders=0,
                modified=child_modified, created=child_created, parent=node,
            ))

        # Hot-zone order: visit folders that tend to hide huge files first. The final tree is still
        # sorted by actual size after the scan completes.
        child_dirs.sort()
        for _priority, _sort_name, child_path, child_modified, child_created in child_dirs:
            if self.cancel_event.is_set():
                break
            child = self._scan_dir(child_path, node, depth + 1, (child_modified, child_created))
            if not child:
                continue
            node.children.append(child)
            node.size += child.size
            node.files += child.files
            node.folders += 1 + child.folders
            node.modified = max(node.modified, child.modified or node.modified)

        return node

    def _priority_for_dir(self, name: str, path: str, depth: int) -> int:
        """Lower values scan earlier.

        The priority combines:
        - hot-zone names from the report, such as downloads, videos, appdata, cache, node_modules
        - the latest snapshot's size estimate, so previously huge folders get scanned first
        - depth penalty, so the scanner does not get trapped too deep before covering nearby folders
        """
        lower = name.lower()
        hot = self.HOT_FOLDER_NAMES.get(lower, 0)
        if lower in CACHE_NAMES or lower in DEV_HEAVY_NAMES:
            hot += 35
        hinted_size = int(self.size_hints.get(os.path.abspath(path), 0) or 0)
        if hinted_size:
            # log2 scaling prevents one giant directory from completely starving everything else.
            hot += min(110, int(hinted_size.bit_length() * 4))
        return depth * 35 - hot

    def _iter_entries_scandir(self, path: str):
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_symlink():
                    yield (entry.name, entry.path, False, True, 0, 0.0, 0.0)
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    st = entry.stat(follow_symlinks=False)
                    yield (
                        entry.name,
                        entry.path,
                        is_dir,
                        False,
                        max(int(getattr(st, "st_size", 0)), 0),
                        float(st.st_mtime),
                        float(st.st_ctime),
                    )
                except PermissionError:
                    self.stats.permission_errors += 1
                except OSError:
                    self.stats.other_errors += 1

    def _iter_entries_win32_large_fetch(self, path: str):
        """Windows-only directory iterator using FindFirstFileExW + LARGE_FETCH."""
        FILE_ATTRIBUTE_DIRECTORY = 0x10
        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        FIND_FIRST_EX_LARGE_FETCH = 0x2
        FindExInfoBasic = 1
        FindExSearchNameMatch = 0
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

        class WIN32_FIND_DATAW(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", ctypes.c_uint32),
                ("ftCreationTime", FILETIME),
                ("ftLastAccessTime", FILETIME),
                ("ftLastWriteTime", FILETIME),
                ("nFileSizeHigh", ctypes.c_uint32),
                ("nFileSizeLow", ctypes.c_uint32),
                ("dwReserved0", ctypes.c_uint32),
                ("dwReserved1", ctypes.c_uint32),
                ("cFileName", ctypes.c_wchar * 260),
                ("cAlternateFileName", ctypes.c_wchar * 14),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        FindFirstFileExW = kernel32.FindFirstFileExW
        FindFirstFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        FindFirstFileExW.restype = ctypes.c_void_p
        FindNextFileW = kernel32.FindNextFileW
        FindNextFileW.argtypes = [ctypes.c_void_p, ctypes.POINTER(WIN32_FIND_DATAW)]
        FindNextFileW.restype = ctypes.c_int
        FindClose = kernel32.FindClose
        FindClose.argtypes = [ctypes.c_void_p]
        FindClose.restype = ctypes.c_int

        def to_win_long(p: str) -> str:
            p = os.path.abspath(p)
            if p.startswith("\\\\?\\"):
                return p
            if p.startswith("\\\\"):
                return "\\\\?\\UNC\\" + p[2:]
            return "\\\\?\\" + p

        def filetime_to_unix(ft: FILETIME) -> float:
            value = (int(ft.dwHighDateTime) << 32) + int(ft.dwLowDateTime)
            if value <= 0:
                return 0.0
            return (value - 116444736000000000) / 10_000_000

        data = WIN32_FIND_DATAW()
        search = to_win_long(os.path.join(path, "*"))
        handle = FindFirstFileExW(search, FindExInfoBasic, ctypes.byref(data), FindExSearchNameMatch, None, FIND_FIRST_EX_LARGE_FETCH)
        if handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise OSError(err, ctypes.FormatError(err), path)
        try:
            while True:
                name = data.cFileName
                if name not in (".", ".."):
                    attrs = int(data.dwFileAttributes)
                    is_dir = bool(attrs & FILE_ATTRIBUTE_DIRECTORY)
                    is_reparse = bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
                    size = (int(data.nFileSizeHigh) << 32) + int(data.nFileSizeLow)
                    child_path = os.path.join(path, name)
                    yield (name, child_path, is_dir, is_reparse, size, filetime_to_unix(data.ftLastWriteTime), filetime_to_unix(data.ftCreationTime))
                if not FindNextFileW(handle, ctypes.byref(data)):
                    err = ctypes.get_last_error()
                    if err == 18:  # ERROR_NO_MORE_FILES
                        break
                    raise OSError(err, ctypes.FormatError(err), path)
        finally:
            FindClose(handle)

    def _send_progress(self, current_path: str) -> None:
        now = time.time()
        if now - self._progress_last > 0.10:
            self._progress_last = now
            elapsed = max(now - self.stats.start_time, 0.01)
            rate = self.stats.scanned_items / elapsed
            self.out_queue.put(("scan_progress", self.stats.scanned_items, rate, current_path))

    def _record_file(self, path: str, size: int, modified: float, created: float) -> None:
        ext = Path(path).suffix.lower() or "[no extension]"
        total, count = self.ext_sizes.get(ext, (0, 0))
        self.ext_sizes[ext] = (total + size, count + 1)
        rec = FileRecord(path=path, size=size, modified=modified, created=created, ext=ext)
        self.all_files.append(rec)
        self.largest_files.append((size, path, modified))
        if len(self.largest_files) > MAX_LARGEST_FILES * 2:
            self.largest_files.sort(reverse=True, key=lambda item: item[0])
            del self.largest_files[MAX_LARGEST_FILES:]

    def _top_largest_files(self) -> List[Tuple[int, str, float]]:
        self.largest_files.sort(reverse=True, key=lambda item: item[0])
        return self.largest_files[:MAX_LARGEST_FILES]


# ---------- helpers ----------

def format_size(num: int) -> str:
    sign = "-" if num < 0 else ""
    value = float(abs(num))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            if unit == "B":
                return f"{sign}{int(value):,} {unit}"
            return f"{sign}{value:,.2f} {unit}"
        value /= 1024
    return f"{num:,} B"


def parse_size_to_bytes(value: str, unit: str) -> int:
    try:
        number = float(value.strip() or "0")
    except ValueError:
        number = 0
    multipliers = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(max(number, 0) * multipliers.get(unit, 1))


def format_time(ts: float) -> str:
    if not ts:
        return ""
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def safe_percent(part: int, whole: int) -> str:
    if whole <= 0:
        return "0.00%"
    return f"{(part / whole) * 100:,.2f}%"


def age_days(ts: float) -> int:
    if not ts:
        return 0
    return max(0, int((time.time() - ts) / 86400))


def truncate_middle(text: str, max_len: int = 100) -> str:
    if len(text) <= max_len:
        return text
    keep = max_len - 3
    return text[: keep // 2] + "..." + text[-(keep // 2) :]


def category_for_ext(ext: str) -> str:
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in DOC_EXTS:
        return "document"
    if ext in INSTALLER_EXTS:
        return "installer"
    return "other"


def open_path(path: str) -> None:
    if not path or not os.path.exists(path):
        messagebox.showwarning("Path not found", "That path does not exist anymore.")
        return
    system = platform.system().lower()
    try:
        if system == "windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as exc:
        messagebox.showerror("Open failed", str(exc))


def reveal_path(path: str) -> None:
    if not path:
        return
    system = platform.system().lower()
    try:
        if system == "windows":
            subprocess.run(["explorer", "/select,", os.path.normpath(path)], check=False)
        elif system == "darwin":
            subprocess.run(["open", "-R", path], check=False)
        else:
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            subprocess.run(["xdg-open", folder], check=False)
    except Exception as exc:
        messagebox.showerror("Reveal failed", str(exc))


def open_terminal_here(path: str) -> None:
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not folder or not os.path.isdir(folder):
        messagebox.showwarning("Folder not found", "Could not find a folder for that item.")
        return
    system = platform.system().lower()
    try:
        if system == "windows":
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(["cmd.exe", "/K", f'cd /d "{folder}"'], creationflags=flags)
        elif system == "darwin":
            subprocess.run(["open", "-a", "Terminal", folder], check=False)
        else:
            for cmd in (["x-terminal-emulator"], ["gnome-terminal"], ["konsole"], ["xfce4-terminal"]):
                try:
                    subprocess.Popen(cmd, cwd=folder)
                    return
                except FileNotFoundError:
                    continue
            subprocess.run(["xdg-open", folder], check=False)
    except Exception as exc:
        messagebox.showerror("Terminal failed", str(exc))


def recycle_or_delete(path: str) -> bool:
    if not os.path.exists(path):
        messagebox.showwarning("Not found", "That item does not exist anymore.")
        return False

    if platform.system().lower() == "windows":
        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x0040
        FOF_NOCONFIRMATION = 0x0010
        FOF_SILENT = 0x0004

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_bool),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p),
            ]

        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = os.path.abspath(path) + "\0\0"
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))  # type: ignore[attr-defined]
        return result == 0 and not op.fAnyOperationsAborted

    answer = messagebox.askyesno(
        "Permanent delete",
        "Recycle Bin support is only built in for Windows. Permanently delete this item instead?",
    )
    if not answer:
        return False
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    return True


def hash_file(path: str, cancel_event: threading.Event) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                if cancel_event.is_set():
                    return None
                chunk = f.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def partial_hash_file(path: str, cancel_event: threading.Event, sample_size: int = HASH_CHUNK_SIZE) -> Optional[str]:
    """Fast duplicate prefilter: hash only the first chunk.

    The duplicate pipeline is size -> partial SHA-256 -> full SHA-256.
    This saves a lot of reading when large files share size but not contents.
    """
    try:
        with open(path, "rb") as f:
            if cancel_event.is_set():
                return None
            data = f.read(sample_size)
        return hashlib.sha256(data).hexdigest()
    except OSError:
        return None


# ---------- research-backed snapshot cache ----------

class SnapshotDB:
    """Small SQLite cache for SOTA-style incremental thinking.

    This is not a real NTFS change-journal index yet. It is the safe portable layer:
    - remembers previous scan sizes
    - supplies directory-size hints to the adaptive scanner
    - lets the app compare current scans against older cached scans
    - avoids forcing users to manually manage snapshot files
    """

    def __init__(self) -> None:
        app_dir = Path.home() / ".spacelens_sota"
        app_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = app_dir / "snapshots.sqlite3"
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(str(self.db_path))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    total_size INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    folder_count INTEGER NOT NULL,
                    duration REAL NOT NULL,
                    app_version TEXT NOT NULL,
                    backend TEXT NOT NULL
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS nodes (
                    snapshot_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    is_dir INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    folder_count INTEGER NOT NULL,
                    modified REAL NOT NULL,
                    created REAL NOT NULL,
                    ext TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, path)
                )"""
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_nodes_snapshot_size ON nodes(snapshot_id, size DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_root_time ON snapshots(root, created_at DESC)")

    def latest_snapshot_id(self, root: str) -> Optional[int]:
        root = os.path.abspath(root)
        with self._connect() as con:
            row = con.execute(
                "SELECT id FROM snapshots WHERE root=? ORDER BY created_at DESC LIMIT 1", (root,)
            ).fetchone()
            return int(row[0]) if row else None

    def size_hints(self, root: str, limit: int = 200000) -> Dict[str, int]:
        snap_id = self.latest_snapshot_id(root)
        if not snap_id:
            return {}
        with self._connect() as con:
            rows = con.execute(
                "SELECT path, size FROM nodes WHERE snapshot_id=? AND is_dir=1 AND size>0 ORDER BY size DESC LIMIT ?",
                (snap_id, limit),
            ).fetchall()
        return {os.path.abspath(path): int(size) for path, size in rows}

    def save_current_scan(self, root_node: Node, stats: ScanStats, duration: float, backend: str = "adaptive-scandir") -> int:
        rows = []
        stack = [root_node]
        while stack:
            n = stack.pop()
            ext = "" if n.is_dir else os.path.splitext(n.name)[1].lower()
            rows.append((
                n.path, n.name, 1 if n.is_dir else 0, int(n.size), int(n.files), int(n.folders),
                float(n.modified or 0), float(n.created or 0), ext,
            ))
            stack.extend(n.children)
        with self._connect() as con:
            cur = con.execute(
                """INSERT INTO snapshots(root, created_at, total_size, file_count, folder_count, duration, app_version, backend)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (os.path.abspath(root_node.path), _dt.datetime.now().isoformat(timespec="seconds"), int(root_node.size), int(root_node.files), int(root_node.folders), float(duration), APP_VERSION, backend),
            )
            snap_id = int(cur.lastrowid)
            con.executemany(
                """INSERT INTO nodes(snapshot_id, path, name, is_dir, size, file_count, folder_count, modified, created, ext)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(snap_id,) + row for row in rows],
            )
        return snap_id

    def list_snapshots(self, root: Optional[str] = None, limit: int = 20) -> List[Tuple[int, str, str, int, int, int, float, str]]:
        with self._connect() as con:
            if root:
                rows = con.execute(
                    """SELECT id, created_at, root, total_size, file_count, folder_count, duration, backend
                       FROM snapshots WHERE root=? ORDER BY created_at DESC LIMIT ?""",
                    (os.path.abspath(root), limit),
                ).fetchall()
            else:
                rows = con.execute(
                    """SELECT id, created_at, root, total_size, file_count, folder_count, duration, backend
                       FROM snapshots ORDER BY created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        return [(int(a), b, c, int(d), int(e), int(f), float(g), h) for a,b,c,d,e,f,g,h in rows]

    def load_tree(self, snapshot_id: int) -> Optional[Node]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT path, name, is_dir, size, file_count, folder_count, modified, created, ext
                   FROM nodes WHERE snapshot_id=? ORDER BY LENGTH(path) ASC""",
                (int(snapshot_id),),
            ).fetchall()
        if not rows:
            return None
        nodes: Dict[str, Node] = {}
        root_node: Optional[Node] = None
        for path, name, is_dir, size, files, folders, modified, created, _ext in rows:
            n = Node(
                name=name, path=path, is_dir=bool(is_dir), size=int(size), files=int(files), folders=int(folders),
                modified=float(modified or 0), created=float(created or 0), parent=None,
            )
            nodes[path] = n
            parent_path = os.path.dirname(path.rstrip(os.sep))
            parent = nodes.get(parent_path)
            if parent and parent is not n:
                n.parent = parent
                parent.children.append(n)
            elif root_node is None:
                root_node = n
        if root_node:
            root_node.sort_deep()
        return root_node

    def compare_to_snapshot(self, root_node: Node, snapshot_id: int, limit: int = MAX_COMPARE_ROWS) -> List[Tuple[int, int, int, str, str]]:
        current = {}
        stack = [root_node]
        while stack:
            n = stack.pop()
            current[n.path] = (n.size, "folder" if n.is_dir else "file")
            stack.extend(n.children)
        with self._connect() as con:
            old_rows = con.execute(
                "SELECT path, size, is_dir FROM nodes WHERE snapshot_id=?", (int(snapshot_id),)
            ).fetchall()
        old = {p: (int(size), "folder" if is_dir else "file") for p, size, is_dir in old_rows}
        paths = set(current) | set(old)
        out = []
        for path in paths:
            new_size, kind = current.get(path, (0, old.get(path, (0, "file"))[1]))
            old_size, old_kind = old.get(path, (0, kind))
            change = int(new_size) - int(old_size)
            if change:
                out.append((change, int(old_size), int(new_size), kind or old_kind, path))
        out.sort(key=lambda row: abs(row[0]), reverse=True)
        return out[:limit]


class FastIndexBackend:
    """Future backend hook for true NTFS MFT/USN support.

    The research report shows this is the only realistic way to approach WizTree/Everything speed.
    It is left explicit instead of pretending raw volume parsing is already implemented.
    """

    name = "experimental-ntfs-mft"

    @staticmethod
    def available() -> bool:
        return platform.system().lower() == "windows"

    @staticmethod
    def explain() -> str:
        return (
            "Raw MFT/USN indexing requires admin-level NTFS volume access. "
            "SpaceLens SOTA v3 uses the portable adaptive scanner by default and keeps this backend as a safe extension point."
        )


# ---------- app ----------

class SpaceLensApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1320x820")
        self.minsize(1040, 640)

        self.root_node: Optional[Node] = None
        self.stats: Optional[ScanStats] = None
        self.ext_sizes: Dict[str, Tuple[int, int]] = {}
        self.all_files: List[FileRecord] = []
        self.largest_files: List[Tuple[int, str, float]] = []
        self.node_by_iid: Dict[str, Node] = {}
        self.iid_by_node_id: Dict[int, str] = {}
        self.loaded_iids: set[str] = set()
        self.cleanup_by_iid: Dict[str, CleanupSuggestion] = {}
        self.path_by_iid: Dict[str, str] = {}
        self.compare_path_by_iid: Dict[str, str] = {}
        self.treemap_rects: List[Tuple[int, int, int, int, Node]] = []
        self.map_focus_node: Optional[Node] = None
        self.map_history: List[Node] = []
        self.current_duplicate_hash_thread: Optional[threading.Thread] = None
        self.snapshot_db = SnapshotDB()
        self.current_scan_backend = "adaptive-scandir"

        self.scan_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.hash_cancel_event = threading.Event()
        self.scan_thread: Optional[threading.Thread] = None
        self.scan_start_time = 0.0

        self.current_folder = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="choose a folder to scan")
        self.search_var = tk.StringVar(value="")
        self.include_files_var = tk.BooleanVar(value=True)
        self.case_var = tk.BooleanVar(value=False)
        self.min_size_var = tk.StringVar(value="0")
        self.min_size_unit_var = tk.StringVar(value="MB")
        self.dup_min_size_var = tk.StringVar(value="5")
        self.dup_min_unit_var = tk.StringVar(value="MB")
        self.theme_mode = tk.StringVar(value="dark")

        self.style = ttk.Style(self)
        self._setup_style()
        self._build_ui()
        self._apply_theme()
        self.populate_cache_tab()
        self.after(100, self._poll_queue)

    # ---------- ui ----------

    def _setup_style(self) -> None:
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.default_font = ("Segoe UI", 10)
        self.heading_font = ("Segoe UI", 10, "bold")
        self.title_font = ("Segoe UI", 15, "bold")

    def _build_ui(self) -> None:
        self.top = ttk.Frame(self, padding=(10, 8))
        self.top.pack(side=tk.TOP, fill=tk.X)

        title = ttk.Label(self.top, text="SpaceLens SOTA", font=self.title_font)
        title.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(self.top, text="Folder:").pack(side=tk.LEFT)
        self.folder_entry = ttk.Entry(self.top, textvariable=self.current_folder)
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 8))
        self.folder_entry.bind("<Return>", lambda _e: self.start_scan())

        ttk.Button(self.top, text="Browse", command=self.browse_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(self.top, text="Scan", style="Accent.TButton", command=self.start_scan).pack(side=tk.LEFT, padx=2)
        ttk.Button(self.top, text="Cancel", command=self.cancel_scan).pack(side=tk.LEFT, padx=2)
        ttk.Button(self.top, text="Dark/Light", command=self.toggle_theme).pack(side=tk.LEFT, padx=(8, 2))

        self.quick_bar = ttk.Frame(self, padding=(10, 0, 10, 8))
        self.quick_bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(self.quick_bar, text="quick scan:").pack(side=tk.LEFT, padx=(0, 6))
        for label, path in self._quick_paths():
            ttk.Button(self.quick_bar, text=label, command=lambda p=path: self.scan_path(p)).pack(side=tk.LEFT, padx=2)

        self.filter_bar = ttk.Frame(self, padding=(10, 0, 10, 8))
        self.filter_bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(self.filter_bar, text="Search:").pack(side=tk.LEFT)
        search = ttk.Entry(self.filter_bar, textvariable=self.search_var, width=34)
        search.pack(side=tk.LEFT, padx=(6, 8))
        search.bind("<Return>", lambda _e: self.apply_search())
        ttk.Button(self.filter_bar, text="Find", command=self.apply_search).pack(side=tk.LEFT, padx=2)
        ttk.Button(self.filter_bar, text="Clear", command=self.clear_search).pack(side=tk.LEFT, padx=2)
        ttk.Checkbutton(self.filter_bar, text="Include files", variable=self.include_files_var).pack(side=tk.LEFT, padx=(12, 2))
        ttk.Checkbutton(self.filter_bar, text="Case sensitive", variable=self.case_var).pack(side=tk.LEFT, padx=2)
        ttk.Label(self.filter_bar, text="   over").pack(side=tk.LEFT)
        ttk.Entry(self.filter_bar, textvariable=self.min_size_var, width=7).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Combobox(self.filter_bar, textvariable=self.min_size_unit_var, values=("KB", "MB", "GB", "TB"), width=4, state="readonly").pack(side=tk.LEFT)
        ttk.Button(self.filter_bar, text="Filter size", command=self.apply_size_filter).pack(side=tk.LEFT, padx=4)
        ttk.Button(self.filter_bar, text="Export", command=self.show_export_menu).pack(side=tk.RIGHT, padx=2)
        ttk.Button(self.filter_bar, text="SOTA notes", command=self.show_sota_notes).pack(side=tk.RIGHT, padx=2)
        ttk.Button(self.filter_bar, text="Load cache", command=self.load_latest_cache).pack(side=tk.RIGHT, padx=2)
        ttk.Button(self.filter_bar, text="Cache compare", command=self.compare_cached_snapshot).pack(side=tk.RIGHT, padx=2)
        ttk.Button(self.filter_bar, text="Save snapshot", command=self.save_snapshot).pack(side=tk.RIGHT, padx=2)
        ttk.Button(self.filter_bar, text="Compare file", command=self.compare_snapshot).pack(side=tk.RIGHT, padx=2)

        self.paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        left = ttk.Frame(self.paned)
        right = ttk.Frame(self.paned)
        self.paned.add(left, weight=3)
        self.paned.add(right, weight=2)

        columns = ("size", "percent", "files", "folders", "modified", "path")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Name", command=lambda: self.sort_visible_tree("name", False))
        self.tree.heading("size", text="Size", command=lambda: self.sort_visible_tree("size", True))
        self.tree.heading("percent", text="%", command=lambda: self.sort_visible_tree("percent", True))
        self.tree.heading("files", text="Files", command=lambda: self.sort_visible_tree("files", True))
        self.tree.heading("folders", text="Folders", command=lambda: self.sort_visible_tree("folders", True))
        self.tree.heading("modified", text="Modified", command=lambda: self.sort_visible_tree("modified", True))
        self.tree.heading("path", text="Path")
        self.tree.column("#0", width=270, minwidth=180)
        self.tree.column("size", width=105, anchor=tk.E)
        self.tree.column("percent", width=75, anchor=tk.E)
        self.tree.column("files", width=80, anchor=tk.E)
        self.tree.column("folders", width=80, anchor=tk.E)
        self.tree.column("modified", width=135, anchor=tk.CENTER)
        self.tree.column("path", width=420)

        vsb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(left, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<<TreeviewOpen>>", self.on_tree_open)
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.tree.bind("<Button-3>", self.show_context_menu)

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Open", command=self.open_selected)
        self.context_menu.add_command(label="Reveal in file manager", command=self.reveal_selected)
        self.context_menu.add_command(label="Copy path", command=self.copy_selected_path)
        self.context_menu.add_command(label="Open terminal here", command=self.terminal_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Rescan this folder", command=self.rescan_selected)
        self.context_menu.add_command(label="Move to Recycle Bin", command=self.delete_selected)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.summary_tab = ttk.Frame(self.notebook, padding=10)
        self.types_tab = ttk.Frame(self.notebook, padding=10)
        self.large_tab = ttk.Frame(self.notebook, padding=10)
        self.dup_tab = ttk.Frame(self.notebook, padding=10)
        self.cleanup_tab = ttk.Frame(self.notebook, padding=10)
        self.map_tab = ttk.Frame(self.notebook, padding=10)
        self.compare_tab = ttk.Frame(self.notebook, padding=10)
        self.cache_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.summary_tab, text="Summary")
        self.notebook.add(self.types_tab, text="Types")
        self.notebook.add(self.large_tab, text="Largest")
        self.notebook.add(self.dup_tab, text="Duplicates")
        self.notebook.add(self.cleanup_tab, text="Cleanup")
        self.notebook.add(self.map_tab, text="Treemap")
        self.notebook.add(self.compare_tab, text="Compare")
        self.notebook.add(self.cache_tab, text="Cache")

        self.summary_text = tk.Text(self.summary_tab, height=10, wrap="word", borderwidth=0, font=self.default_font)
        self.summary_text.pack(fill=tk.BOTH, expand=True)
        self.summary_text.configure(state=tk.DISABLED)

        self.types_tree = ttk.Treeview(self.types_tab, columns=("ext", "category", "size", "percent", "count"), show="headings")
        for col, text, width, anchor in (
            ("ext", "Extension", 130, tk.W),
            ("category", "Category", 105, tk.W),
            ("size", "Size", 110, tk.E),
            ("percent", "%", 80, tk.E),
            ("count", "Files", 90, tk.E),
        ):
            self.types_tree.heading(col, text=text)
            self.types_tree.column(col, width=width, anchor=anchor)
        self.types_tree.pack(fill=tk.BOTH, expand=True)

        self.large_tree = ttk.Treeview(self.large_tab, columns=("size", "age", "modified", "path"), show="headings")
        for col, text, width, anchor in (
            ("size", "Size", 110, tk.E),
            ("age", "Age", 90, tk.E),
            ("modified", "Modified", 135, tk.CENTER),
            ("path", "Path", 520, tk.W),
        ):
            self.large_tree.heading(col, text=text)
            self.large_tree.column(col, width=width, anchor=anchor)
        self.large_tree.pack(fill=tk.BOTH, expand=True)
        self.large_tree.bind("<Double-1>", lambda _e: self.open_path_from_tree(self.large_tree))
        self.large_tree.bind("<Button-3>", lambda e: self.show_path_tree_menu(e, self.large_tree))

        dup_top = ttk.Frame(self.dup_tab)
        dup_top.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(dup_top, text="minimum duplicate size").pack(side=tk.LEFT)
        ttk.Entry(dup_top, textvariable=self.dup_min_size_var, width=7).pack(side=tk.LEFT, padx=(6, 2))
        ttk.Combobox(dup_top, textvariable=self.dup_min_unit_var, values=("KB", "MB", "GB"), width=4, state="readonly").pack(side=tk.LEFT)
        ttk.Button(dup_top, text="Refresh candidates", command=self.populate_duplicates).pack(side=tk.LEFT, padx=6)
        ttk.Button(dup_top, text="Verify with SHA-256", command=self.verify_duplicates_with_hash).pack(side=tk.LEFT, padx=2)
        ttk.Button(dup_top, text="Cancel hash", command=self.cancel_hashing).pack(side=tk.LEFT, padx=2)
        self.dup_tree = ttk.Treeview(self.dup_tab, columns=("size", "count", "status", "path"), show="tree headings")
        self.dup_tree.heading("#0", text="Group / file")
        for col, text, width, anchor in (
            ("size", "Size", 110, tk.E),
            ("count", "Count", 70, tk.E),
            ("status", "Status", 130, tk.W),
            ("path", "Path", 520, tk.W),
        ):
            self.dup_tree.heading(col, text=text)
            self.dup_tree.column(col, width=width, anchor=anchor)
        self.dup_tree.column("#0", width=220)
        self.dup_tree.pack(fill=tk.BOTH, expand=True)
        self.dup_tree.bind("<Double-1>", lambda _e: self.open_path_from_tree(self.dup_tree))
        self.dup_tree.bind("<Button-3>", lambda e: self.show_path_tree_menu(e, self.dup_tree))

        cleanup_top = ttk.Frame(self.cleanup_tab)
        cleanup_top.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(cleanup_top, text="suggestions are not auto-deleted; review before removing anything").pack(side=tk.LEFT)
        ttk.Button(cleanup_top, text="Refresh", command=self.populate_cleanup).pack(side=tk.RIGHT)
        self.cleanup_tree = ttk.Treeview(self.cleanup_tab, columns=("reason", "size", "age", "modified", "path"), show="headings")
        for col, text, width, anchor in (
            ("reason", "Reason", 190, tk.W),
            ("size", "Size", 110, tk.E),
            ("age", "Age", 90, tk.E),
            ("modified", "Modified", 135, tk.CENTER),
            ("path", "Path", 520, tk.W),
        ):
            self.cleanup_tree.heading(col, text=text)
            self.cleanup_tree.column(col, width=width, anchor=anchor)
        self.cleanup_tree.pack(fill=tk.BOTH, expand=True)
        self.cleanup_tree.bind("<Double-1>", lambda _e: self.open_path_from_tree(self.cleanup_tree))
        self.cleanup_tree.bind("<Button-3>", lambda e: self.show_path_tree_menu(e, self.cleanup_tree))

        map_toolbar = ttk.Frame(self.map_tab)
        map_toolbar.pack(fill=tk.X, pady=(0, 8))
        self.map_label = ttk.Label(map_toolbar, text="select a folder to map")
        self.map_label.pack(side=tk.LEFT)
        ttk.Button(map_toolbar, text="Back", command=self.map_back).pack(side=tk.RIGHT, padx=2)
        ttk.Button(map_toolbar, text="Root", command=self.map_root).pack(side=tk.RIGHT, padx=2)
        ttk.Button(map_toolbar, text="Refresh", command=self.draw_treemap).pack(side=tk.RIGHT, padx=2)
        self.treemap = tk.Canvas(self.map_tab, highlightthickness=1)
        self.treemap.pack(fill=tk.BOTH, expand=True)
        self.treemap.bind("<Configure>", lambda _e: self.draw_treemap())
        self.treemap.bind("<Button-1>", self.on_treemap_click)
        self.treemap.bind("<Double-1>", self.on_treemap_double_click)

        compare_top = ttk.Frame(self.compare_tab)
        compare_top.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(compare_top, text="load an older snapshot to see what grew or shrank").pack(side=tk.LEFT)
        ttk.Button(compare_top, text="Compare snapshot", command=self.compare_snapshot).pack(side=tk.RIGHT)
        self.compare_tree = ttk.Treeview(self.compare_tab, columns=("change", "old", "new", "kind", "path"), show="headings")
        for col, text, width, anchor in (
            ("change", "Change", 110, tk.E),
            ("old", "Old", 110, tk.E),
            ("new", "New", 110, tk.E),
            ("kind", "Type", 80, tk.W),
            ("path", "Path", 560, tk.W),
        ):
            self.compare_tree.heading(col, text=text)
            self.compare_tree.column(col, width=width, anchor=anchor)
        self.compare_tree.pack(fill=tk.BOTH, expand=True)
        self.compare_tree.bind("<Double-1>", lambda _e: self.open_path_from_tree(self.compare_tree))
        self.compare_tree.bind("<Button-3>", lambda e: self.show_path_tree_menu(e, self.compare_tree))

        cache_top = ttk.Frame(self.cache_tab)
        cache_top.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(cache_top, text="automatic SQLite snapshots used for size hints and comparisons").pack(side=tk.LEFT)
        ttk.Button(cache_top, text="Refresh", command=self.populate_cache_tab).pack(side=tk.RIGHT, padx=2)
        ttk.Button(cache_top, text="Compare selected", command=self.compare_selected_cache_snapshot).pack(side=tk.RIGHT, padx=2)
        self.cache_tree = ttk.Treeview(self.cache_tab, columns=("time", "size", "files", "folders", "duration", "backend", "root"), show="headings")
        for col, text, width, anchor in (
            ("time", "Time", 150, tk.W),
            ("size", "Size", 110, tk.E),
            ("files", "Files", 90, tk.E),
            ("folders", "Folders", 90, tk.E),
            ("duration", "Seconds", 85, tk.E),
            ("backend", "Backend", 150, tk.W),
            ("root", "Root", 520, tk.W),
        ):
            self.cache_tree.heading(col, text=text)
            self.cache_tree.column(col, width=width, anchor=anchor)
        self.cache_tree.pack(fill=tk.BOTH, expand=True)
        self.cache_snapshot_by_iid: Dict[str, int] = {}

        self.path_menu = tk.Menu(self, tearoff=0)
        self.path_menu.add_command(label="Open", command=lambda: self.open_context_path())
        self.path_menu.add_command(label="Reveal in file manager", command=lambda: self.reveal_context_path())
        self.path_menu.add_command(label="Copy path", command=lambda: self.copy_context_path())
        self.path_menu.add_command(label="Open terminal here", command=lambda: self.terminal_context_path())
        self.path_menu.add_separator()
        self.path_menu.add_command(label="Move to Recycle Bin", command=lambda: self.delete_context_path())
        self._context_path = ""

        bottom = ttk.Frame(self, padding=(10, 0, 10, 8))
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Label(bottom, textvariable=self.status_text).pack(side=tk.RIGHT)

    def _quick_paths(self) -> List[Tuple[str, str]]:
        home = str(Path.home())
        paths: List[Tuple[str, str]] = [("home", home)]
        system = platform.system().lower()
        if system == "windows":
            system_drive = os.environ.get("SystemDrive", "C:") + os.sep
            paths.insert(0, ("c drive", system_drive))
        else:
            paths.insert(0, ("root", os.sep))
        for name in ("Desktop", "Downloads", "Documents", "Pictures", "Videos"):
            p = os.path.join(home, name)
            if os.path.isdir(p):
                paths.append((name.lower(), p))
        if system == "windows":
            for label, env in (("temp", "TEMP"), ("appdata", "APPDATA"), ("localappdata", "LOCALAPPDATA")):
                p = os.environ.get(env)
                if p and os.path.isdir(p):
                    paths.append((label, p))
        else:
            for label, p in (("tmp", "/tmp"), ("cache", os.path.join(home, ".cache"))):
                if os.path.isdir(p):
                    paths.append((label, p))
        return paths[:12]

    def _apply_theme(self) -> None:
        dark = self.theme_mode.get() == "dark"
        if dark:
            bg = "#15171c"
            panel = "#1f232b"
            text = "#ecedf1"
            muted = "#aab0bd"
            entry = "#252a33"
            accent = "#6aa5ff"
            tree = "#191d24"
            select = "#30496f"
        else:
            bg = "#f4f5f7"
            panel = "#ffffff"
            text = "#1d2430"
            muted = "#4e5664"
            entry = "#ffffff"
            accent = "#2f6fdd"
            tree = "#ffffff"
            select = "#cfe2ff"

        self.colors = {"bg": bg, "panel": panel, "text": text, "muted": muted, "entry": entry, "accent": accent, "tree": tree, "select": select}
        self.configure(bg=bg)
        self.style.configure(".", background=bg, foreground=text, font=self.default_font)
        self.style.configure("TFrame", background=bg)
        self.style.configure("TLabel", background=bg, foreground=text)
        self.style.configure("TButton", padding=(8, 4), background=panel, foreground=text)
        self.style.map("TButton", background=[("active", entry)])
        self.style.configure("Accent.TButton", font=self.heading_font, foreground=text, background=accent)
        self.style.configure("TEntry", fieldbackground=entry, foreground=text, insertcolor=text)
        self.style.configure("TCombobox", fieldbackground=entry, foreground=text, background=entry, arrowcolor=text)
        self.style.configure("TCheckbutton", background=bg, foreground=text)
        self.style.configure("TNotebook", background=bg, borderwidth=0)
        self.style.configure("TNotebook.Tab", background=panel, foreground=text, padding=(10, 5))
        self.style.map("TNotebook.Tab", background=[("selected", entry)])
        self.style.configure("Treeview", rowheight=25, background=tree, fieldbackground=tree, foreground=text, borderwidth=0)
        self.style.configure("Treeview.Heading", font=self.heading_font, background=panel, foreground=text)
        self.style.map("Treeview", background=[("selected", select)], foreground=[("selected", text)])
        self.style.configure("Horizontal.TProgressbar", background=accent, troughcolor=panel)
        self.summary_text.configure(bg=panel, fg=text, insertbackground=text)
        self.treemap.configure(background=panel, highlightbackground=muted)

    def toggle_theme(self) -> None:
        self.theme_mode.set("light" if self.theme_mode.get() == "dark" else "dark")
        self._apply_theme()
        self.draw_treemap()

    # ---------- scan ----------

    def browse_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder to scan")
        if folder:
            self.current_folder.set(folder)

    def scan_path(self, path: str) -> None:
        self.current_folder.set(path)
        self.start_scan()

    def start_scan(self) -> None:
        folder = self.current_folder.get().strip().strip('"')
        if not folder:
            self.browse_folder()
            folder = self.current_folder.get().strip().strip('"')
        if not folder:
            return
        if not os.path.isdir(folder):
            messagebox.showerror("Invalid folder", "Choose a valid folder.")
            return
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showinfo("Scan already running", "Cancel the current scan first.")
            return

        self._clear_scan_data()
        self.cancel_event.clear()
        self.hash_cancel_event.set()
        self.scan_queue = queue.Queue()
        self.scan_start_time = time.time()
        size_hints = self.snapshot_db.size_hints(folder)
        hint_note = f" with {len(size_hints):,} cached size hints" if size_hints else ""
        self.status_text.set(f"scanning with adaptive SOTA engine{hint_note}...")
        self.progress.start(12)
        scanner = Scanner(folder, self.scan_queue, self.cancel_event, size_hints=size_hints)
        self.current_scan_backend = scanner.backend_name + (" + sqlite-size-hints" if size_hints else "")
        self.scan_thread = threading.Thread(target=scanner.scan, daemon=True)
        self.scan_thread.start()

    def cancel_scan(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            self.cancel_event.set()
            self.status_text.set("canceling scan...")

    def _clear_scan_data(self) -> None:
        self.root_node = None
        self.stats = None
        self.ext_sizes.clear()
        self.all_files = []
        self.largest_files = []
        self.node_by_iid.clear()
        self.iid_by_node_id.clear()
        self.loaded_iids.clear()
        self.cleanup_by_iid.clear()
        self.path_by_iid.clear()
        self.compare_path_by_iid.clear()
        self.map_focus_node = None
        self.map_history = []
        for tree in (self.tree, self.types_tree, self.large_tree, self.dup_tree, self.cleanup_tree, self.compare_tree):
            tree.delete(*tree.get_children())
        self.clear_summary()
        self.treemap.delete("all")

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.scan_queue.get_nowait()
                kind = msg[0]
                if kind == "scan_progress":
                    _, count, rate, current_path = msg
                    self.status_text.set(f"scanned {count:,} items  |  {rate:,.0f}/sec  |  {truncate_middle(current_path, 90)}")
                elif kind == "scan_done":
                    _, root, stats, ext_sizes, largest, all_files = msg
                    self.progress.stop()
                    if root is None:
                        self.status_text.set("scan canceled")
                    else:
                        self.root_node = root
                        self.stats = stats
                        self.ext_sizes = ext_sizes
                        self.largest_files = largest
                        self.all_files = all_files
                        self.populate_all(root, stats, ext_sizes, largest)
                elif kind == "dup_progress":
                    _, done, total, path = msg
                    self.status_text.set(f"hashing duplicate candidates {done:,}/{total:,}  |  {truncate_middle(path, 80)}")
                elif kind == "dup_done":
                    _, duplicate_groups, skipped = msg
                    self.progress.stop()
                    self.render_verified_duplicates(duplicate_groups, skipped)
                elif kind == "fatal":
                    self.progress.stop()
                    self.status_text.set("operation failed")
                    messagebox.showerror("Operation failed", msg[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def populate_all(self, root: Node, stats: ScanStats, ext_sizes: Dict[str, Tuple[int, int]], largest: List[Tuple[int, str, float]], save_cache: bool = True) -> None:
        self.populate_main_tree(root)
        self.populate_types(ext_sizes, root.size)
        self.populate_largest(largest)
        self.populate_duplicates()
        self.populate_cleanup()
        self.write_summary(root, stats)
        self.map_focus_node = root
        self.draw_treemap()
        elapsed = max(time.time() - stats.start_time, 0.01)
        cached_note = ""
        if save_cache and not self.cancel_event.is_set():
            try:
                snap_id = self.snapshot_db.save_current_scan(root, stats, elapsed, self.current_scan_backend)
                cached_note = f" | cached snapshot #{snap_id}"
                self.populate_cache_tab()
            except Exception as exc:
                cached_note = f" | cache save failed: {exc}"
        canceled_note = " canceled" if self.cancel_event.is_set() else " complete"
        self.status_text.set(f"scan{canceled_note}: {format_size(root.size)} across {root.files:,} files and {root.folders:,} folders in {elapsed:,.1f}s{cached_note}")

    # ---------- SOTA cache / notes ----------

    def show_sota_notes(self) -> None:
        messagebox.showinfo(
            "SpaceLens SOTA engine",
            "This build uses the realistic fast path from the research report:\n\n"
            "• adaptive os.scandir traversal to avoid redundant metadata calls\n"
            "• Windows large-fetch directory enumeration when available\n"
            "• hot-zone ordering for Downloads, Videos, AppData, cache folders, node_modules, and game folders\n"
            "• SQLite snapshot cache so repeat scans can prioritize folders that were huge last time\n"
            "• duplicate detection by size, partial SHA-256, then full SHA-256\n\n"
            + FastIndexBackend.explain()
        )

    def populate_cache_tab(self) -> None:
        if not hasattr(self, "cache_tree"):
            return
        self.cache_tree.delete(*self.cache_tree.get_children())
        self.cache_snapshot_by_iid.clear()
        root_filter = self.current_folder.get().strip().strip('"') or None
        rows = self.snapshot_db.list_snapshots(root_filter if root_filter and os.path.isdir(root_filter) else None, limit=80)
        for snap_id, created, root, total_size, files, folders, duration, backend in rows:
            iid = self.cache_tree.insert(
                "", tk.END,
                values=(created, format_size(total_size), f"{files:,}", f"{folders:,}", f"{duration:.2f}", backend, root)
            )
            self.cache_snapshot_by_iid[iid] = snap_id

    def load_latest_cache(self) -> None:
        folder = self.current_folder.get().strip().strip('"')
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("Choose folder", "Choose a folder first, then load its latest cache.")
            return
        snap_id = self.snapshot_db.latest_snapshot_id(folder)
        if not snap_id:
            messagebox.showinfo("No cache", "No cached snapshot exists for this folder yet. Run a scan first.")
            return
        try:
            root = self.snapshot_db.load_tree(snap_id)
            if not root:
                raise RuntimeError("cached tree was empty")
            self._clear_scan_data()
            self.root_node = root
            stats = ScanStats(scanned_items=root.files + root.folders + 1)
            stats.start_time = time.time()
            self.stats = stats
            self.all_files = [FileRecord(n.path, n.size, n.modified, n.created, os.path.splitext(n.name)[1].lower()) for n in self._iter_nodes(root) if not n.is_dir]
            self.largest_files = sorted([(r.size, r.path, r.modified) for r in self.all_files], reverse=True)[:MAX_LARGEST_FILES]
            ext_sizes: Dict[str, Tuple[int, int]] = {}
            for rec in self.all_files:
                size, count = ext_sizes.get(rec.ext or "[no extension]", (0, 0))
                ext_sizes[rec.ext or "[no extension]"] = (size + rec.size, count + 1)
            self.ext_sizes = ext_sizes
            self.populate_all(root, stats, ext_sizes, self.largest_files, save_cache=False)
            self.status_text.set(f"loaded cached snapshot #{snap_id} instantly. rescan for fresh duplicate and cleanup data.")
        except Exception as exc:
            messagebox.showerror("Cache load failed", str(exc))

    def compare_selected_cache_snapshot(self) -> None:
        if not self.root_node:
            messagebox.showinfo("Nothing to compare", "Scan a folder first, then compare a cached snapshot.")
            return
        iid = self.cache_tree.focus()
        snap_id = self.cache_snapshot_by_iid.get(iid)
        if not snap_id:
            messagebox.showinfo("Choose snapshot", "Select a cached snapshot first.")
            return
        self._render_cache_compare(snap_id)

    def compare_cached_snapshot(self) -> None:
        if not self.root_node:
            messagebox.showinfo("Nothing to compare", "Scan a folder first, then compare a cached snapshot.")
            return
        rows = self.snapshot_db.list_snapshots(self.root_node.path, limit=8)
        # Skip the newest snapshot if it is the one just saved for this scan.
        candidates = [r for r in rows if r[3] != self.root_node.size or abs(time.time() - _dt.datetime.fromisoformat(r[1]).timestamp()) > 5]
        if not candidates:
            messagebox.showinfo("No older cache", "No older cached snapshot exists for this folder yet. Scan it again later to compare growth.")
            return
        snap_id = candidates[0][0]
        self._render_cache_compare(snap_id)

    def _render_cache_compare(self, snap_id: int) -> None:
        if not self.root_node:
            return
        rows = self.snapshot_db.compare_to_snapshot(self.root_node, snap_id)
        self.compare_tree.delete(*self.compare_tree.get_children())
        self.compare_path_by_iid.clear()
        for change, old_size, new_size, kind, path in rows:
            sign = "+" if change > 0 else ""
            iid = self.compare_tree.insert("", tk.END, values=(sign + format_size(change), format_size(old_size), format_size(new_size), kind, path))
            self.compare_path_by_iid[iid] = path
        self.notebook.select(self.compare_tab)
        self.status_text.set(f"cache comparison loaded from snapshot #{snap_id}; showing {len(rows):,} largest changes")

    # ---------- main tree / lazy loading ----------

    def populate_main_tree(self, root: Node) -> None:
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.iid_by_node_id.clear()
        self.loaded_iids.clear()
        iid = self._insert_node("", root, root.size)
        self.tree.item(iid, open=True)
        self._load_children(iid)
        self.tree.selection_set(iid)
        self.tree.focus(iid)

    def _insert_node(self, parent_iid: str, node: Node, total: int) -> str:
        values = (
            format_size(node.size),
            safe_percent(node.size, total),
            f"{node.files:,}",
            f"{node.folders:,}" if node.is_dir else "",
            format_time(node.modified),
            node.path,
        )
        label = node.name + ("  ⚠" if node.error else "")
        iid = self.tree.insert(parent_iid, tk.END, text=label, values=values, open=False)
        self.node_by_iid[iid] = node
        self.iid_by_node_id[id(node)] = iid
        if node.is_dir and node.children:
            self.tree.insert(iid, tk.END, text="loading...", values=("", "", "", "", "", ""), tags=("dummy",))
        return iid

    def on_tree_open(self, _event=None) -> None:
        iid = self.tree.focus()
        if iid:
            self._load_children(iid)

    def _load_children(self, iid: str) -> None:
        if iid in self.loaded_iids:
            return
        node = self.node_by_iid.get(iid)
        if not node or not node.children:
            self.loaded_iids.add(iid)
            return
        for child in self.tree.get_children(iid):
            self.tree.delete(child)
        total = self.root_node.size if self.root_node else node.size
        shown = node.children[:MAX_CHILDREN_PER_FOLDER]
        for child_node in shown:
            self._insert_node(iid, child_node, total)
        hidden = len(node.children) - len(shown)
        if hidden > 0:
            self.tree.insert(iid, tk.END, text=f"[{hidden:,} more hidden here to keep the UI fast; use search/export]", values=("", "", "", "", "", node.path))
        self.loaded_iids.add(iid)

    def selected_node(self) -> Optional[Node]:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.node_by_iid.get(selection[0])

    def selected_iid(self) -> Optional[str]:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def on_tree_select(self, _event=None) -> None:
        node = self.selected_node()
        if node and node.is_dir:
            self.map_focus_node = node
        self.draw_treemap()

    def on_tree_double_click(self, _event=None) -> None:
        node = self.selected_node()
        if node:
            open_path(node.path)

    def show_context_menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def open_selected(self) -> None:
        node = self.selected_node()
        if node:
            open_path(node.path)

    def reveal_selected(self) -> None:
        node = self.selected_node()
        if node:
            reveal_path(node.path)

    def copy_selected_path(self) -> None:
        node = self.selected_node()
        if node:
            self.clipboard_clear()
            self.clipboard_append(node.path)
            self.status_text.set("copied path")

    def terminal_selected(self) -> None:
        node = self.selected_node()
        if node:
            open_terminal_here(node.path)

    def rescan_selected(self) -> None:
        node = self.selected_node()
        if not node:
            return
        path = node.path if node.is_dir else os.path.dirname(node.path)
        if path:
            self.current_folder.set(path)
            self.start_scan()

    def delete_selected(self) -> None:
        node = self.selected_node()
        if not node:
            return
        self.delete_path_with_prompt(node.path)

    def delete_path_with_prompt(self, path: str) -> None:
        answer = messagebox.askyesno(
            "Move to Recycle Bin",
            f"Move this item to the Recycle Bin?\n\n{path}\n\nRescan afterward to refresh exact sizes.",
        )
        if not answer:
            return
        try:
            if recycle_or_delete(path):
                self.status_text.set("deleted. rescan for exact updated sizes")
        except Exception as exc:
            messagebox.showerror("Delete failed", str(exc))

    def sort_visible_tree(self, key: str, reverse_default: bool) -> None:
        def node_value(iid: str):
            n = self.node_by_iid.get(iid)
            if not n:
                return 0
            if key == "name":
                return n.name.lower()
            if key in ("size", "percent"):
                return n.size
            if key == "files":
                return n.files
            if key == "folders":
                return n.folders
            if key == "modified":
                return n.modified
            return n.name.lower()

        def sort_children(parent: str) -> None:
            children = list(self.tree.get_children(parent))
            children.sort(key=node_value, reverse=reverse_default)
            for index, iid in enumerate(children):
                self.tree.move(iid, parent, index)
                sort_children(iid)

        sort_children("")

    # ---------- tabs ----------

    def populate_types(self, ext_sizes: Dict[str, Tuple[int, int]], total_size: int) -> None:
        self.types_tree.delete(*self.types_tree.get_children())
        rows = sorted(ext_sizes.items(), key=lambda item: item[1][0], reverse=True)
        for ext, (size, count) in rows[:MAX_TYPE_ROWS]:
            self.types_tree.insert("", tk.END, values=(ext, category_for_ext(ext), format_size(size), safe_percent(size, total_size), f"{count:,}"))

    def populate_largest(self, largest: List[Tuple[int, str, float]]) -> None:
        self.large_tree.delete(*self.large_tree.get_children())
        self.path_by_iid.clear()
        for size, path, modified in largest:
            iid = self.large_tree.insert("", tk.END, values=(format_size(size), f"{age_days(modified):,} days", format_time(modified), path))
            self.path_by_iid[iid] = path

    def populate_duplicates(self) -> None:
        self.dup_tree.delete(*self.dup_tree.get_children())
        self.path_by_iid = {iid: path for iid, path in self.path_by_iid.items() if iid in self.large_tree.get_children("")}
        if not self.all_files:
            return
        min_size = parse_size_to_bytes(self.dup_min_size_var.get(), self.dup_min_unit_var.get())
        by_size: Dict[int, List[FileRecord]] = defaultdict(list)
        for rec in self.all_files:
            if rec.size >= min_size:
                by_size[rec.size].append(rec)
        groups = [(size, files) for size, files in by_size.items() if len(files) > 1]
        groups.sort(key=lambda item: item[0] * len(item[1]), reverse=True)
        for idx, (size, files) in enumerate(groups[:MAX_DUPLICATE_GROUPS], start=1):
            parent = self.dup_tree.insert("", tk.END, text=f"same-size group {idx}", values=(format_size(size), f"{len(files):,}", "needs hash", ""), open=False)
            for rec in sorted(files, key=lambda r: r.path.lower())[:500]:
                iid = self.dup_tree.insert(parent, tk.END, text=os.path.basename(rec.path), values=(format_size(rec.size), "", "candidate", rec.path))
                self.path_by_iid[iid] = rec.path
        if groups:
            self.status_text.set(f"found {len(groups):,} same-size duplicate candidate groups. hash verify for true duplicates")

    def verify_duplicates_with_hash(self) -> None:
        if not self.all_files:
            messagebox.showinfo("No scan", "Scan a folder first.")
            return
        if self.current_duplicate_hash_thread and self.current_duplicate_hash_thread.is_alive():
            messagebox.showinfo("Hash already running", "Cancel the current duplicate hash check first.")
            return
        min_size = parse_size_to_bytes(self.dup_min_size_var.get(), self.dup_min_unit_var.get())
        self.hash_cancel_event.clear()
        self.progress.start(12)
        self.current_duplicate_hash_thread = threading.Thread(target=self._hash_duplicate_worker, args=(min_size,), daemon=True)
        self.current_duplicate_hash_thread.start()

    def cancel_hashing(self) -> None:
        self.hash_cancel_event.set()
        self.status_text.set("canceling hash check...")

    def _hash_duplicate_worker(self, min_size: int) -> None:
        try:
            by_size: Dict[int, List[FileRecord]] = defaultdict(list)
            for rec in self.all_files:
                if rec.size >= min_size:
                    by_size[rec.size].append(rec)
            size_groups = [(size, files) for size, files in by_size.items() if len(files) > 1]

            # Stage 1: partial SHA-256 prefilter. This is the SOTA report's cheap prune step.
            total_files = sum(len(files) for _, files in size_groups)
            done = 0
            skipped = 0
            partial_groups: List[Tuple[int, List[FileRecord]]] = []
            for size, files in size_groups:
                if self.hash_cancel_event.is_set():
                    break
                by_partial: Dict[str, List[FileRecord]] = defaultdict(list)
                for rec in files:
                    if self.hash_cancel_event.is_set():
                        break
                    digest = partial_hash_file(rec.path, self.hash_cancel_event)
                    done += 1
                    self.scan_queue.put(("dup_progress", done, total_files, "partial " + rec.path))
                    if digest:
                        by_partial[digest].append(rec)
                    else:
                        skipped += 1
                for _partial, recs in by_partial.items():
                    if len(recs) > 1:
                        partial_groups.append((size, recs))

            # Stage 2: full SHA-256 only for the survivors.
            full_total = sum(len(files) for _, files in partial_groups)
            done = 0
            verified: List[Tuple[int, str, List[FileRecord]]] = []
            for size, files in partial_groups:
                if self.hash_cancel_event.is_set():
                    break
                by_hash: Dict[str, List[FileRecord]] = defaultdict(list)
                for rec in files:
                    if self.hash_cancel_event.is_set():
                        break
                    digest = hash_file(rec.path, self.hash_cancel_event)
                    done += 1
                    self.scan_queue.put(("dup_progress", done, full_total, "full " + rec.path))
                    if digest:
                        by_hash[digest].append(rec)
                    else:
                        skipped += 1
                for digest, recs in by_hash.items():
                    if len(recs) > 1:
                        verified.append((size, digest, recs))
            verified.sort(key=lambda item: item[0] * len(item[2]), reverse=True)
            self.scan_queue.put(("dup_done", verified[:MAX_DUPLICATE_GROUPS], skipped))
        except Exception as exc:
            self.scan_queue.put(("fatal", str(exc)))

    def render_verified_duplicates(self, duplicate_groups: List[Tuple[int, str, List[FileRecord]]], skipped: int) -> None:
        self.dup_tree.delete(*self.dup_tree.get_children())
        for idx, (size, digest, files) in enumerate(duplicate_groups, start=1):
            wasted = size * (len(files) - 1)
            parent = self.dup_tree.insert("", tk.END, text=f"true duplicate group {idx}", values=(format_size(wasted), f"{len(files):,}", "sha-256 match", digest[:16] + "..."), open=False)
            for rec in sorted(files, key=lambda r: r.path.lower()):
                iid = self.dup_tree.insert(parent, tk.END, text=os.path.basename(rec.path), values=(format_size(rec.size), "", "duplicate", rec.path))
                self.path_by_iid[iid] = rec.path
        if self.hash_cancel_event.is_set():
            self.status_text.set("hash check canceled")
        else:
            self.status_text.set(f"verified {len(duplicate_groups):,} duplicate groups. skipped {skipped:,} unreadable files")

    def populate_cleanup(self) -> None:
        self.cleanup_tree.delete(*self.cleanup_tree.get_children())
        self.cleanup_by_iid.clear()
        if not self.root_node:
            return
        suggestions = unique_cleanup_suggestions(self._build_cleanup_suggestions())
        for sug in suggestions:
            iid = self.cleanup_tree.insert("", tk.END, values=(sug.reason, format_size(sug.size), f"{age_days(sug.modified):,} days", format_time(sug.modified), sug.path))
            self.cleanup_by_iid[iid] = sug
            self.path_by_iid[iid] = sug.path

    def _build_cleanup_suggestions(self) -> List[CleanupSuggestion]:
        suggestions: List[CleanupSuggestion] = []
        now = time.time()
        downloads_marker = os.sep + "downloads" + os.sep
        for rec in self.all_files:
            path_lower = rec.path.lower()
            ext = rec.ext
            age = (now - rec.modified) / 86400 if rec.modified else 0
            if rec.size >= 100 * 1024**2 and age >= 180:
                suggestions.append(CleanupSuggestion("old huge file", rec.size, rec.modified, rec.path, "file"))
            if ext in ARCHIVE_EXTS and rec.size >= 50 * 1024**2:
                suggestions.append(CleanupSuggestion("large archive", rec.size, rec.modified, rec.path, "file"))
            if ext in INSTALLER_EXTS and (downloads_marker in path_lower or path_lower.endswith(os.sep + "downloads")):
                suggestions.append(CleanupSuggestion("downloaded installer", rec.size, rec.modified, rec.path, "file"))
            elif ext in INSTALLER_EXTS and rec.size >= 100 * 1024**2:
                suggestions.append(CleanupSuggestion("large installer/image", rec.size, rec.modified, rec.path, "file"))
            if ext in VIDEO_EXTS and rec.size >= 500 * 1024**2:
                suggestions.append(CleanupSuggestion("large video", rec.size, rec.modified, rec.path, "file"))

        for node in self._iter_nodes(self.root_node):
            if not node.is_dir or node is self.root_node:
                continue
            lname = node.name.lower()
            if lname in CACHE_NAMES and node.size >= 100 * 1024**2:
                suggestions.append(CleanupSuggestion("large cache/temp folder", node.size, node.modified, node.path, "folder"))
            elif lname in DEV_HEAVY_NAMES and node.size >= 250 * 1024**2:
                suggestions.append(CleanupSuggestion("large dev/build folder", node.size, node.modified, node.path, "folder"))
        return suggestions

    def write_summary(self, root: Node, stats: ScanStats) -> None:
        elapsed = max(time.time() - stats.start_time, 0.01)
        largest_child = max(root.children, key=lambda n: n.size, default=None)
        duplicate_candidates = self._count_duplicate_candidate_groups()
        cleanup_total = (
            sum(s.size for s in unique_cleanup_suggestions(self._build_cleanup_suggestions()))
            if self.all_files
            else 0
        )
        lines = [
            f"{APP_NAME} {APP_VERSION} scan summary",
            "",
            f"Scanned folder: {root.path}",
            f"Total size: {format_size(root.size)}",
            f"Files: {root.files:,}",
            f"Folders: {root.folders:,}",
            f"Items scanned: {stats.scanned_items:,}",
            f"Elapsed time: {elapsed:,.2f} seconds",
            f"Average speed: {stats.scanned_items / elapsed:,.0f} items/sec",
            "",
            f"Duplicate candidate groups: {duplicate_candidates:,}",
            f"Potential cleanup suggestions total: {format_size(cleanup_total)}",
            "",
            f"Permission errors: {stats.permission_errors:,}",
            f"Other errors: {stats.other_errors:,}",
            f"Skipped symbolic links: {stats.skipped_links:,}",
            f"Skipped repeated/reparse folders: {stats.skipped_reparse_or_seen:,}",
        ]
        if largest_child:
            lines.extend([
                "",
                f"Largest direct child: {largest_child.name}",
                f"Largest direct child size: {format_size(largest_child.size)} ({safe_percent(largest_child.size, root.size)})",
            ])
        self.summary_text.configure(state=tk.NORMAL)
        self.summary_text.delete("1.0", tk.END)
        self.summary_text.insert("1.0", "\n".join(lines))
        self.summary_text.configure(state=tk.DISABLED)

    def _count_duplicate_candidate_groups(self) -> int:
        min_size = parse_size_to_bytes(self.dup_min_size_var.get(), self.dup_min_unit_var.get())
        by_size: Dict[int, int] = defaultdict(int)
        for rec in self.all_files:
            if rec.size >= min_size:
                by_size[rec.size] += 1
        return sum(1 for count in by_size.values() if count > 1)

    def clear_summary(self) -> None:
        self.summary_text.configure(state=tk.NORMAL)
        self.summary_text.delete("1.0", tk.END)
        self.summary_text.configure(state=tk.DISABLED)

    # ---------- treemap ----------

    def draw_treemap(self) -> None:
        if not hasattr(self, "treemap"):
            return
        self.treemap.delete("all")
        self.treemap_rects.clear()
        node = self.map_focus_node or self.selected_node() or self.root_node
        if not node:
            self._canvas_text("scan a folder to see a treemap")
            return
        if not node.is_dir:
            node = node.parent or self.root_node
        self.map_focus_node = node
        self.map_label.configure(text=truncate_middle(node.path, 85))
        children = [c for c in node.children if c.size > 0]
        if not children:
            self._canvas_text("no sizable children to draw")
            return
        children = sorted(children, key=lambda n: n.size, reverse=True)[:90]
        width = max(self.treemap.winfo_width(), 250)
        height = max(self.treemap.winfo_height(), 180)
        rects = self._binary_treemap(children, 8, 8, width - 16, height - 16)
        for idx, (x, y, w, h, child) in enumerate(rects):
            self._draw_rect(x, y, w, h, child, idx)

    def _canvas_text(self, text: str) -> None:
        self.treemap.create_text(20, 20, text=text, anchor="nw", fill=self.colors["text"], font=self.default_font)

    def _binary_treemap(self, nodes: List[Node], x: int, y: int, w: int, h: int) -> List[Tuple[int, int, int, int, Node]]:
        if not nodes or w <= 2 or h <= 2:
            return []
        if len(nodes) == 1:
            return [(x, y, w, h, nodes[0])]
        total = sum(n.size for n in nodes)
        half = total / 2
        acc = 0
        split = 1
        for i, n in enumerate(nodes):
            if i > 0 and acc + n.size > half:
                break
            acc += n.size
            split = i + 1
        group1 = nodes[:split]
        group2 = nodes[split:]
        if not group2:
            return [(x, y, w, h, nodes[0])]
        total1 = sum(n.size for n in group1)
        if w >= h:
            w1 = max(2, int(w * (total1 / total)))
            return self._binary_treemap(group1, x, y, w1, h) + self._binary_treemap(group2, x + w1, y, w - w1, h)
        h1 = max(2, int(h * (total1 / total)))
        return self._binary_treemap(group1, x, y, w, h1) + self._binary_treemap(group2, x, y + h1, w, h - h1)

    def _draw_rect(self, x: int, y: int, w: int, h: int, node: Node, idx: int) -> None:
        palette = {
            "folder": ("#3b5f8a", "#6aa5ff"),
            "video": ("#714b8f", "#c18cff"),
            "audio": ("#7a5a2a", "#e9bd6a"),
            "image": ("#2d725f", "#6ed6b6"),
            "archive": ("#7a573b", "#e0a16f"),
            "installer": ("#7c3d3d", "#ef7777"),
            "document": ("#59636f", "#b5c0ce"),
            "other": ("#3d4654", "#8fa2bd"),
        }
        cat = "folder" if node.is_dir else category_for_ext(Path(node.path).suffix.lower() or "[no extension]")
        fill, outline = palette.get(cat, palette["other"])
        rect = self.treemap.create_rectangle(x, y, x + w, y + h, fill=fill, outline=outline, width=1)
        self.treemap_rects.append((x, y, x + w, y + h, node))
        if w > 70 and h > 34:
            label = f"{node.name}\n{format_size(node.size)}"
            self.treemap.create_text(x + 5, y + 5, text=label, anchor="nw", width=max(20, w - 10), font=("Segoe UI", 9), fill="#ffffff")
        self.treemap.tag_bind(rect, "<Enter>", lambda _e, n=node: self.status_text.set(f"{n.path}  |  {format_size(n.size)}"))

    def on_treemap_click(self, event) -> None:
        for x1, y1, x2, y2, node in reversed(self.treemap_rects):
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self._select_node_in_tree(node)
                if node.is_dir:
                    if self.map_focus_node is not node:
                        if self.map_focus_node:
                            self.map_history.append(self.map_focus_node)
                        self.map_focus_node = node
                    self.draw_treemap()
                return

    def on_treemap_double_click(self, event) -> None:
        for x1, y1, x2, y2, node in reversed(self.treemap_rects):
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                open_path(node.path)
                return

    def map_back(self) -> None:
        if self.map_history:
            self.map_focus_node = self.map_history.pop()
        elif self.map_focus_node and self.map_focus_node.parent:
            self.map_focus_node = self.map_focus_node.parent
        self.draw_treemap()

    def map_root(self) -> None:
        if self.root_node:
            self.map_focus_node = self.root_node
            self.map_history = []
            self._select_node_in_tree(self.root_node)
            self.draw_treemap()

    def _select_node_in_tree(self, target: Node) -> None:
        if not self.root_node:
            return
        chain: List[Node] = []
        n: Optional[Node] = target
        while n is not None:
            chain.append(n)
            n = n.parent
        chain.reverse()
        for index, node in enumerate(chain):
            iid = self.iid_by_node_id.get(id(node))
            if iid is None and index > 0:
                parent = chain[index - 1]
                parent_iid = self.iid_by_node_id.get(id(parent))
                if parent_iid:
                    self._load_children(parent_iid)
                    iid = self.iid_by_node_id.get(id(node))
            if iid:
                self._load_children(iid)
                self.tree.item(iid, open=True)
        target_iid = self.iid_by_node_id.get(id(target))
        if target_iid:
            self.tree.selection_set(target_iid)
            self.tree.focus(target_iid)
            self.tree.see(target_iid)

    # ---------- search/filter ----------

    def apply_search(self) -> None:
        if not self.root_node:
            return
        term = self.search_var.get()
        if not term:
            return
        case_sensitive = self.case_var.get()
        include_files = self.include_files_var.get()
        needle = term if case_sensitive else term.lower()
        matches: List[Node] = []

        for n in self._iter_nodes(self.root_node):
            if len(matches) >= MAX_SEARCH_RESULTS:
                break
            hay = n.path if case_sensitive else n.path.lower()
            if needle in hay and (include_files or n.is_dir):
                matches.append(n)
        if not matches:
            messagebox.showinfo("No matches", "No matching paths found.")
            return
        self._show_flat_nodes(matches, f"showing {len(matches):,} search results for '{term}'. clear search to restore tree")

    def apply_size_filter(self) -> None:
        if not self.root_node:
            return
        threshold = parse_size_to_bytes(self.min_size_var.get(), self.min_size_unit_var.get())
        matches = [n for n in self._iter_nodes(self.root_node) if n.size >= threshold and (self.include_files_var.get() or n.is_dir)]
        matches.sort(key=lambda n: n.size, reverse=True)
        self._show_flat_nodes(matches[:MAX_SEARCH_RESULTS], f"showing items over {format_size(threshold)}. clear search to restore tree")

    def _show_flat_nodes(self, nodes: List[Node], status: str) -> None:
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.iid_by_node_id.clear()
        self.loaded_iids.clear()
        total = self.root_node.size if self.root_node else 0
        for n in sorted(nodes, key=lambda x: x.size, reverse=True):
            values = (
                format_size(n.size),
                safe_percent(n.size, total),
                f"{n.files:,}",
                f"{n.folders:,}" if n.is_dir else "",
                format_time(n.modified),
                n.path,
            )
            iid = self.tree.insert("", tk.END, text=n.name, values=values)
            self.node_by_iid[iid] = n
            self.iid_by_node_id[id(n)] = iid
        self.status_text.set(status)

    def clear_search(self) -> None:
        self.search_var.set("")
        if self.root_node:
            self.populate_main_tree(self.root_node)
            self.status_text.set("restored folder tree")

    # ---------- snapshot / compare / exports ----------

    def save_snapshot(self) -> None:
        if not self.root_node:
            messagebox.showinfo("Nothing to save", "Scan a folder first.")
            return
        default_name = f"spacelens_snapshot_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json.gz"
        path = filedialog.asksaveasfilename(
            title="Save SpaceLens snapshot",
            defaultextension=".json.gz",
            initialfile=default_name,
            filetypes=[("Compressed JSON", "*.json.gz"), ("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            data = {
                "app": APP_NAME,
                "version": APP_VERSION,
                "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "root": self.root_node.path,
                "items": [self._node_to_snapshot_row(n) for n in self._iter_nodes(self.root_node)],
            }
            if path.lower().endswith(".gz"):
                with gzip.open(path, "wt", encoding="utf-8") as f:
                    json.dump(data, f)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            self.status_text.set(f"saved snapshot: {path}")
        except Exception as exc:
            messagebox.showerror("Snapshot failed", str(exc))

    def _node_to_snapshot_row(self, n: Node) -> dict:
        return {
            "path": n.path,
            "name": n.name,
            "type": "folder" if n.is_dir else "file",
            "size": n.size,
            "files": n.files,
            "folders": n.folders,
            "modified": n.modified,
        }

    def compare_snapshot(self) -> None:
        if not self.root_node:
            messagebox.showinfo("Nothing to compare", "Scan a folder first, then compare an older snapshot.")
            return
        path = filedialog.askopenfilename(
            title="Open older SpaceLens snapshot",
            filetypes=[("Snapshots", "*.json.gz *.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            if path.lower().endswith(".gz"):
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    old = json.load(f)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f)
            old_map = {item["path"]: item for item in old.get("items", []) if "path" in item}
            new_map = {n.path: self._node_to_snapshot_row(n) for n in self._iter_nodes(self.root_node)}
            rows = []
            for p, new_item in new_map.items():
                old_item = old_map.get(p)
                old_size = int(old_item.get("size", 0)) if old_item else 0
                new_size = int(new_item.get("size", 0))
                change = new_size - old_size
                if change != 0:
                    rows.append((change, old_size, new_size, new_item.get("type", ""), p))
            for p, old_item in old_map.items():
                if p not in new_map:
                    old_size = int(old_item.get("size", 0))
                    rows.append((-old_size, old_size, 0, old_item.get("type", ""), p))
            rows.sort(key=lambda r: abs(r[0]), reverse=True)
            self.compare_tree.delete(*self.compare_tree.get_children())
            self.compare_path_by_iid.clear()
            for change, old_size, new_size, kind, p in rows[:MAX_COMPARE_ROWS]:
                sign = "+" if change > 0 else ""
                iid = self.compare_tree.insert("", tk.END, values=(sign + format_size(change), format_size(old_size), format_size(new_size), kind, p))
                self.compare_path_by_iid[iid] = p
            self.notebook.select(self.compare_tab)
            self.status_text.set(f"compared snapshot. showing {min(len(rows), MAX_COMPARE_ROWS):,} largest changes")
        except Exception as exc:
            messagebox.showerror("Compare failed", str(exc))

    def show_export_menu(self) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Export CSV", command=self.export_csv)
        menu.add_command(label="Export JSON", command=self.export_json)
        menu.add_command(label="Export HTML report", command=self.export_html)
        try:
            x = self.winfo_pointerx()
            y = self.winfo_pointery()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def export_csv(self) -> None:
        if not self.root_node:
            messagebox.showinfo("Nothing to export", "Scan a folder first.")
            return
        default_name = f"spacelens_scan_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path = filedialog.asksaveasfilename(title="Export scan as CSV", defaultextension=".csv", initialfile=default_name, filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["path", "name", "type", "size_bytes", "size", "files", "folders", "modified", "error"])
                for n in self._iter_nodes(self.root_node):
                    writer.writerow([n.path, n.name, "folder" if n.is_dir else "file", n.size, format_size(n.size), n.files, n.folders, format_time(n.modified), n.error or ""])
            self.status_text.set(f"exported csv: {path}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def export_json(self) -> None:
        if not self.root_node:
            messagebox.showinfo("Nothing to export", "Scan a folder first.")
            return
        default_name = f"spacelens_scan_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path = filedialog.asksaveasfilename(title="Export scan as JSON", defaultextension=".json", initialfile=default_name, filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = {"app": APP_NAME, "version": APP_VERSION, "root": self.root_node.path, "items": [self._node_to_snapshot_row(n) for n in self._iter_nodes(self.root_node)]}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.status_text.set(f"exported json: {path}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def export_html(self) -> None:
        if not self.root_node:
            messagebox.showinfo("Nothing to export", "Scan a folder first.")
            return
        default_name = f"spacelens_report_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        path = filedialog.asksaveasfilename(title="Export HTML report", defaultextension=".html", initialfile=default_name, filetypes=[("HTML", "*.html"), ("All files", "*.*")])
        if not path:
            return
        try:
            top_files = "\n".join(f"<tr><td>{html.escape(format_size(s))}</td><td>{html.escape(format_time(m))}</td><td>{html.escape(p)}</td></tr>" for s, p, m in self.largest_files[:150])
            top_types = "\n".join(
                f"<tr><td>{html.escape(ext)}</td><td>{html.escape(category_for_ext(ext))}</td><td>{html.escape(format_size(size))}</td><td>{count:,}</td></tr>"
                for ext, (size, count) in sorted(self.ext_sizes.items(), key=lambda item: item[1][0], reverse=True)[:100]
            )
            content = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>SpaceLens Report</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:30px;background:#11151b;color:#eef2f8}}table{{border-collapse:collapse;width:100%;margin:16px 0}}td,th{{border-bottom:1px solid #333b47;padding:8px;text-align:left}}h1,h2{{color:#8dbbff}}code{{background:#202632;padding:2px 5px;border-radius:4px}}</style></head>
<body><h1>SpaceLens Report</h1>
<p><b>Root:</b> <code>{html.escape(self.root_node.path)}</code></p>
<p><b>Total:</b> {html.escape(format_size(self.root_node.size))} | <b>Files:</b> {self.root_node.files:,} | <b>Folders:</b> {self.root_node.folders:,}</p>
<h2>Largest files</h2><table><tr><th>Size</th><th>Modified</th><th>Path</th></tr>{top_files}</table>
<h2>File types</h2><table><tr><th>Extension</th><th>Category</th><th>Size</th><th>Count</th></tr>{top_types}</table>
</body></html>"""
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self.status_text.set(f"exported html: {path}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    # ---------- path tree shared menu ----------

    def show_path_tree_menu(self, event, tree: ttk.Treeview) -> None:
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        tree.focus(iid)
        path = self._path_from_any_tree(tree, iid)
        if path:
            self._context_path = path
            self.path_menu.tk_popup(event.x_root, event.y_root)

    def _path_from_any_tree(self, tree: ttk.Treeview, iid: str) -> str:
        values = tree.item(iid, "values")
        if not values:
            return ""
        # most tabular trees keep path in the final column
        maybe = str(values[-1])
        if maybe and (os.path.exists(maybe) or os.path.isabs(maybe)):
            return maybe
        return self.path_by_iid.get(iid) or self.compare_path_by_iid.get(iid) or ""

    def open_path_from_tree(self, tree: ttk.Treeview) -> None:
        selection = tree.selection()
        if not selection:
            return
        path = self._path_from_any_tree(tree, selection[0])
        if path:
            open_path(path)

    def open_context_path(self) -> None:
        if self._context_path:
            open_path(self._context_path)

    def reveal_context_path(self) -> None:
        if self._context_path:
            reveal_path(self._context_path)

    def copy_context_path(self) -> None:
        if self._context_path:
            self.clipboard_clear()
            self.clipboard_append(self._context_path)
            self.status_text.set("copied path")

    def terminal_context_path(self) -> None:
        if self._context_path:
            open_terminal_here(self._context_path)

    def delete_context_path(self) -> None:
        if self._context_path:
            self.delete_path_with_prompt(self._context_path)

    # ---------- iter ----------

    def _iter_nodes(self, node: Optional[Node]) -> Iterable[Node]:
        if node is None:
            return
        yield node
        for child in node.children:
            yield from self._iter_nodes(child)


def main() -> None:
    app = SpaceLensApp()
    if len(sys.argv) > 1:
        initial = sys.argv[1].strip('"')
        if os.path.isdir(initial):
            app.current_folder.set(initial)
    app.mainloop()


if __name__ == "__main__":
    main()
