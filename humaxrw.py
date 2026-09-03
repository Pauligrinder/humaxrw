#!/usr/bin/env python3
"""HumaxRW 2026 — extract recordings from Humax 9200T/9150T/9300T disk images.

The original HumaxRW 1.15 (xyz321, ~2013) was a 32-bit Windows/Linux/MIPSel
tool for a proprietary PVR filesystem. This is a read-only reimplementation
that runs on any machine with Python 3.9+ (including Apple Silicon) and works
directly on dd/ddrescue dumps as well as block devices.

Disk layout (reverse-engineered from contemporary documentation and the 1.15
binary's behaviour):

* Custom partition table in sector 0, signature ``AA 55`` at offset 0x1FC.
* Partition 1 (recordings) typically starts at LBA 16 (byte 0x2000).
* Each partition begins with a 512-byte header. On 9200T dumps:
    * +0x0FC  directory start LBA
    * +0x104  FAT start LBA
* Directory entries are 0x80 bytes (0x130 on 9200C). For each entry:
    * +0x0C  start LBA (uint32 LE)
    * +0x14  last-block LBA
    * +0x1C  allocated 512-byte sectors
    * +0x24  file size in bytes
    * +0x28  type (0x1002 = recording, 0x1006 = timeshift buffer)
    * +0x40  ASCII name (``2.av``, ``2.elu``, ``_RECORD_LIST_``, …)
* MPEG-TS is often stored with every 32-bit word byte-swapped; extraction
  undoes that when needed so VLC/ffmpeg see a normal 188-byte TS.
* Programme titles live in ``_RECORD_LIST_`` / ``N.epg`` on partition 2.

This tool never writes to the Humax image.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional, Sequence

VERSION = "2026.1"
SECTOR = 512
DIR_ENTRY_9200T = 0x80
DIR_ENTRY_9200C = 0x130
TYPE_RECORDING = 0x1002
TYPE_BUFFER = 0x1006
MBR_SIG_OFF = 0x1FC
PART_HEADER_LBA = 16
HEADER_DIR_OFF = 0x0FC
HEADER_FAT_OFF = 0x104
NAME_OFF = 0x40
NAME_LEN = 32

__all__ = [
    "VERSION",
    "HumaxError",
    "DirEntry",
    "Recording",
    "HumaxDisk",
    "bswap32",
    "parse_list",
    "main",
]


class HumaxError(Exception):
    """Unrecoverable problem reading a Humax image."""


def u32(buf: bytes, off: int = 0) -> int:
    return int.from_bytes(buf[off : off + 4], "little")


def u16(buf: bytes, off: int = 0) -> int:
    return int.from_bytes(buf[off : off + 2], "little")


def bswap32(data: bytes | bytearray) -> bytes:
    """Byte-swap every 32-bit word (Humax 9200T transport-stream storage)."""
    n = len(data) - (len(data) % 4)
    out = bytearray(data)
    for i in range(0, n, 4):
        out[i], out[i + 1], out[i + 2], out[i + 3] = (
            out[i + 3],
            out[i + 2],
            out[i + 1],
            out[i],
        )
    return bytes(out)


def ts_sync_score(data: bytes, limit: int = 188 * 24) -> int:
    """How many 188-byte 0x47 syncs we see at the start of *data*."""
    chunk = data[:limit]
    if len(chunk) < 188:
        return 1 if chunk[:1] == b"\x47" else 0
    score = 0
    for i in range(0, len(chunk) - 187, 188):
        if chunk[i] == 0x47:
            score += 1
    return score


def needs_bswap(data: bytes) -> bool:
    raw = ts_sync_score(data)
    swapped = ts_sync_score(bswap32(data[: min(len(data), 188 * 32)]))
    return swapped > raw and swapped >= 3


def c_string(buf: bytes) -> str:
    end = buf.find(b"\x00")
    if end >= 0:
        buf = buf[:end]
    text = buf.decode("latin-1", "replace").strip()
    return "".join(ch if ch.isprintable() else " " for ch in text).strip()


def printable_runs(buf: bytes, min_len: int = 4) -> list[str]:
    runs: list[str] = []
    cur: list[str] = []
    for b in buf:
        if 32 <= b < 127 and b not in (0x7F,):
            cur.append(chr(b))
        else:
            if len(cur) >= min_len:
                runs.append("".join(cur).strip())
            cur = []
    if len(cur) >= min_len:
        runs.append("".join(cur).strip())
    return [r for r in runs if r]


def parse_list(spec: str, hi: int) -> list[int]:
    """Parse a HumaxRW-style list: ``10-20,30,41-42`` (1-based, inclusive)."""
    spec = spec.strip()
    if spec in ("*", "all"):
        return list(range(1, hi + 1))
    out: list[int] = []
    seen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
            if a > b:
                a, b = b, a
            for n in range(a, b + 1):
                if 1 <= n <= hi and n not in seen:
                    seen.add(n)
                    out.append(n)
        else:
            n = int(part)
            if 1 <= n <= hi and n not in seen:
                seen.add(n)
                out.append(n)
    return out


def human_bytes(n: int) -> str:
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if v < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(v)} {unit}"
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{n} B"


def safe_filename(name: str, fallback: str) -> str:
    name = name.strip() or fallback
    name = re.sub(r"[/\\:*?\"<>|\x00-\x1f]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:180] or fallback


@dataclass
class DirEntry:
    index: int
    name: str
    start_lba: int
    last_lba: int
    alloc_sectors: int
    size: int
    type_id: int
    raw: bytes
    directory_offset: int
    entry_size: int

    @property
    def is_recording(self) -> bool:
        if self.type_id == TYPE_BUFFER:
            return False
        if self.type_id == TYPE_RECORDING:
            return True
        # Recovery / unknown type: treat numbered .av files as recordings.
        return bool(re.fullmatch(r"\d+\.av", self.name, re.I))

    @property
    def is_buffer(self) -> bool:
        return self.type_id == TYPE_BUFFER or self.name.lower() in ("0.av", "1.av")

    @property
    def rec_num(self) -> Optional[int]:
        m = re.fullmatch(r"(\d+)\.(av|elu|epg|hre)", self.name, re.I)
        return int(m.group(1)) if m else None

    @property
    def suffix(self) -> str:
        if "." in self.name:
            return self.name.rsplit(".", 1)[-1].lower()
        return ""


@dataclass
class Recording:
    number: int
    av: DirEntry
    elu: Optional[DirEntry] = None
    epg: Optional[DirEntry] = None
    title: str = ""
    channel: str = ""
    start: Optional[_dt.datetime] = None
    duration_s: int = 0
    protected: bool = False
    extra: str = ""

    def display_name(self) -> str:
        if self.title:
            return self.title
        return self.av.name

    def output_stem(self) -> str:
        if self.start and self.title:
            stamp = self.start.strftime("%Y%m%d %H%M")
            return safe_filename(f"{stamp} {self.title}", f"{self.number:03d}")
        if self.title:
            return safe_filename(self.title, f"{self.number:03d}")
        return f"{self.number:03d}_{self.av.name}"

    def list_line(self) -> str:
        if self.start:
            when = self.start.strftime("%a %d/%m/%y %H:%M")
        else:
            when = ""
        if self.duration_s:
            h, rem = divmod(self.duration_s, 3600)
            m, s = divmod(rem, 60)
            dur = f"{h}:{m:02d}:{s:02d}"
        else:
            dur = human_bytes(self.av.size)
        ch = self.channel or ""
        title = self.display_name()
        prot = " (Protected)" if self.protected else ""
        if when and ch:
            body = f"{when:<24} {dur:>10} {ch} - {title}"
        elif when:
            body = f"{when:<24} {dur:>10} {title}"
        else:
            kind = "buffer" if self.av.is_buffer else "recording"
            body = f"{title:<24} {dur:>10}  {kind}  lba=0x{self.av.start_lba:x}"
        return f"{self.number:3d}: {body}{prot}"

    def to_json(self) -> dict:
        return {
            "number": self.number,
            "name": self.av.name,
            "title": self.title,
            "channel": self.channel,
            "start": self.start.isoformat() if self.start else None,
            "duration_s": self.duration_s,
            "size": self.av.size,
            "start_lba": self.av.start_lba,
            "type": self.av.type_id,
            "buffer": self.av.is_buffer,
            "protected": self.protected,
        }


@dataclass
class Partition:
    index: int
    start_lba: int
    dir_lba: int
    fat_lba: int
    entry_size: int
    entries: list[DirEntry] = field(default_factory=list)


class ImageIO:
    def __init__(self, path: Path):
        self.path = path
        self.fp: BinaryIO = open(path, "rb")
        try:
            self.size = self.fp.seek(0, os.SEEK_END)
        except OSError:
            self.size = 0

    def close(self) -> None:
        self.fp.close()

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0:
            raise HumaxError(f"negative read offset {offset}")
        self.fp.seek(offset)
        data = self.fp.read(size)
        return data

    def read_lba(self, lba: int, count: int = 1) -> bytes:
        return self.read_at(lba * SECTOR, count * SECTOR)


def _looks_like_name(name: str) -> bool:
    if not name or len(name) > 31:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._+\- ]+", name):
        return False
    return "." in name or name.startswith("_")


def parse_dir_entry(raw: bytes, index: int, directory_offset: int, entry_size: int) -> Optional[DirEntry]:
    if len(raw) < 0x50:
        return None
    name = c_string(raw[NAME_OFF : NAME_OFF + NAME_LEN])
    if not _looks_like_name(name):
        return None
    start = u32(raw, 0x0C)
    last = u32(raw, 0x14)
    alloc = u32(raw, 0x1C)
    size = u32(raw, 0x24)
    type_id = u32(raw, 0x28)
    if size > 1 << 40:
        return None
    return DirEntry(
        index=index,
        name=name,
        start_lba=start,
        last_lba=last,
        alloc_sectors=alloc,
        size=size,
        type_id=type_id,
        raw=raw,
        directory_offset=directory_offset,
        entry_size=entry_size,
    )


def parse_directory(blob: bytes, directory_offset: int, entry_size: int) -> list[DirEntry]:
    entries: list[DirEntry] = []
    n = len(blob) // entry_size
    for i in range(n):
        raw = blob[i * entry_size : (i + 1) * entry_size]
        if raw == b"\x00" * entry_size or not raw.strip(b"\x00"):
            continue
        ent = parse_dir_entry(raw, i, directory_offset + i * entry_size, entry_size)
        if ent:
            entries.append(ent)
    return entries


def directory_quality(entries: Sequence[DirEntry]) -> int:
    score = 0
    for e in entries:
        if re.fullmatch(r"\d+\.av", e.name, re.I):
            score += 3
        elif e.name.endswith((".elu", ".epg", ".av")):
            score += 2
        elif e.name.startswith("_"):
            score += 4
        else:
            score += 1
    return score


class HumaxDisk:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        if not self.path.exists():
            raise HumaxError(f"Humax disk not found: {self.path}")
        self.io = ImageIO(self.path)
        self.partitions: list[Partition] = []
        self.entries: list[DirEntry] = []
        self.recordings: list[Recording] = []
        self.model = "9200-series"
        self._open()

    def close(self) -> None:
        self.io.close()

    def __enter__(self) -> "HumaxDisk":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _open(self) -> None:
        if self.io.size < SECTOR * 32:
            raise HumaxError("Image is too small to be a Humax PVR disk")
        mbr = self.io.read_lba(0)
        if mbr[MBR_SIG_OFF : MBR_SIG_OFF + 2] not in (b"\xAA\x55", b"\x55\xAA"):
            # Still try: some dumps start at the first partition, or the
            # signature was wiped by Windows "initialise disk".
            self.model = "signature-missing"
        self.partitions = self._discover_partitions(mbr)
        if not self.partitions:
            raise HumaxError("Unknown Humax disk format (no directory found)")
        seen_off: set[int] = set()
        for part in self.partitions:
            for e in part.entries:
                if e.directory_offset not in seen_off:
                    seen_off.add(e.directory_offset)
                    self.entries.append(e)
        self.recordings = self._build_recordings()

    def _discover_partitions(self, mbr: bytes) -> list[Partition]:
        candidates = self._candidate_header_lbas(mbr)
        parts: list[Partition] = []
        seen: set[int] = set()
        for i, lba in enumerate(candidates):
            part = self._try_partition(i + 1, lba)
            if part and part.dir_lba not in seen and part.entries:
                seen.add(part.dir_lba)
                parts.append(part)
        if not parts:
            scanned = self._scan_for_directory(0, min(self.io.size, 32 * 1024 * 1024))
            if scanned:
                parts.append(scanned)
        return parts

    def _candidate_header_lbas(self, mbr: bytes) -> list[int]:
        found: list[int] = [PART_HEADER_LBA]
        max_lba = max(self.io.size // SECTOR, 1)
        # uint32 LE values in the custom MBR area that look like LBAs.
        for off in range(0x1B0, 0x1FC, 2):
            if off + 4 > len(mbr):
                break
            val = u32(mbr, off)
            if 16 <= val < max_lba and val not in found:
                # Prefer values that are sector-aligned headers (often 0x10).
                found.append(val)
        # Common second/third partition locations from 160–500 GB boxes.
        # These are cheap header-only probes.
        return found

    def _try_partition(self, index: int, start_lba: int) -> Optional[Partition]:
        try:
            hdr = self.io.read_lba(start_lba)
        except OSError:
            return None
        if len(hdr) < SECTOR:
            return None
        dir_lba = u32(hdr, HEADER_DIR_OFF)
        fat_lba = u32(hdr, HEADER_FAT_OFF)
        max_lba = max(self.io.size // SECTOR, 1)
        if not (1 <= dir_lba < max_lba):
            # Pointers might be relative to the partition start.
            rel = start_lba + dir_lba
            if 1 <= rel < max_lba:
                dir_lba = rel
                fat_lba = start_lba + fat_lba
            else:
                return None
        for entry_size in (DIR_ENTRY_9200T, DIR_ENTRY_9200C):
            part = self._read_partition(index, start_lba, dir_lba, fat_lba, entry_size)
            if part and directory_quality(part.entries) >= 2:
                if entry_size == DIR_ENTRY_9200C:
                    self.model = "9200C"
                return part
        return None

    def _read_partition(
        self,
        index: int,
        start_lba: int,
        dir_lba: int,
        fat_lba: int,
        entry_size: int,
    ) -> Optional[Partition]:
        # Read up to 256 KiB of directory (2048 × 128-byte entries).
        blob = self.io.read_at(dir_lba * SECTOR, 256 * 1024)
        if len(blob) < entry_size:
            return None
        entries = parse_directory(blob, dir_lba * SECTOR, entry_size)
        if not entries:
            return None
        return Partition(
            index=index,
            start_lba=start_lba,
            dir_lba=dir_lba,
            fat_lba=fat_lba,
            entry_size=entry_size,
            entries=entries,
        )

    def _scan_for_directory(self, byte_off: int, length: int) -> Optional[Partition]:
        blob = self.io.read_at(byte_off, length)
        best: Optional[tuple[int, int, list[DirEntry]]] = None
        for entry_size in (DIR_ENTRY_9200T, DIR_ENTRY_9200C):
            step = entry_size
            i = 0
            while i + entry_size <= len(blob):
                # Cheap reject: name slot should be ASCII.
                name_slot = blob[i + NAME_OFF : i + NAME_OFF + 8]
                if not (32 <= name_slot[:1][0] < 127 if name_slot else False):
                    i += step
                    continue
                window = blob[i : i + min(64 * entry_size, len(blob) - i)]
                entries = parse_directory(window, byte_off + i, entry_size)
                q = directory_quality(entries)
                if q >= 6 and (best is None or q > best[0]):
                    best = (q, byte_off + i, entries)
                    i += max(len(entries), 1) * entry_size
                    continue
                i += step
        if not best:
            return None
        _, off, entries = best
        return Partition(
            index=1,
            start_lba=PART_HEADER_LBA,
            dir_lba=off // SECTOR,
            fat_lba=0,
            entry_size=entries[0].entry_size if entries else DIR_ENTRY_9200T,
            entries=entries,
        )

    def _build_recordings(self) -> list[Recording]:
        by_name = {e.name.lower(): e for e in self.entries}
        recs: dict[int, Recording] = {}
        extras: list[DirEntry] = []
        for e in self.entries:
            num = e.rec_num
            if e.suffix == "av" and (e.is_recording or e.is_buffer) and num is not None:
                recs[num] = Recording(number=num, av=e)
            elif e.suffix == "av" and num is None and e.is_recording:
                extras.append(e)
        for e in self.entries:
            num = e.rec_num
            if num is None or num not in recs:
                continue
            if e.suffix == "elu":
                recs[num].elu = e
            elif e.suffix == "epg":
                recs[num].epg = e
        # Attach leftover uniquely named .av files using their directory index.
        next_num = max(recs, default=0) + 1
        for e in extras:
            recs[next_num] = Recording(number=next_num, av=e)
            next_num += 1
        recordings = [recs[k] for k in sorted(recs)]
        self._apply_metadata(recordings, by_name)
        return recordings

    def _apply_metadata(self, recordings: Sequence[Recording], by_name: dict[str, DirEntry]) -> None:
        record_list = by_name.get("_record_list_")
        backup = by_name.get("_rl_backup_")
        blob = b""
        if record_list and record_list.size:
            blob = self.read_file(record_list)
        elif backup and backup.size:
            blob = self.read_file(backup)
        max_num = max((r.number for r in recordings), default=0)
        for rec in recordings:
            if rec.av.is_buffer:
                continue
            meta = b""
            if rec.epg and rec.epg.size:
                try:
                    meta += self.read_file(rec.epg)
                except OSError:
                    pass
            if blob:
                slice_ = _record_list_slice(blob, rec.number, max_num)
                if slice_:
                    meta += slice_
            _fill_metadata(rec, meta)

    def read_file(self, entry: DirEntry, progress=None) -> bytes:
        """Read a directory entry. Uses start LBA + size (contiguous case)."""
        if entry.size <= 0:
            return b""
        offset = entry.start_lba * SECTOR
        remaining = entry.size
        chunks: list[bytes] = []
        done = 0
        chunk_size = 8 * 1024 * 1024
        while remaining > 0:
            n = min(chunk_size, remaining)
            data = self.io.read_at(offset + done, n)
            if not data:
                break
            chunks.append(data)
            done += len(data)
            remaining -= len(data)
            if progress:
                progress(done, entry.size)
            if len(data) < n:
                break
        return b"".join(chunks)

    def extract_ts(self, rec: Recording) -> bytes:
        data = self.read_file(rec.av)
        if not data:
            return b""
        if needs_bswap(data):
            data = bswap32(data)
        # Trim to whole TS packets when it looks like MPEG-TS.
        if data[:1] == b"\x47" and len(data) >= 188:
            n = (len(data) // 188) * 188
            data = data[:n]
        return data

    def recording(self, number: int) -> Recording:
        for rec in self.recordings:
            if rec.number == number:
                return rec
        raise HumaxError(f"Recording {number} does not exist")


def _record_list_slice(blob: bytes, number: int, max_num: int) -> bytes:
    if number < 1 or not blob:
        return b""
    need = max(number, max_num, 1)
    for size in (0x1000, 0x800, 0x400, 0x200, 0x130, 0x100, 0x80):
        if len(blob) % size == 0 and len(blob) // size >= need:
            off = (number - 1) * size
            if off + size <= len(blob):
                return blob[off : off + size]
    return b""


def _fill_metadata(rec: Recording, meta: bytes) -> None:
    if not meta:
        return
    strings = printable_runs(meta, 5)
    skip = re.compile(
        r"^(\d+\.(av|elu|epg|hre)|_record_list_|_rl_|mpeg|jpeg|eng)$",
        re.I,
    )
    cleaned = []
    for s in strings:
        if skip.match(s) or s.lower() == rec.av.name.lower():
            continue
        if len(s) < 4:
            continue
        cleaned.append(s)
    if cleaned:
        titled = [s for s in cleaned if " " in s]
        rec.title = (titled or cleaned)[0] if titled else max(cleaned, key=len)
        others = [s for s in cleaned if s != rec.title and len(s) <= 40]
        if others:
            rec.channel = min(others, key=len)
    header = meta[:0x80]
    for i in range(0, max(0, len(header) - 3), 4):
        v = u32(header, i)
        if 1104537600 <= v <= 1514764800:
            rec.start = _dt.datetime.fromtimestamp(v, tz=_dt.timezone.utc).replace(tzinfo=None)
            break
    for i in range(0, max(0, len(header) - 3), 4):
        v = u32(header, i)
        if 60 <= v <= 8 * 3600 and v % 30 == 0:
            rec.duration_s = v
            break
    rec.protected = b"protect" in meta.lower() or rec.protected


def extract_one(
    disk: HumaxDisk,
    rec: Recording,
    out_dir: Path,
    sidecar: bool = False,
    overwrite: bool = False,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = rec.output_stem()
    ts_path = out_dir / f"{stem}.ts"
    written: list[Path] = []

    def _write(path: Path, data: bytes) -> None:
        if path.exists() and not overwrite:
            print(f"Skipping file \"{path.name}\", it already exists", file=sys.stderr)
            return
        path.write_bytes(data)
        written.append(path)

    print(f"Copying av file ({human_bytes(rec.av.size)}) -> {ts_path.name}")
    last_pct = [-1]

    def progress(done: int, total: int) -> None:
        if total <= 0:
            return
        pct = done * 100 // total
        if pct != last_pct[0] and (pct % 5 == 0 or done == total):
            last_pct[0] = pct
            print(f"\r  {pct:3d}%", end="", file=sys.stderr, flush=True)

    data = disk.extract_ts(rec)
    if sys.stderr.isatty():
        print("\r     ", end="\r", file=sys.stderr)
    _write(ts_path, data)
    if sidecar:
        if rec.elu:
            print("Copying elu file...")
            _write(out_dir / f"{stem}.elu", disk.read_file(rec.elu))
        if rec.epg:
            print("Copying epg file...")
            _write(out_dir / f"{stem}.epg", disk.read_file(rec.epg))
        hre = Path(f"{stem}.hre")
        # Save the raw directory entry as a stand-in .hre when we have no
        # dedicated record-list slice (still useful as provenance).
        slice_ = rec.av.raw
        _write(out_dir / hre.name, slice_)
    return written


def carve_ts(disk: HumaxDisk, out_dir: Path, min_bytes: int = 1_000_000) -> list[Path]:
    """Last-resort recovery: find MPEG-TS runs in the image."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    step = 1024 * 1024
    size = disk.io.size
    offset = 0
    n = 1
    print("Carving MPEG-TS... this can take a while on large dumps", file=sys.stderr)
    while offset + 188 * 8 < size:
        sample = disk.io.read_at(offset, 188 * 16)
        if not sample:
            break
        use = sample
        swapped = False
        if needs_bswap(sample):
            use = bswap32(sample)
            swapped = True
        if ts_sync_score(use) < 8:
            offset += step
            continue
        # Walk forward while sync holds.
        start = offset
        buf = bytearray()
        pos = offset
        while pos < size:
            chunk = disk.io.read_at(pos, step)
            if not chunk:
                break
            if swapped:
                chunk = bswap32(chunk)
            # Keep only whole packets that still sync.
            good = 0
            for i in range(0, len(chunk) - 187, 188):
                if chunk[i] == 0x47:
                    good += 1
                else:
                    break
            if good < 8 and pos > start:
                break
            take = good * 188
            buf.extend(chunk[:take])
            pos += take if take else step
            if take == 0:
                break
        if len(buf) >= min_bytes:
            path = out_dir / f"recover_{n:04d}.ts"
            path.write_bytes(bytes(buf))
            written.append(path)
            print(f"  carved {path.name} ({human_bytes(len(buf))}) at offset 0x{start:x}")
            n += 1
        offset = max(pos, offset + step)
    return written


def format_info(rec: Recording) -> str:
    lines = [
        f"Rec: {rec.number}",
        rec.list_line(),
        f"  file:   {rec.av.name}",
        f"  size:   {rec.av.size} ({human_bytes(rec.av.size)})",
        f"  lba:    0x{rec.av.start_lba:x}",
        f"  type:   0x{rec.av.type_id:x}",
    ]
    if rec.channel:
        lines.append(f"  channel:{rec.channel}")
    if rec.extra:
        lines.append(f"  {rec.extra}")
    if not rec.title and not rec.channel:
        lines.append("  No further information available for this recording")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="humaxrw",
        description="Read-only 2026 HumaxRW: list and extract recordings "
        "from Humax 9200T/9150T/9300T disk dumps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python3 humaxrw.py -l dump.img
  python3 humaxrw.py -g 1-20,35 -o ./out dump.img
  python3 humaxrw.py -b -o ./out dump.img
  python3 humaxrw.py -r -l dump.img
""",
    )
    p.add_argument("disk", nargs="?", help="disk image or block device")
    p.add_argument("-l", "--list", action="store_true", help="list recordings")
    p.add_argument("-g", "--get", metavar="LIST", help="get recordings (e.g. 1-10,12)")
    p.add_argument("-i", "--info", metavar="LIST", help="show info for recordings")
    p.add_argument("-b", "--backup", action="store_true", help="extract all recordings")
    p.add_argument("-r", "--recover", action="store_true", help="recovery mode (ignore record list)")
    p.add_argument("-n", "--no-info", action="store_true", help="do not parse titles / EPG")
    p.add_argument("-o", "--output", default=".", help="output directory (default: current)")
    p.add_argument("--sidecar", action="store_true", help="also write .elu/.epg/.hre sidecars")
    p.add_argument("--carve", action="store_true", help="carve MPEG-TS if the directory is unusable")
    p.add_argument("--json", action="store_true", dest="as_json", help="JSON listing")
    p.add_argument("--overwrite", action="store_true", help="overwrite existing output files")
    p.add_argument("-v", "--version", action="store_true", help="print version")
    p.add_argument("-y", action="store_true", help="accepted for compatibility (no prompts)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"humaxrw version {VERSION} (read-only reimplementation of xyz321 1.15)")
        return 0

    if not args.disk:
        parser.print_help()
        return 2

    if not any((args.list, args.get, args.info, args.backup, args.carve)):
        args.list = True

    try:
        disk = HumaxDisk(args.disk)
    except HumaxError as e:
        print(e, file=sys.stderr)
        return 1

    with disk:
        recs = disk.recordings
        if args.no_info or args.recover:
            for rec in recs:
                rec.title = rec.title if not args.recover else ""
                if args.recover:
                    rec.channel = ""
                    rec.start = None

        if args.as_json and args.list:
            print(json.dumps([r.to_json() for r in recs], indent=2))
        elif args.list:
            print(f"Humax disk: {disk.path}")
            print(f"Model hint: {disk.model}   partitions: {len(disk.partitions)}")
            if not recs:
                print("Record list is empty")
            for rec in recs:
                if rec.av.is_buffer:
                    print(f"{rec.number:3d}: ***Buffer***  {rec.av.name}  {human_bytes(rec.av.size)}")
                elif args.recover or args.no_info:
                    print(f"{rec.number:3d}: {rec.av.name}  {human_bytes(rec.av.size)}")
                else:
                    print(rec.list_line())
            avs = [r for r in recs if not r.av.is_buffer]
            total = sum(r.av.size for r in avs)
            print(f"{len(avs)} recordings, {human_bytes(total)} of programme data")

        if args.info:
            try:
                nums = parse_list(args.info, max((r.number for r in recs), default=0))
            except ValueError:
                print("Invalid range", file=sys.stderr)
                return 1
            for n in nums:
                try:
                    print(format_info(disk.recording(n)))
                except HumaxError as e:
                    print(e, file=sys.stderr)

        to_get: list[int] = []
        if args.backup:
            to_get = [r.number for r in recs if not r.av.is_buffer]
        elif args.get:
            try:
                to_get = parse_list(args.get, max((r.number for r in recs), default=0))
            except ValueError:
                print("Invalid range", file=sys.stderr)
                return 1

        out_dir = Path(args.output)
        if to_get:
            for n in to_get:
                try:
                    rec = disk.recording(n)
                except HumaxError as e:
                    print(e, file=sys.stderr)
                    continue
                if rec.av.is_buffer and not args.recover:
                    continue
                extract_one(disk, rec, out_dir, sidecar=args.sidecar, overwrite=args.overwrite)

        if args.carve:
            carve_ts(disk, out_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
