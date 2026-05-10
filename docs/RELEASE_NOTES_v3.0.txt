SpaceLens SOTA v3.0
====================

A free TreeSize-inspired disk usage app built from the research-backed design in the SOTA report.

How to run
----------
1. Install Python 3.10 or newer from python.org.
2. Extract this zip.
3. Double click run_spacelens_sota.bat.

No third-party Python packages are required.

What is new in v3.0
-------------------
- Adaptive os.scandir scanner.
- Windows FindFirstFileExW large-fetch path when available.
- Hot-zone scan ordering for folders likely to contain large files.
- SQLite snapshot cache at ~/.spacelens_sota/snapshots.sqlite3.
- Cached size hints. Repeat scans prioritize folders that were large last time.
- Automatic cached snapshots after each completed scan.
- Cache comparison tab for growth/shrink analysis.
- Duplicate finder uses size grouping, partial SHA-256, then full SHA-256.
- Treemap, largest-files view, extension breakdown, cleanup suggestions.
- CSV, JSON, and HTML exports.
- Benchmark script included.

Important honesty note
----------------------
This version does not fake raw NTFS MFT scanning.
True WizTree/Everything-level speed requires privileged NTFS Master File Table and USN Change Journal indexing.
That is the future fast-index backend, but this v3.0 build keeps the default engine portable, safe, and dependency-free.

Benchmarking
------------
Run:
    python benchmark_spacelens_sota.py C:\Users\YourName\Downloads

or create a temporary synthetic test:
    python benchmark_spacelens_sota.py --make-demo

Tips for best speed
-------------------
- Run a scan once, then scan the same root again. The cache gives the scanner size hints.
- Avoid scanning C:\Windows unless you need to. Permission errors slow scans.
- Antivirus can slow file enumeration. This app does not bypass security scanning.
- SSDs usually make parallel traversal less useful than smarter ordering and caching.

