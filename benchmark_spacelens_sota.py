"""
SpaceLens SOTA benchmark helper.

Usage:
    python benchmark_spacelens_sota.py C:\\Users\\You\\Downloads
    python benchmark_spacelens_sota.py --make-demo

It compares:
- os.walk baseline
- raw os.scandir recursion
- SpaceLens adaptive scanner with hot-zone + cache-hint support
"""
from __future__ import annotations

import argparse
import os
import queue
import shutil
import tempfile
import time
from pathlib import Path

from SpaceLens_SOTA import Scanner, SnapshotDB


def walk_scan(path: str) -> tuple[int, int]:
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
                count += 1
            except OSError:
                pass
    return total, count


def scandir_scan(path: str) -> tuple[int, int]:
    total = 0
    count = 0

    def rec(folder: str) -> None:
        nonlocal total, count
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            rec(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        pass
        except OSError:
            pass

    rec(path)
    return total, count


def adaptive_scan(path: str, use_cache_hints: bool = True) -> tuple[int, int]:
    q: queue.Queue = queue.Queue()
    import threading
    cancel = threading.Event()
    hints = SnapshotDB().size_hints(path) if use_cache_hints else {}
    scanner = Scanner(path, q, cancel, size_hints=hints)
    scanner.scan()
    while True:
        msg = q.get()
        if msg[0] == "scan_done":
            root = msg[1]
            return (root.size if root else 0), (root.files if root else 0)
        if msg[0] == "fatal":
            raise RuntimeError(msg[1])


def make_demo_tree() -> str:
    base = tempfile.mkdtemp(prefix="spacelens_sota_demo_")
    root = Path(base) / "demo"
    for i in range(24):
        folder = root / ("Downloads" if i == 0 else f"folder_{i:02d}")
        folder.mkdir(parents=True, exist_ok=True)
        for j in range(650):
            with open(folder / f"file_{j:04d}.bin", "wb") as f:
                f.write(os.urandom(512))
    # a couple of duplicate groups
    blob = os.urandom(1024 * 128)
    dup_dir = root / "Videos" / "duplicates"
    dup_dir.mkdir(parents=True, exist_ok=True)
    for k in range(8):
        with open(dup_dir / f"clip_copy_{k}.mp4", "wb") as f:
            f.write(blob)
    return str(root)


def run_one(name: str, fn, path: str) -> dict:
    t0 = time.perf_counter()
    size, files = fn(path)
    elapsed = time.perf_counter() - t0
    return {"method": name, "seconds": elapsed, "size": size, "files": files, "files_per_sec": files / elapsed if elapsed else 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", help="folder to benchmark")
    parser.add_argument("--make-demo", action="store_true", help="create and benchmark a temporary synthetic tree")
    args = parser.parse_args()

    cleanup = None
    if args.make_demo:
        path = make_demo_tree()
        cleanup = Path(path).parent
    else:
        path = args.path or os.getcwd()

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise SystemExit(f"not a folder: {path}")

    print(f"Benchmark path: {path}")
    print("This measures metadata traversal, not UI rendering. Run twice to see OS cache effects.\n")

    results = [
        run_one("os.walk", walk_scan, path),
        run_one("os.scandir", scandir_scan, path),
        run_one("SpaceLens adaptive", adaptive_scan, path),
    ]
    baseline = results[0]["seconds"]
    for r in results:
        speedup = baseline / r["seconds"] if r["seconds"] else 0
        print(f"{r['method']:<22} {r['seconds']:>8.4f}s  files={r['files']:<8,} size={r['size']:,}  speedup={speedup:>5.2f}x")

    if cleanup:
        shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
    main()
