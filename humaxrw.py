#!/usr/bin/env python3
"""HumaxRW 2026 — read Humax 9200T / 9150T / 9300T disks and disk images.

Pure Python 3, no third-party dependencies. Works on macOS (Apple Silicon and
Intel), Linux, and Windows. Read-only: it never writes back to the Humax disk.

The original HumaxRW (xyz321, last public build 1.15) was a 32-bit Windows/Linux
binary. This is a from-scratch reader of that proprietary on-disk layout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from struct import unpack_from
from typing import BinaryIO, Iterable, Iterator, Optional

VERSION = "2026.1"
SECTOR = 512
DIR_ENTRY_SIZE = 128
TS_PACKET = 188

FLAG_ELU = 0x1000
FLAG_AV = 0x1002
FLAG_TIMESHIFT = 0x1006
FLAG_BUFFER2 = 0x1026
FAT_EOF = {0, 0xFFFFFFFE, 0xFFFFFFFF, 0x0FFFFFFF}


def u16le(buf: bytes, off: int) -> int:
    return unpack_from("<H", buf, off)[0]


def u32le(buf: bytes, off: int) -> int:
    return unpack_from("<I", buf, off)[0]


def swap32(data: bytes) -> bytes:
    """Reverse each 32-bit word (Humax MIPS on-disk packing)."""
    n = len(data) - (len(data) % 4)
    out = bytearray(data)
    for i in range(0, n, 4):
        out[i : i + 4] = out[i : i + 4][::-1]
    return bytes(out)


def decode_humax_name(raw: bytes) -> str:
    swapped = swap32(raw).split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    plain = raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()

    def score(name: str) -> int:
        if re.fullmatch(r"\d+\.(av|elu|epg|hre)", name, re.I):
            return 3
        if name.startswith("_") and name.endswith("_"):
            return 3
        if name and all(32 <= ord(c) < 127 for c in name) and "." in name:
            return 1
        return 0

    return swapped if score(swapped) >= score(plain) else plain


def latin_strings(data: bytes, min_len: int = 4) -> list[str]:
    out: list[str] = []
    cur = bytearray()
    for c in data:
        if 32 <= c < 127 or c >= 160:
            cur.append(c)
        else:
            if len(cur) >= min_len:
                out.append(bytes(cur).decode("latin-1"))
            cur.clear()
    if len(cur) >= min_len:
        out.append(bytes(cur).decode("latin-1"))
    return out


def human_size(n: int) -> str:
    for unit, div in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def sanitize_filename(name: str, max_len: int = 80) -> str:
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in name).strip(" .")
    cleaned = " ".join(cleaned.split())
    return (cleaned or "recording")[:max_len]


def fat_next_cluster(fat: bytes, cluster: int) -> Optional[int]:
    """Return the next cluster, or None at end-of-chain.

    Humax FAT[0] holds the free-block count, so the entry for cluster C lives
    at index C+1. For a contiguous file starting at C, FAT[C+1] == C+1.
    """
    n = len(fat) // 4
    idx = cluster + 1
    if idx < 0 or idx >= n:
        return None
    val = u32le(fat, idx * 4)
    if val in FAT_EOF or val == 0:
        return None
    if 0 < val < n:
        return val
    return None


def parse_id_list(spec: str) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower() in ("all", "*"):
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if hi < lo:
                lo, hi = hi, lo
            for n in range(lo, hi + 1):
                if n not in seen:
                    seen.add(n)
                    out.append(n)
        else:
            n = int(part)
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


@dataclass
class Partition:
    index: int
    start_lba: int
    size_lba: int
    dir_lba: int
    dir_size_lba: int
    fat_lba: int
    fat_size_lba: int
    data_lba: int
    spc: int
    superblock: bytes = field(repr=False, default=b"")

    def cluster_lba(self, cluster: int) -> int:
        return self.data_lba + cluster * self.spc


@dataclass
class DirEntry:
    name: str
    start_lba: int
    last_lba: int
    sectors: int
    size: int
    flags: int
    extra: int

    @property
    def number(self) -> Optional[int]:
        stem = self.name.rsplit(".", 1)[0]
        return int(stem) if stem.isdigit() else None

    @property
    def suffix(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""

    @property
    def is_buffer(self) -> bool:
        if self.flags in (FLAG_TIMESHIFT, FLAG_BUFFER2):
            return True
        return self.name.lower() in ("0.av", "1.av")


@dataclass
class Recording:
    number: int
    av: DirEntry
    elu: Optional[DirEntry] = None
    epg: Optional[DirEntry] = None
    title: str = ""
    synopsis: str = ""

    @property
    def size(self) -> int:
        return self.av.size


def _looks_like_superblock(sb: bytes) -> bool:
    if len(sb) < SECTOR:
        return False
    if sum(1 for b in sb[:0xE0] if b == 0) < 160:
        return False
    spc = u16le(sb, 0xE6)
    dir_lba = u32le(sb, 0xFC)
    return 1 <= spc <= 4096 and dir_lba != 0


class HumaxDisk:
    def __init__(self, source: str | os.PathLike[str]):
        self.path = Path(source)
        self._fp: Optional[BinaryIO] = None
        self.size = 0
        self.partitions: list[Partition] = []
        self._fat: Optional[bytes] = None
        self._av_dir: list[DirEntry] = []
        self._meta_dir: list[DirEntry] = []
        self._recordings: Optional[list[Recording]] = None

    def __enter__(self) -> "HumaxDisk":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        self._fp = open(self.path, "rb")
        try:
            self.size = os.fstat(self._fp.fileno()).st_size
        except OSError:
            self.size = 0
        sector0 = self.read_lba(0, 1)
        if len(sector0) < SECTOR:
            raise RuntimeError("Could not read sector 0 — is this a disk image?")
        starts = self._parse_partition_table(sector0)
        if not starts:
            raise RuntimeError("Not a Humax 9000-series disk (no partition table).")
        for i, (start, size) in enumerate(starts):
            part = self._read_superblock(i, start, size)
            if part is not None:
                self.partitions.append(part)
        if not self.partitions:
            raise RuntimeError("Partition table found but superblocks are unreadable.")
        self._av_dir = self._read_directory(self.partitions[0])
        if len(self.partitions) > 1:
            self._meta_dir = self._read_directory(self.partitions[1])

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None

    def read_lba(self, lba: int, count: int = 1) -> bytes:
        assert self._fp is not None
        self._fp.seek(lba * SECTOR)
        return self._fp.read(count * SECTOR)

    def read_at(self, lba: int, nbytes: int) -> bytes:
        assert self._fp is not None
        self._fp.seek(lba * SECTOR)
        return self._fp.read(nbytes)

    def _parse_partition_table(self, sector0: bytes) -> list[tuple[int, int]]:
        if sector0[0x1FC:0x1FE] not in (b"\xaa\x55", b"\x55\xaa"):
            return []
        max_lba = max(self.size // SECTOR, 1) if self.size else 1 << 32
        # Custom Humax MBR: type 0xAF entries, start LBA as hi<<16 | lo.
        p1 = u16le(sector0, 0x1CE) or 0x10
        p2 = (u16le(sector0, 0x1D8) << 16) | u16le(sector0, 0x1DE)
        p3 = (u16le(sector0, 0x1E8) << 16) | u16le(sector0, 0x1EE)
        starts = []
        for start in (p1, p2, p3):
            if 0x10 <= start < max_lba and start not in starts:
                starts.append(start)
        if 0x10 not in starts:
            starts.insert(0, 0x10)
        parts: list[tuple[int, int]] = []
        for i, start in enumerate(starts):
            nxt = starts[i + 1] if i + 1 < len(starts) else max_lba
            parts.append((start, max(nxt - start, 1)))
        return parts

    def _read_superblock(self, index: int, start: int, size: int) -> Optional[Partition]:
        sb = self.read_lba(start, 1)
        if not _looks_like_superblock(sb):
            return None
        spc = u16le(sb, 0xE6) or 1
        # Pointers in the superblock are absolute LBAs on every partition.
        dir_lba = u32le(sb, 0xFC)
        fat_lba = u32le(sb, 0x104)
        data_lba = u32le(sb, 0x10C)
        dir_size = u32le(sb, 0x110) or 0x64
        fat_size = u32le(sb, 0x11C) or 1
        return Partition(
            index=index,
            start_lba=start,
            size_lba=size,
            dir_lba=dir_lba,
            dir_size_lba=min(dir_size, 0x200),
            fat_lba=fat_lba,
            fat_size_lba=fat_size,
            data_lba=data_lba,
            spc=spc,
            superblock=sb,
        )

    def _read_directory(self, part: Partition) -> list[DirEntry]:
        raw = self.read_lba(part.dir_lba, part.dir_size_lba)
        entries: list[DirEntry] = []
        for off in range(0, len(raw), DIR_ENTRY_SIZE):
            ent = raw[off : off + DIR_ENTRY_SIZE]
            if len(ent) < DIR_ENTRY_SIZE or ent == b"\x00" * DIR_ENTRY_SIZE:
                continue
            name = decode_humax_name(ent[0x40:0x80])
            if not name:
                continue
            entries.append(
                DirEntry(
                    name=name,
                    start_lba=u32le(ent, 0x0C),
                    last_lba=u32le(ent, 0x14),
                    sectors=u32le(ent, 0x1C),
                    size=u32le(ent, 0x24),
                    flags=u32le(ent, 0x28),
                    extra=u32le(ent, 0x2C),
                )
            )
        return entries

    def _load_fat(self) -> bytes:
        if self._fat is None:
            p = self.partitions[0]
            self._fat = self.read_lba(p.fat_lba, p.fat_size_lba)
        return self._fat

    def _fat_next(self, cluster: int) -> Optional[int]:
        return fat_next_cluster(self._load_fat(), cluster)

    def iter_file_clusters(self, entry: DirEntry) -> Iterator[int]:
        part = self.partitions[0]
        nclus = max(1, (entry.sectors + part.spc - 1) // part.spc)
        start_c = (entry.start_lba - part.data_lba) // part.spc
        fat_n = len(self._load_fat()) // 4
        seen: set[int] = set()
        c = start_c
        for _ in range(nclus):
            if c < 0 or c >= fat_n or c in seen:
                break
            seen.add(c)
            yield c
            nxt = self._fat_next(c)
            if nxt is None:
                break
            c = nxt

    def iter_file_bytes(self, entry: DirEntry) -> Iterator[bytes]:
        """Read the file, dword-swap MPEG-TS, yield chunks."""
        part = self.partitions[0]
        remaining = entry.size
        cluster_bytes = part.spc * SECTOR
        start_c = (entry.start_lba - part.data_lba) // part.spc
        clusters = list(self.iter_file_clusters(entry))
        contiguous = clusters == list(range(start_c, start_c + len(clusters)))
        assert self._fp is not None
        if contiguous or not clusters:
            self._fp.seek(entry.start_lba * SECTOR)
            while remaining > 0:
                n = min(1024 * 1024, remaining)
                data = self._fp.read(n)
                if not data:
                    break
                yield swap32(data)
                remaining -= len(data)
            return
        for cluster in clusters:
            if remaining <= 0:
                break
            n = min(cluster_bytes, remaining)
            data = self.read_at(part.cluster_lba(cluster), n)
            if not data:
                break
            yield swap32(data)
            remaining -= len(data)

    def recordings(self) -> list[Recording]:
        if self._recordings is not None:
            return self._recordings
        avs = {
            e.number: e
            for e in self._av_dir
            if e.suffix == "av" and e.number is not None and not e.is_buffer
        }
        elus = {e.number: e for e in self._av_dir if e.suffix == "elu" and e.number is not None}
        epgs = {e.number: e for e in self._meta_dir if e.suffix == "epg" and e.number is not None}
        recs: list[Recording] = []
        for num in sorted(avs):
            epg_ent = epgs.get(num)
            title, synopsis = ("", "")
            if epg_ent is not None:
                title, synopsis = self._parse_epg(epg_ent)
            recs.append(
                Recording(
                    number=num,
                    av=avs[num],
                    elu=elus.get(num),
                    epg=epg_ent,
                    title=title,
                    synopsis=synopsis,
                )
            )
        self._recordings = recs
        return recs

    def _parse_epg(self, entry: DirEntry) -> tuple[str, str]:
        raw = self.read_at(entry.start_lba, min(entry.size, 8192))
        strings = latin_strings(swap32(raw), min_len=4)
        title = strings[0].strip() if strings else ""
        synopsis = ""
        for s in strings[1:]:
            s = s.strip()
            if len(s) >= 12 and s != title:
                synopsis = s
                break
        return title, synopsis

    def find_recordings(self, spec: str) -> list[Recording]:
        recs = self.recordings()
        if spec.strip().lower() in ("all", "*"):
            return recs
        by_num = {r.number: r for r in recs}
        out: list[Recording] = []
        seen: set[int] = set()
        for n in parse_id_list(spec):
            rec = by_num.get(n)
            if rec is None and 1 <= n <= len(recs):
                rec = recs[n - 1]
            if rec is not None and rec.number not in seen:
                seen.add(rec.number)
                out.append(rec)
        return out


def ts_align(data: bytes) -> tuple[int, bytes]:
    """Skip leading padding and return (offset, packet-aligned payload)."""
    if len(data) < TS_PACKET * 3:
        return 0, data
    search = min(len(data) - TS_PACKET * 2, 1024)
    for off in range(search):
        if (
            data[off] == 0x47
            and data[off + TS_PACKET] == 0x47
            and data[off + TS_PACKET * 2] == 0x47
        ):
            aligned = data[off:]
            n = (len(aligned) // TS_PACKET) * TS_PACKET
            return off, aligned[:n] if n else aligned
    return 0, data


def extract_recording(
    disk: HumaxDisk,
    rec: Recording,
    out_dir: Path,
    *,
    sidecars: bool = True,
    progress: bool = True,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    label = sanitize_filename(f"{rec.number:03d} {rec.title}" if rec.title else f"{rec.number:03d}")
    ts_path = out_dir / f"{label}.ts"
    first = True
    written = 0
    align_skip = 0
    leftover = b""
    with ts_path.open("wb") as out:
        for chunk in disk.iter_file_bytes(rec.av):
            if first:
                align_skip, chunk = ts_align(chunk)
                first = False
            data = leftover + chunk
            n = (len(data) // TS_PACKET) * TS_PACKET
            out.write(data[:n])
            leftover = data[n:]
            written += n
            if progress:
                pct = min(100.0, 100.0 * written / max(rec.size, 1))
                print(
                    f"\r  {ts_path.name}: {human_size(written)} / {human_size(rec.size)} ({pct:5.1f}%)",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
        if leftover:
            out.write(leftover)
            written += len(leftover)
    if progress:
        print(file=sys.stderr)
    if sidecars:
        if rec.elu is not None:
            (out_dir / f"{label}.elu").write_bytes(disk.read_at(rec.elu.start_lba, rec.elu.size))
        if rec.epg is not None:
            (out_dir / f"{label}.epg").write_bytes(disk.read_at(rec.epg.start_lba, rec.epg.size))
        meta = {
            "number": rec.number,
            "title": rec.title,
            "synopsis": rec.synopsis,
            "bytes": rec.size,
            "written": written,
            "ts_sync_skip": align_skip,
            "source": str(disk.path),
        }
        (out_dir / f"{label}.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        if rec.title or rec.synopsis:
            (out_dir / f"{label}.txt").write_text(
                f"{rec.title}\n\n{rec.synopsis}\n", encoding="utf-8"
            )
    return ts_path


def cmd_list(disk: HumaxDisk, as_json: bool = False) -> None:
    recs = disk.recordings()
    if as_json:
        payload = [
            {
                "index": i + 1,
                "number": r.number,
                "title": r.title,
                "bytes": r.size,
                "sectors": r.av.sectors,
                "synopsis": r.synopsis,
            }
            for i, r in enumerate(recs)
        ]
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return
    print(f"Humax disk: {disk.path}")
    print(f"Partitions: {len(disk.partitions)}")
    if not recs:
        print("No recordings found.")
        return
    print(f"{'#':>4} {'No':>4} {'Size':>10}  Title")
    print("-" * 72)
    for i, r in enumerate(recs, 1):
        title = r.title or r.av.name
        print(f"{i:4d} {r.number:4d} {human_size(r.size):>10}  {title}")
    total = sum(r.size for r in recs)
    print("-" * 72)
    print(f"{len(recs)} recordings, {human_size(total)} of MPEG-TS")


def cmd_info(disk: HumaxDisk, spec: Optional[str]) -> None:
    recs = disk.find_recordings(spec) if spec else disk.recordings()
    p0 = disk.partitions[0]
    print(f"Image:      {disk.path}")
    print(f"Size:       {human_size(disk.size)} ({disk.size} bytes)")
    print(f"AV LBA:     {p0.start_lba:#x}  cluster={p0.spc} sectors ({p0.spc * SECTOR} bytes)")
    print(f"Directory:  LBA {p0.dir_lba:#x}")
    print(f"FAT:        LBA {p0.fat_lba:#x}, {p0.fat_size_lba} sectors")
    print(f"Data:       LBA {p0.data_lba:#x}")
    print()
    for r in recs:
        print(f"Recording {r.number}: {r.title or r.av.name}")
        print(f"  MPEG-TS    {human_size(r.size)}  start LBA {r.av.start_lba:#x}")
        if r.elu:
            print(f"  timing     {human_size(r.elu.size)}")
        if r.epg:
            print(f"  epg        {human_size(r.epg.size)}")
        if r.synopsis:
            syn = r.synopsis if len(r.synopsis) < 240 else r.synopsis[:237] + "..."
            print(f"  synopsis   {syn}")
        print()


def cmd_get(disk: HumaxDisk, spec: str, out_dir: Path, sidecars: bool) -> None:
    recs = disk.find_recordings(spec)
    if not recs:
        raise SystemExit(f"No recordings matched {spec!r}")
    for rec in recs:
        print(f"Extracting {rec.number} — {rec.title or rec.av.name}", file=sys.stderr)
        path = extract_recording(disk, rec, out_dir, sidecars=sidecars)
        print(path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="humaxrw",
        description="Read recordings from a Humax 9200T/9150T/9300T disk or dd image.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python3 humaxrw.py -l dump.img
  python3 humaxrw.py -g 49 -o ./out dump.img
  python3 humaxrw.py list dump.img
  python3 humaxrw.py get dump.img 1-5,49 -o ./out
""",
    )
    p.add_argument("-V", "--version", action="version", version=f"HumaxRW {VERSION}")
    p.add_argument("-l", action="store_true", help="list recordings")
    p.add_argument("-g", metavar="LIST", help="get recordings (e.g. 1-10,49 or all)")
    p.add_argument("-i", nargs="?", const="", metavar="LIST", help="show info")
    p.add_argument("-b", action="store_true", help="extract all recordings")
    p.add_argument("-o", "--output", default=".", help="output directory")
    p.add_argument("--json", action="store_true", help="JSON listing")
    p.add_argument("--no-sidecars", action="store_true", help="write only the .ts file")
    p.add_argument(
        "words",
        nargs="*",
        help="IMAGE, or: list|info|get|backup IMAGE [ids]",
    )
    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    words = list(args.words)
    cmd = None
    image = None
    ids = None
    if words and words[0] in ("list", "info", "get", "backup"):
        cmd = words.pop(0)
        if words:
            image = words.pop(0)
        if words:
            ids = words[0]
    elif words:
        image = words[0]

    if args.l:
        cmd = cmd or "list"
    elif args.g:
        cmd = cmd or "get"
        ids = args.g
    elif args.i is not None:
        cmd = cmd or "info"
        ids = args.i or ids
    elif args.b:
        cmd = cmd or "backup"

    if not cmd:
        cmd = "list"
    if not image:
        parser.print_help()
        return 2

    with HumaxDisk(image) as disk:
        if cmd == "list":
            cmd_list(disk, as_json=args.json)
        elif cmd == "info":
            cmd_info(disk, ids)
        elif cmd == "get":
            if not ids:
                raise SystemExit("get requires a recording list, e.g. -g 49 or get IMAGE 49")
            cmd_get(disk, ids, Path(args.output), sidecars=not args.no_sidecars)
        elif cmd == "backup":
            cmd_get(disk, "all", Path(args.output), sidecars=not args.no_sidecars)
        else:
            parser.print_help()
            return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
    except KeyboardInterrupt:
        raise SystemExit(130)
