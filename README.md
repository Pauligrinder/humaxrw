# HumaxRW 2026

Read recordings off a **Humax PVR-9200T / 9150T / 9300T** hard disk (or a `dd`/`ddrescue` image of one) on any current machine.

The original HumaxRW was a 32-bit Windows/Linux command-line tool (last public build **1.15**, xyz321). It cannot run natively on Apple Silicon. This is a from-scratch, **read-only** Python 3 replacement: no 32-bit libraries, no Wine, no extra packages.

Those boxes use a proprietary filesystem, not FAT or ext. Plug the drive in (or point this at an image) and extract MPEG-TS files you can play in VLC.

## Requirements

- Python 3.9 or later (the one that ships with macOS 15/16 is fine)
- A disk image, or the Humax drive attached as a raw device

## Usage

```bash
python3 humaxrw.py -l /path/to/humax-9200t.img
python3 humaxrw.py -g 49 -o ./out /path/to/humax-9200t.img
python3 humaxrw.py -g 2-10,49 -o ./out /path/to/humax-9200t.img
python3 humaxrw.py -b -o ./out /path/to/humax-9200t.img
```

Recording numbers are the Humax file numbers (`36.av` → `36`). Subcommands also work: `list`, `info`, `get`, `backup`.

Each extract writes:

| File | Contents |
|------|----------|
| `NNN Title.ts` | MPEG-TS video (playable in VLC / IINA / mpv) |
| `.elu` | Original timing sidecar |
| `.epg` | Original programme info |
| `.txt` / `.json` | Title and synopsis in plain text |

The tool never writes to the Humax image or disk.

## Disk images

A full-disk `ddrescue` image is the usual input:

```bash
sudo ddrescue -n /dev/rdiskN humax-9200t.img humax-9200t.log
python3 humaxrw.py list humax-9200t.img
```

You can also pass a raw device (`/dev/rdiskN` on macOS, `/dev/sdX` on Linux) if you would rather not image first. macOS may require `sudo` for raw disks.

## What this is *not*

- It does not talk to later Humax models (HDR-FOX T2, etc.). Those use a normal Linux filesystem.
- It does not write recordings back onto a Humax disk (the old `-p` / delete options).
- Encrypted HD recordings from later boxes are a different problem.

## Layout notes

The 9200-series disk has three partitions: a large AV volume, a 256 MiB EPG/metadata volume, and a small “user” volume. Recordings live as `N.av` + `N.elu` on the first, with `N.epg` (title/synopsis) on the second. Names and MPEG-TS payloads are stored as 32-bit MIPS words; this tool unpacks them into host byte order so the `.ts` files play in VLC. Recordings are often fragmented; the extractor follows the on-disk FAT (the first FAT word is a free-block count, so cluster *C* is at index *C+1*).
