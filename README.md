# SpaceLens SOTA

A free, TreeSize-inspired disk usage explorer built on research-backed scanning techniques. Pure Python, zero third-party dependencies, cross-platform.

## Features

- **Adaptive scanner** built on `os.scandir`, with a Windows `FindFirstFileExW` large-fetch path when available
- **Hot-zone scan ordering** so the UI shows meaningful results quickly
- **Snapshot cache** at `~/.spacelens_sota/snapshots.sqlite3` for fast repeat scans and growth/shrink comparison
- **Duplicate finder** using size grouping → partial SHA-256 → full SHA-256
- **Treemap**, largest-files view, extension breakdown, cleanup suggestions
- **CSV / JSON / HTML exports**
- Dark/light mode, lazy-loaded tree, background scanning
- Safe open / reveal / copy path / recycle-bin actions
- Benchmark script included

See [`docs/Research_Report.md`](docs/Research_Report.md) for the design rationale and performance experiments.

## Requirements

- Python 3.10 or newer
- No third-party packages required (uses only the standard library)

## Quick Start

### Windows
Double-click `run_spacelens_sota.bat`.

### macOS / Linux
```bash
python3 SpaceLens_SOTA.py
```

## Benchmarking

Run against a real folder:
```bash
python benchmark_spacelens_sota.py /path/to/folder
```

Or generate a synthetic test tree:
```bash
python benchmark_spacelens_sota.py --make-demo
```

The benchmark compares `os.walk`, raw `os.scandir`, and the SpaceLens adaptive scanner.

## Building a Standalone Executable (optional)

On Windows, run `build_exe_optional.bat` to bundle the app via PyInstaller. The resulting `.exe` will appear under `dist/`. PyInstaller is not required to run the app normally.

## Repository Layout

```
.
├── SpaceLens_SOTA.py            # Main application (tkinter)
├── benchmark_spacelens_sota.py  # Benchmark harness
├── run_spacelens_sota.bat       # Windows launcher
├── build_exe_optional.bat       # Optional PyInstaller bundler
└── docs/
    ├── Research_Report.md       # Design rationale and experiments
    ├── RELEASE_NOTES_v3.0.txt   # v3.0 release notes
    └── assets/                  # Benchmark charts
```

## Tips for Best Speed

- Run a scan once, then scan the same root again — the cache provides size hints for ordering
- Avoid scanning `C:\Windows` unless needed; permission errors slow scans
- Antivirus can slow file enumeration; this app does not bypass security scanning
- On SSDs, smarter ordering and caching beat parallel traversal

## Honesty Note

This build does **not** perform raw NTFS MFT scanning. True WizTree / Everything-level speed requires privileged NTFS Master File Table and USN Change Journal indexing — that is the future fast-index backend. The default engine is portable, safe, and dependency-free.

## License

MIT — see [LICENSE](LICENSE).
