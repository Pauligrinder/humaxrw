from __future__ import annotations

import unittest

from struct import pack

from humaxrw import (
    HRE_SIZE_9200,
    decode_humax_name,
    fat_next_cluster,
    human_size,
    parse_hre_entries,
    parse_id_list,
    parse_size_limit,
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

    def test_ts_align_keeps_trailing_partial(self):
        pkt = b"\x47" + b"\x00" * 187
        partial = b"\x47" + b"\x11" * 50
        blob = b"\x00" * 5 + pkt * 3 + partial
        off, aligned = ts_align(blob)
        self.assertEqual(off, 5)
        self.assertTrue(aligned.endswith(partial))

    def test_ts_align_leading_zeros(self):
        pkt = b"\x47" + b"\x00" * 187
        blob = b"\x00" * 78 + pkt * 4
        off, aligned = ts_align(blob)
        self.assertEqual(off, 78)
        self.assertEqual(aligned[:1], b"\x47")

    def test_parse_size_limit(self):
        self.assertEqual(parse_size_limit(None), 20 * 1024**3)
        self.assertIsNone(parse_size_limit("0"))
        self.assertEqual(parse_size_limit("50G"), 50 * 1024**3)

    def test_parse_hre_entries(self):
        ent = bytearray(HRE_SIZE_9200)
        ent[0x05] = 0x00
        ent[0x06] = 0x31  # number 49
        # start 2010-06-15 18:30 UTC = 1276626600 = 0x4C17B4A8
        ent[0x11:0x15] = bytes.fromhex("4c17b4a8")
        ent[0x15:0x19] = bytes.fromhex("4c17c000")
        ent[0x1C : 0x1C + 11] = b"Vain elamaa"
        ent[0x4D : 0x4D + 4] = b"YLE1"
        parsed = parse_hre_entries(bytes(ent))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].number, 49)
        self.assertEqual(parsed[0].title, "Vain elamaa")
        self.assertEqual(parsed[0].channel, "YLE1")
        self.assertEqual(parsed[0].start_unix, 0x4C17B4A8)

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
