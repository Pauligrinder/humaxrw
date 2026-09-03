from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import humaxrw as hr  # noqa: E402


def put_u32(buf: bytearray, off: int, value: int) -> None:
    buf[off : off + 4] = int(value).to_bytes(4, "little")


def make_ts_packet(pid: int = 0x100, payload: bytes = b"HELLOHUMX") -> bytes:
    pkt = bytearray(188)
    pkt[0] = 0x47
    pkt[1] = (pid >> 8) & 0x1F
    pkt[2] = pid & 0xFF
    pkt[3] = 0x10
    pkt[4 : 4 + len(payload)] = payload
    return bytes(pkt)


def dir_entry(
    name: str,
    start_lba: int,
    size: int,
    type_id: int = hr.TYPE_RECORDING,
    alloc: int | None = None,
) -> bytes:
    raw = bytearray(hr.DIR_ENTRY_9200T)
    put_u32(raw, 0x0C, start_lba)
    put_u32(raw, 0x14, start_lba + max(size // hr.SECTOR, 1))
    put_u32(raw, 0x1C, alloc if alloc is not None else (size + 511) // 512)
    put_u32(raw, 0x24, size)
    put_u32(raw, 0x28, type_id)
    encoded = name.encode("ascii") + b"\x00"
    raw[0x40 : 0x40 + len(encoded)] = encoded
    return bytes(raw)


def build_image(path: Path) -> dict:
    """Build a tiny but structurally valid 9200T-style dump."""
    size = 4 * 1024 * 1024
    img = bytearray(size)

    img[hr.MBR_SIG_OFF : hr.MBR_SIG_OFF + 2] = b"\xAA\x55"
    img[0x1CC:0x1D0] = (16).to_bytes(4, "little")  # partition hint

    dir_lba = 0x20
    fat_lba = 0x40
    data_lba = 0x100
    rl_lba = 0x80

    header = bytearray(hr.SECTOR)
    put_u32(header, hr.HEADER_DIR_OFF, dir_lba)
    put_u32(header, hr.HEADER_FAT_OFF, fat_lba)
    img[16 * hr.SECTOR : 16 * hr.SECTOR + hr.SECTOR] = header

    packets = [make_ts_packet(payload=f"PKT{i:04d}".encode()) for i in range(20)]
    ts = b"".join(packets)
    stored = hr.bswap32(ts)
    img[data_lba * hr.SECTOR : data_lba * hr.SECTOR + len(stored)] = stored

    title = b"Sunday Grandstand"
    # Slot index is recording_number - 1 (2.av -> slot 1).
    record_list = bytearray(0x200 * 3)
    slot = memoryview(record_list)[0x200 * 1 : 0x200 * 2]
    put_u32(slot, 0x10, 1286668800)  # 2010-10-10 00:00:00 UTC
    put_u32(slot, 0x20, 3600)  # 1 hour
    slot[0x40 : 0x40 + len(title)] = title
    slot[0x70 : 0x70 + 7] = b"BBC One"
    img[rl_lba * hr.SECTOR : rl_lba * hr.SECTOR + len(record_list)] = record_list

    entries = [
        dir_entry("0.av", 0x90, 1024, hr.TYPE_BUFFER),
        dir_entry("1.av", 0x91, 1024, hr.TYPE_BUFFER),
        dir_entry("2.av", data_lba, len(stored), hr.TYPE_RECORDING),
        dir_entry("2.elu", 0xA0, 64, 0),
        dir_entry("_RECORD_LIST_", rl_lba, 0x200 * 3, 0),
    ]
    blob = b"".join(entries)
    img[dir_lba * hr.SECTOR : dir_lba * hr.SECTOR + len(blob)] = blob

    path.write_bytes(bytes(img))
    return {"ts": ts, "stored": stored, "title": title.decode()}


class ParseListTests(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(hr.parse_list("10-12,14", 20), [10, 11, 12, 14])
        self.assertEqual(hr.parse_list("all", 3), [1, 2, 3])
        self.assertEqual(hr.parse_list("2-1", 5), [1, 2])


class SwapTests(unittest.TestCase):
    def test_roundtrip(self):
        data = bytes(range(16))
        self.assertEqual(hr.bswap32(hr.bswap32(data)), data)

    def test_detects_swapped_ts(self):
        ts = b"".join(make_ts_packet() for _ in range(12))
        self.assertFalse(hr.needs_bswap(ts))
        self.assertTrue(hr.needs_bswap(hr.bswap32(ts)))


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = Path(self.tmp.name) / "humax.img"
        self.meta = build_image(self.img)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_and_extract(self):
        with hr.HumaxDisk(self.img) as disk:
            nums = [r.number for r in disk.recordings]
            self.assertIn(2, nums)
            rec = disk.recording(2)
            self.assertEqual(rec.av.name, "2.av")
            self.assertEqual(rec.title, "Sunday Grandstand")
            data = disk.extract_ts(rec)
            self.assertEqual(data, self.meta["ts"])
            self.assertEqual(data[0], 0x47)
            self.assertEqual(data[188], 0x47)

    def test_cli_list_and_get(self):
        out = Path(self.tmp.name) / "out"
        rc = hr.main(["-l", str(self.img)])
        self.assertEqual(rc, 0)
        rc = hr.main(["-g", "2", "-o", str(out), str(self.img)])
        self.assertEqual(rc, 0)
        files = list(out.glob("*.ts"))
        self.assertEqual(len(files), 1)
        payload = files[0].read_bytes()
        self.assertEqual(payload[0], 0x47)
        self.assertEqual(payload, self.meta["ts"])
        self.assertIn("Sunday Grandstand", files[0].name)

    def test_json_list(self):
        rc = hr.main(["-l", "--json", str(self.img)])
        self.assertEqual(rc, 0)

    def test_missing_file(self):
        rc = hr.main(["-l", str(Path(self.tmp.name) / "nope.img")])
        self.assertEqual(rc, 1)


class FilenameTests(unittest.TestCase):
    def test_safe(self):
        self.assertEqual(hr.safe_filename("A/B:C", "x"), "A_B_C")


if __name__ == "__main__":
    unittest.main()
