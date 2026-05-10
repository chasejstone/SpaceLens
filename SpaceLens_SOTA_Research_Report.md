
# State-of-the-Art Disk Usage Scanning: Research and Experiments

## Introduction

To push SpaceLens toward cutting-edge performance, I examined state-of-the-art (SOTA) research on file‑system traversal, disk scanning, and duplicate detection.  I also conducted my own experiments to compare different scanning strategies.  This report summarises the findings and proposes improvements for making SpaceLens both faster and more feature‑rich.

## Research Findings

### Faster directory traversal with `os.scandir`

The Python community introduced `os.scandir()` to speed up directory iteration.  PEP 471 notes that `os.scandir()` avoids redundant `stat()` calls and can make `os.walk()` **2–20 times** faster【581771386933928†L65-L91】.  By reducing system calls from roughly `2 N` to `N`, scanning large trees becomes noticeably faster【581771386933928†L65-L91】.  This cross‑platform improvement is easy to adopt because it is part of the standard library.

### User‑space I/O scheduling

A 2009 IEEE paper by Lunde et al. explored how file‑system utilities that read many files sequentially suffer from excessive disk seeks.  The authors proposed reading metadata first to determine each file’s physical location on disk, then scheduling I/O to minimise head movement【60756556511615†L99-L109】.  In experiments they modified the `tar` utility and reduced the time to archive the Linux source tree from **82.5 s** to **17.9 s**—a **78 %** improvement【60756556511615†L99-L109】.  This demonstrates that knowing where data blocks reside can enable a “one‑sweep” traversal.

### Lessons from parallel scanning

A 2023 student report on parallel file‑system statistics investigated whether spawning worker threads for each subdirectory would speed up traversal.  The first attempt achieved no speed‑up, and even a refined version slowed down scanning; the cost of parallelism on a single disk was about **1.6×** the cost of a sequential scan【359301939943579†L197-L327】.  The author concluded that parallelizing `stat()` calls can hurt performance when there is only one storage device【359301939943579†L197-L343】.  This suggests that multi‑threaded scanning should be used sparingly.

### Write‑optimised file systems

File‑system research has also addressed scanning performance at the storage layer.  The 2016 FAST paper on BetrFS 0.2, a write‑optimised file system, showed that reorganising on‑disk metadata and adopting techniques such as late‑binding journaling and zoning allowed directory scans **2.2 ×** faster than conventional file systems【375065994139366†L48-L53】.  These gains arise from co‑locating related metadata on disk, but they require replacing the underlying file system rather than just improving the scanning tool.

### NTFS Master File Table and change journal

Windows‑specific tools such as *Everything* and *WizTree* achieve near‑instantaneous indexing by reading the NTFS Master File Table (MFT) and monitoring the NTFS change journal for updates.  Instead of walking every directory, they build an index from file metadata; the *Everything* documentation explains that it creates an index by reading file names from the MFT and then keeps the index up to date using the NTFS change journal【741093663994051†L160-L165】.  This approach requires administrator privileges and only works on NTFS/ReFS volumes, but it can outperform conventional scans by orders of magnitude.

### Duplicate detection research

A 2026 paper on duplicate detection in academic settings reviewed recent trends.  Early approaches used MD5 or SHA‑1 to generate hashes; by 2024, researchers adopted SHA‑256 for better collision resistance and security【769479142325282†L96-L118】.  By 2025, research explored machine‑learning‑based methods, but these systems were complex and resource‑intensive【769479142325282†L123-L137】.  The paper proposed a lightweight duplicate‑detection system that performs real‑time SHA‑256 hashing during upload and notifies users of duplicates, making it more practical for everyday use【769479142325282†L145-L150】.

### Disk geometry–aware traversal

The DAFT (Disk geometry‑Aware File system Traversal) research programme showed that reordering file access based on disk geometry can reduce the time to enumerate all files by **5–15×**【910515124673925†L82-L84】.  DAFT is based on computing the physical location of files and reading them in order, similar to the I/O‑scheduling paper above.  Implementing DAFT requires access to low‑level disk metadata (e.g., file extents), which is not directly exposed by high‑level APIs.

## Proposed Improvements for SpaceLens

Based on these findings, the following strategies can make SpaceLens more SOTA:

1. **Use `os.scandir` everywhere.**  Replace any remaining `os.walk` calls with an iterator built around `os.scandir` to cut the number of system calls and improve cache efficiency【581771386933928†L65-L91】.  My experiments below show this provides a measurable speed‑up.

2. **Hot‑zone and heuristic ordering.**  Begin by scanning likely “hot” folders (e.g., Downloads, Videos) so the UI can show meaningful results quickly.  Then scan remaining directories in descending order of estimated size.  This improves perceived speed, even if total scan time is unchanged.

3. **Optional NTFS MFT scanning.**  On Windows, add a mode that reads file metadata directly from the MFT and uses the NTFS change journal to stay up to date【741093663994051†L160-L165】.  This requires administrator rights but can drastically reduce scan time.  Libraries such as [omerbenamram/mft](https://github.com/omerbenamram/mft) provide cross‑platform code for parsing the MFT.

4. **User‑space I/O scheduling.**  Implement a mode that collects each file’s physical location (e.g., via `FIEMAP` on Linux or `FSCTL_GET_RETRIEVAL_POINTERS` on Windows) and sorts files by logical block number, then reads them in a single sweep【60756556511615†L99-L109】.  This “one‑sweep” method reduces disk seeks and can yield large performance gains on rotational drives.

5. **Snapshot caching and change monitoring.**  After an initial full scan, save a snapshot of file paths, sizes, and timestamps.  Use a lightweight file‑system watcher (e.g., `watchdog`) to update the cache when files change.  This avoids unnecessary rescans and makes subsequent runs instantaneous.

6. **Advanced duplicate detection.**  Adopt a two‑phase duplicate finder: first group files by size, then compute SHA‑256 hashes only within groups.  Optionally compute a partial hash (e.g., first 1 MB) before the full hash to prune non‑duplicates, striking a balance between speed and collision resistance【769479142325282†L96-L118】.

7. **Keep parallelism optional.**  Provide an option to parallelize scanning across multiple storage devices, but default to sequential scanning on single disks to avoid the overhead highlighted by prior research【359301939943579†L197-L327】.

## Custom Experiments

To evaluate the impact of different scanning strategies, I measured the time to compute the total size of a test directory under three methods: `os.walk` (baseline), a custom scanner built with `os.scandir`, and a parallel version that dispatches subdirectories to worker threads.  Two datasets were tested:

| Dataset            | Files | Size per file | Baseline (`os.walk`) | `os.scandir` | Parallel (`ThreadPool`) |
|--------------------|------:|--------------:|---------------------:|-------------:|------------------------:|
| 10 dirs × 200 files | 2 000 | 4 KiB         | 0.0090 s            | 0.0063 s    | 0.0519 s              |
| 20 dirs × 2 000 files | 40 000 | 512 B        | 0.2543 s            | 0.2002 s    | 0.7765 s              |

In both tests, the `os.scandir`‑based scanner outperformed the baseline by roughly **20 %**, while the parallel version was substantially slower due to thread‑management overhead on a single disk.  The results confirm that using `os.scandir` is a practical improvement, whereas multi‑threading should be applied only when scanning across multiple physical drives or when the operating system can overlap I/O requests.

Below are bar charts illustrating the timing differences for each dataset.

![2k files performance]({{file:file-KuuzWZN8isByg2a21x8csP}})

![40k files performance]({{file:file-Dxqxfjobp3KfCznpxWZCSD}})

## Conclusion

Recent research points to several avenues for improving disk‑usage analyzers.  Simple changes like replacing `os.walk` with `os.scandir` yield measurable speed‑ups【581771386933928†L65-L91】, while more sophisticated techniques—such as scheduling I/O across files【60756556511615†L99-L109】, leveraging NTFS metadata【741093663994051†L160-L165】, and storing snapshots with real‑time updates—offer the potential for order‑of‑magnitude improvements.  Parallel scanning and write‑optimised file systems are promising but must be used judiciously【359301939943579†L197-L327】【375065994139366†L48-L53】.

To make SpaceLens truly SOTA, I recommend integrating these improvements iteratively: start by switching to `os.scandir` and implementing snapshot caching, then offer optional NTFS‑MFT scanning and one‑sweep scheduling modes for advanced users.  Combining these techniques with thoughtful UX (hot‑zone scanning, duplicate detection, and rich exports) will produce a disk‑usage tool that is free, fast, and competitive with proprietary alternatives.
