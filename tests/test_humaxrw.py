from __future__ import annotations

import unittest

from struct import pack

from humaxrw import (
    decode_humax_name,
    fat_next_cluster,
    human_size,
    parse_id_list,
    sanitize_filename,
    swap32,
    ts_align,
)


class CodecTests(unittest.TestCase):
    def test_swap32_roundtrip(self):
        raw = bytes.fromhex("652e33330000756c")
        self.assertEqual(swap32(raw), b"33.elu\x00\x00")
        self.assertEqual(swap32(swap32(raw)), raw)

    def test_decode_name_swapped(self):
        raw = bytes.fromhex("652e33330000756c") + b"\x00" * 8
        self.assertEqual(decode_humax_name(raw), "33.elu")
        av = bytes.fromhex("612e333300000076") + b"\x00" * 8
        self.assertEqual(decode_humax_name(av), "33.av")

    def test_decode_name_plain(self):
        raw = b"2.av" + b"\x00" * 12
        self.assertEqual(decode_humax_name(raw), "2.av")

    def test_parse_id_list(self):
        self.assertEqual(parse_id_list("3,5-7,5"), [3, 5, 6, 7])
        self.assertEqual(parse_id_list("10-8"), [8, 9, 10])

    def test_sanitize(self):
        self.assertEqual(sanitize_filename("Vain elämää: Etkot/live"), "Vain elämää_ Etkot_live")

    def test_human_size(self):
        self.assertEqual(human_size(1024), "1.0 KiB")
        self.assertIn("MiB", human_size(5 * 1024 * 1024))

    def test_ts_align(self):
        pkt = b"\x47" + b"\x00" * 187
        blob = b"\x00\x01\x02" + pkt * 4
        off, aligned = ts_align(blob)
        self.assertEqual(off, 3)
        self.assertEqual(aligned[:1], b"\x47")
        self.assertEqual(len(aligned) % 188, 0)

    def test_ts_align_leading_zeros(self):
        pkt = b"\x47" + b"\x00" * 187
        blob = b"\x00" * 78 + pkt * 4
        off, aligned = ts_align(blob)
        self.assertEqual(off, 78)
        self.assertEqual(aligned[:1], b"\x47")

    def test_fat_next_contiguous(self):
        fat = bytearray(64 * 4)
        fat[0:4] = pack("<I", 100)  # free-block count at FAT[0]
        fat[11 * 4 : 12 * 4] = pack("<I", 11)  # cluster 10 -> 11
        fat[12 * 4 : 13 * 4] = pack("<I", 12)
        self.assertEqual(fat_next_cluster(bytes(fat), 10), 11)
        self.assertEqual(fat_next_cluster(bytes(fat), 11), 12)

    def test_fat_next_jump_and_eof(self):
        fat = bytearray(64 * 4)
        fat[11 * 4 : 12 * 4] = pack("<I", 40)
        fat[41 * 4 : 42 * 4] = pack("<I", 0xFFFFFFFE)
        self.assertEqual(fat_next_cluster(bytes(fat), 10), 40)
        self.assertIsNone(fat_next_cluster(bytes(fat), 40))


if __name__ == "__main__":
    unittest.main()
