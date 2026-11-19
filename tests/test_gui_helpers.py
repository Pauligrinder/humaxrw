from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from drives import DiskDevice, _human_size, _parse_csv_row, elevation_hint, list_physical_disks
from humaxrw import DirEntry, Recording, TS_PACKET, extract_recording


class DriveHelperTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(_human_size(500), "500 B")
        self.assertIn("KiB", _human_size(2048))
        self.assertIn("MiB", _human_size(5 * 1024 * 1024))
        self.assertIn("GiB", _human_size(3 * 1024**3))

    def test_disk_device_display(self):
        d = DiskDevice(path="/dev/rdisk2", label="HUMAX", size_bytes=500 * 1024**3)
        text = d.display
        self.assertIn("HUMAX", text)
        self.assertIn("/dev/rdisk2", text)
        self.assertIn("GiB", text)

    def test_parse_csv_row(self):
        self.assertEqual(
            _parse_csv_row('"1","Samsung SSD","1000","SATA","GPT"'),
            ["1", "Samsung SSD", "1000", "SATA", "GPT"],
        )
        self.assertEqual(_parse_csv_row('"a","b ""c"" d","3"'), ["a", 'b "c" d', "3"])

    def test_elevation_hint_nonempty(self):
        hint = elevation_hint()
        self.assertTrue(len(hint) > 10)

    def test_list_physical_disks_returns_list(self):
        disks = list_physical_disks()
        self.assertIsInstance(disks, list)
        for d in disks:
            self.assertIsInstance(d, DiskDevice)
            self.assertTrue(d.path)


class ProgressCallbackTests(unittest.TestCase):
    def test_on_progress_called(self):
        pkt = b"\x47" + b"\x00" * (TS_PACKET - 1)
        payload = pkt * 20
        av = DirEntry(
            name="7.av",
            start_lba=0,
            last_lba=0,
            sectors=1,
            size=len(payload),
            flags=0,
            extra=0,
        )
        rec = Recording(number=7, av=av, title="Test Show")
        disk = MagicMock()
        disk.iter_file_bytes.return_value = iter([payload])

        events: list[tuple[int, int, str]] = []

        def on_progress(written: int, total: int, name: str) -> None:
            events.append((written, total, name))

        with tempfile.TemporaryDirectory() as tmp:
            paths = extract_recording(
                disk,
                rec,
                Path(tmp),
                sidecars=False,
                progress=False,
                on_progress=on_progress,
            )
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].is_file())
            self.assertEqual(paths[0].stat().st_size, len(payload))

        self.assertTrue(events)
        self.assertEqual(events[-1][0], len(payload))
        self.assertEqual(events[-1][1], len(payload))
        self.assertTrue(events[-1][2].endswith(".ts"))


if __name__ == "__main__":
    unittest.main()
