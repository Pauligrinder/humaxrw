"""Enumerate whole-disk raw devices for HumaxRW (macOS / Linux / Windows)."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class DiskDevice:
    """A physical disk the user can open read-only."""

    path: str
    label: str
    size_bytes: Optional[int] = None

    @property
    def display(self) -> str:
        if self.size_bytes is not None and self.size_bytes > 0:
            size = _human_size(self.size_bytes)
            return f"{self.label}  ({size})  -  {self.path}"
        return f"{self.label}  -  {self.path}"


def _human_size(n: int) -> str:
    for unit, div in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def list_physical_disks() -> list[DiskDevice]:
    """Return whole-disk devices suitable for opening as a Humax source."""
    system = platform.system()
    if system == "Darwin":
        return _list_macos()
    if system == "Linux":
        return _list_linux()
    if system == "Windows":
        return _list_windows()
    return []


def elevation_hint() -> str:
    """Short hint shown when opening a raw device fails with PermissionError."""
    system = platform.system()
    if system == "Darwin":
        return "Opening a raw disk usually needs sudo (e.g. sudo python3 humaxrw_gui.py)."
    if system == "Linux":
        return "Opening a raw disk usually needs root (e.g. sudo python3 humaxrw_gui.py)."
    if system == "Windows":
        return "Opening a physical drive usually needs Administrator rights."
    return "Opening a raw disk may require elevated privileges."


def _list_macos() -> list[DiskDevice]:
    """Prefer /dev/rdiskN whole disks; skip APFS containers and partitions."""
    out: list[DiskDevice] = []
    try:
        plist = subprocess.check_output(
            ["diskutil", "list", "-plist", "physical"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return _list_macos_fallback()

    # Avoid depending on plistlib edge cases: parse disk identifiers from text too.
    try:
        text = subprocess.check_output(
            ["diskutil", "list", "physical"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        text = ""

    # /dev/disk0 (internal, physical):
    for m in re.finditer(r"^/dev/(disk\d+)\s+\(([^)]*)\):", text, re.M):
        disk_id, kind = m.group(1), m.group(2)
        if "virtual" in kind.lower() or "synthesized" in kind.lower():
            continue
        path = f"/dev/r{disk_id}"
        if not Path(path).exists():
            path = f"/dev/{disk_id}"
        size = _macos_disk_size(disk_id)
        name = _macos_disk_name(disk_id) or disk_id
        out.append(DiskDevice(path=path, label=name, size_bytes=size))

    if out:
        return out
    # diskutil physical may be empty on some systems; fall back.
    _ = plist  # kept for future structured parse
    return _list_macos_fallback()


def _list_macos_fallback() -> list[DiskDevice]:
    out: list[DiskDevice] = []
    for entry in sorted(Path("/dev").glob("rdisk[0-9]*")):
        if re.fullmatch(r"rdisk\d+", entry.name):
            disk_id = entry.name[1:]  # diskN
            size = _macos_disk_size(disk_id)
            name = _macos_disk_name(disk_id) or disk_id
            out.append(DiskDevice(path=str(entry), label=name, size_bytes=size))
    return out


def _macos_disk_size(disk_id: str) -> Optional[int]:
    try:
        info = subprocess.check_output(
            ["diskutil", "info", disk_id],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"Disk Size:\s*.*?\((\d+)\s+Bytes\)", info)
    if m:
        return int(m.group(1))
    m = re.search(r"Total Size:\s*.*?\((\d+)\s+Bytes\)", info)
    return int(m.group(1)) if m else None


def _macos_disk_name(disk_id: str) -> Optional[str]:
    try:
        info = subprocess.check_output(
            ["diskutil", "info", disk_id],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    for key in ("Device / Media Name:", "Media Name:", "Volume Name:"):
        m = re.search(rf"{re.escape(key)}\s+(.+)", info)
        if m:
            name = m.group(1).strip()
            if name and name.lower() not in ("not applicable", "none"):
                return name
    return None


def _list_linux() -> list[DiskDevice]:
    """Whole disks under /sys/block (sdX, nvmeXn1, vdX, …), not partitions."""
    out: list[DiskDevice] = []
    sys_block = Path("/sys/block")
    if not sys_block.is_dir():
        return out
    for entry in sorted(sys_block.iterdir()):
        name = entry.name
        if name.startswith(("loop", "ram", "fd", "sr", "dm-", "md", "zram")):
            continue
        # Partitions are not /sys/block entries; skip nvme partition names if present.
        if name.startswith("nvme") and re.search(r"p\d+$", name):
            continue
        path = f"/dev/{name}"
        if not Path(path).exists():
            continue
        size = _linux_block_size(entry)
        model = _linux_model(entry) or name
        out.append(DiskDevice(path=path, label=model, size_bytes=size))
    return out


def _linux_block_size(sys_entry: Path) -> Optional[int]:
    size_file = sys_entry / "size"
    try:
        # Size is in 512-byte sectors.
        sectors = int(size_file.read_text().strip())
        return sectors * 512
    except (OSError, ValueError):
        return None


def _linux_model(sys_entry: Path) -> Optional[str]:
    for rel in ("device/model", "device/name"):
        path = sys_entry / rel
        try:
            text = path.read_text().strip()
            if text:
                return text
        except OSError:
            continue
    return None


def _list_windows() -> list[DiskDevice]:
    """PhysicalDriveN via PowerShell Get-Disk / Get-PhysicalDisk."""
    # Prefer Get-Disk (includes size + friendly name + number).
    ps = (
        "Get-Disk | Select-Object Number, FriendlyName, Size, BusType, PartitionStyle | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            stderr=subprocess.DEVNULL,
            **_win_subprocess_kwargs(),
        )
    except (OSError, subprocess.CalledProcessError):
        return _list_windows_fallback()

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        return _list_windows_fallback()

    # CSV header: "Number","FriendlyName","Size","BusType","PartitionStyle"
    out: list[DiskDevice] = []
    for line in lines[1:]:
        cols = _parse_csv_row(line)
        if len(cols) < 3:
            continue
        try:
            number = int(cols[0])
        except ValueError:
            continue
        name = cols[1] or f"PhysicalDrive{number}"
        try:
            size = int(cols[2]) if cols[2] else None
        except ValueError:
            size = None
        path = rf"\\.\PhysicalDrive{number}"
        label = name
        if len(cols) > 3 and cols[3]:
            label = f"{name} [{cols[3]}]"
        out.append(DiskDevice(path=path, label=label, size_bytes=size))
    return out or _list_windows_fallback()


def _list_windows_fallback() -> list[DiskDevice]:
    """Probe PhysicalDrive0..15 for existence via opening metadata."""
    out: list[DiskDevice] = []
    for n in range(16):
        path = rf"\\.\PhysicalDrive{n}"
        # On Windows, existence is awkward; try a non-destructive open if possible.
        # Without elevation this often fails; still list common indices that wmic knows.
        out.append(DiskDevice(path=path, label=f"PhysicalDrive{n}", size_bytes=None))
    # Trim trailing ones that clearly do not exist via wmic if available.
    try:
        wmic = subprocess.check_output(
            ["wmic", "diskdrive", "get", "Index,Model,Size", "/format:csv"],
            text=True,
            stderr=subprocess.DEVNULL,
            **_win_subprocess_kwargs(),
        )
    except (OSError, subprocess.CalledProcessError):
        return out[:2]  # show a couple of placeholders rather than 16 ghosts

    found: list[DiskDevice] = []
    for line in wmic.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4 or parts[0].lower() == "node":
            continue
        # Node,Index,Model,Size
        try:
            index = int(parts[1])
        except ValueError:
            continue
        model = parts[2] or f"PhysicalDrive{index}"
        try:
            size = int(parts[3]) if parts[3] else None
        except ValueError:
            size = None
        found.append(
            DiskDevice(
                path=rf"\\.\PhysicalDrive{index}",
                label=model,
                size_bytes=size,
            )
        )
    return found or out[:2]


def _parse_csv_row(line: str) -> list[str]:
    """Minimal CSV splitter for PowerShell ConvertTo-Csv output."""
    fields: list[str] = []
    cur: list[str] = []
    in_quotes = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            if in_quotes and i + 1 < len(line) and line[i + 1] == '"':
                cur.append('"')
                i += 2
                continue
            in_quotes = not in_quotes
            i += 1
            continue
        if ch == "," and not in_quotes:
            fields.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def can_open_path(path: str) -> tuple[bool, Optional[str]]:
    """Try a short read-open; return (ok, error_message)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        return False, str(exc)
    try:
        os.close(fd)
    except OSError:
        pass
    return True, None


def _win_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
