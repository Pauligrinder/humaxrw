# HumaxRW 2026

Read-only extractor for **Humax PVR-9200T / 9150T / 9300T** hard-disk dumps. Drop-in spirit of the old 32-bit `humaxrw` 1.15 (xyz321), rewritten so it runs on Apple Silicon and any other current machine with Python 3.9+.

No packages to install. Clone and run:

```bash
git clone https://github.com/Pauligrinder/humaxrw.git
cd humaxrw
python3 humaxrw.py -l /path/to/dump.img
python3 humaxrw.py -g 1-20 -o ./recordings /path/to/dump.img
```

Works the same on macOS (Apple Silicon or Intel), Linux, and Windows. This tool **never writes** to the Humax image.

## What the original did

Those set-top boxes do **not** use FAT32 or ext3 for recordings. The drive has a custom partition table (signature `AA 55` at offset `0x1FC`), a proprietary directory/FAT, and MPEG-TS stored with every 32-bit word byte-swapped. `humaxrw` talked to the raw disk (Windows `2:`, Linux `/dev/sdb`) and could:

- list recordings
- copy them off as `.ts` plus `.hre` / `.elu` / `.epg` sidecars
- copy them back, delete, and repair a corrupt record list

This 2026 port covers the part people still need: **open a dump or a raw disk and get playable `.ts` files out**. It never writes to the Humax image.

## Typical workflow

1. Image the drive with `dd` or `ddrescue` (safer than working on the failing disk).
2. List what is on it:

   ```bash
   python3 humaxrw.py -l dump.img
   ```

3. Extract a range, or everything:

   ```bash
   python3 humaxrw.py -g 2-50,80 -o ./out dump.img
   python3 humaxrw.py -b -o ./out dump.img
   ```

4. Play the `.ts` files in VLC or ffmpeg.

If the record list is corrupt, recovery mode still uses the on-disk directory:

```bash
python3 humaxrw.py -r -l dump.img
python3 humaxrw.py -r -g 2-100 -o ./out dump.img
```

If even the directory is gone, carve MPEG-TS off the image (slow on large dumps):

```bash
python3 humaxrw.py --carve -o ./out dump.img
```

On macOS a live disk looks like `/dev/diskN` (use Disk Utility / `diskutil list`, then `sudo`). Prefer a dump.

## Command line

| Flag | Meaning |
| --- | --- |
| `-l` | List recordings |
| `-g LIST` | Get recordings (`10-20,30,41-42` or `all`) |
| `-i LIST` | Extra info for those numbers |
| `-b` | Backup / extract all programme files |
| `-r` | Recovery mode (ignore titles if metadata is junk) |
| `-n` | Do not parse EPG / titles |
| `-o DIR` | Output directory |
| `--sidecar` | Also write `.elu` / `.epg` / `.hre` |
| `--carve` | Scan the image for MPEG-TS |
| `--json` | Machine-readable listing |
| `--overwrite` | Replace existing output files |
| `-v` | Version |

`LIST` syntax matches the original tool.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Limits

- Read-only: no delete, put, unprotect, or partition repair (`humaxcheck -p`).
- Titles come from `_RECORD_LIST_` / `.epg` when those files are intact; otherwise you get `2.av`-style names.
- Fragmented files that are not contiguous on disk may extract truncated (the contiguous start+size path is what almost all dumps need). Use `--carve` if a file looks wrong.
- 9200C directory records (0x130 bytes) are detected; writes to 9200C were never the goal.

Original HumaxRW was © xyz321. This is an independent, read-only reimplementation of the published on-disk behaviour.

## License

MIT. See [LICENSE](LICENSE).
