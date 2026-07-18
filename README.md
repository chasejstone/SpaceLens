# SpaceLens

SpaceLens is a cross-platform disk usage viewer written in Python. It shows which folders use the most space, finds duplicate files, and compares scans over time. The main app uses only the Python standard library.

## Features

- Scan folders with `os.scandir` and use `FindFirstFileExW` on Windows when available.
- Show large folders early while the rest of the scan continues.
- Cache snapshots in `~/.spacelens_sota/snapshots.sqlite3` for repeat scans and size comparisons.
- Find duplicates by checking file size, a partial SHA-256 hash, and then the full hash.
- Browse a treemap, largest-file list, extension totals, and cleanup suggestions.
- Export results as CSV, JSON, or HTML.
- Open files, reveal them in the file manager, copy paths, or move files to the recycle bin.

The design notes and benchmark results are in [`docs/Research_Report.md`](docs/Research_Report.md).

## Requirements

- Python 3.10 or newer
- No third-party Python packages

## Run

On Windows, double-click `run_spacelens_sota.bat`.

On macOS or Linux:

```bash
python3 SpaceLens_SOTA.py
```

## Benchmarks

Run the benchmark against a folder:

```bash
python benchmark_spacelens_sota.py /path/to/folder
```

Or create a test tree first:

```bash
python benchmark_spacelens_sota.py --make-demo
```

The benchmark compares `os.walk`, `os.scandir`, and the scanner used by SpaceLens.

## Windows executable

Run `build_exe_optional.bat` to build an executable with PyInstaller. The output is written to `dist/`. PyInstaller is only needed for this build step.

## Files

```text
.
├── SpaceLens_SOTA.py
├── benchmark_spacelens_sota.py
├── run_spacelens_sota.bat
├── build_exe_optional.bat
├── requirements-build.txt
└── docs/
    ├── Research_Report.md
    ├── RELEASE_NOTES_v3.0.txt
    └── assets/
```

## Scan notes

The cache helps order repeat scans but does not replace a new scan. Permission errors and antivirus checks can slow file enumeration. Scanning a smaller folder or lowering other disk activity may help.

SpaceLens does not read the NTFS Master File Table or USN Change Journal. Its default scanner works without administrator access and is portable across operating systems.

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
