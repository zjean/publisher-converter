"""Metafile classification and helper-tool resolution.

`inspect` decides whether the tool warns or stays quiet about lost
artwork, so it governs whether the report can be trusted at all.
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path

from pubidml import metafile

EMF_SIGNATURE = 0x464D4520


def emf(*records: tuple) -> bytes:
    """Build a minimal but structurally valid EMF from (type, size) records."""
    header = bytearray(88)
    struct.pack_into("<II", header, 0, 1, 88)          # EMR_HEADER, header size
    struct.pack_into("<4i", header, 24, 0, 0, 100, 100)  # rclFrame
    struct.pack_into("<I", header, 40, EMF_SIGNATURE)
    struct.pack_into("<I", header, 52, len(records) + 1)  # nRecords
    body = bytearray()
    for record_type, size in records:
        chunk = bytearray(size)
        struct.pack_into("<II", chunk, 0, record_type, size)
        body += chunk
    return bytes(header + body)


PUBLISHER_STUB = emf((14, 20))                    # header + EMR_EOF only
REAL_ARTWORK = emf((54, 40), (55, 40), (14, 20))  # two drawing records + EOF

# WMF function codes used below.
META_EOF = 0x0000
META_SELECTOBJECT = 0x012D
META_DELETEOBJECT = 0x01F0
META_SETWINDOWORG = 0x020B
META_SETWINDOWEXT = 0x020C
META_CREATEPENINDIRECT = 0x02FA
META_CREATEBRUSHINDIRECT = 0x02FC
META_POLYGON = 0x0324
META_POLYPOLYGON = 0x0538

EMR_STRETCHDIBITS = 81


def wmf(*records: tuple, placeable: bool = False) -> bytes:
    """Build a structurally valid WMF from (function, byte size) records.

    Publisher writes both spellings. A placeable WMF carries a 22-byte
    Aldus header with a bounding box in front of the standard 18-byte
    header; a plain one starts at the standard header, which has no
    bounding box anywhere in it.
    """
    body = bytearray()
    for function, size in records:
        chunk = bytearray(size)
        struct.pack_into("<IH", chunk, 0, size // 2, function)
        body += chunk

    header = bytearray(18)
    struct.pack_into("<HHH", header, 0, 1, 9, 0x0300)  # memory metafile, 9 words, 3.0
    struct.pack_into("<I", header, 6, (18 + len(body)) // 2)
    out = bytes(header) + bytes(body)

    if placeable:
        aldus = bytearray(22)
        struct.pack_into("<I", aldus, 0, metafile.WMF_PLACEABLE_KEY)
        struct.pack_into("<4h", aldus, 6, 0, 0, 120, 80)
        out = bytes(aldus) + out
    return out


def dib(width: int, height: int, pixels: bytes, bits: int = 32,
        compression: int = 0) -> bytes:
    """A BITMAPINFOHEADER plus pixel data, as a metafile would carry it."""
    header = bytearray(40)
    struct.pack_into("<IiiHHIIiiII", header, 0,
                     40, width, height, 1, bits, compression,
                     len(pixels), 0, 0, 0, 0)
    return bytes(header) + pixels


def emf_with_dib(payload: bytes, *extra: tuple) -> bytes:
    """An EMF whose only drawing record blits `payload`, plus optional records.

    This is how Publisher stores a pasted photograph: one
    EMR_STRETCHDIBITS wrapping an uncompressed bitmap, nothing else.
    """
    record = bytearray(80)
    struct.pack_into("<II", record, 0, EMR_STRETCHDIBITS, 80 + len(payload))
    struct.pack_into("<IIII", record, 48, 80, 40, 80 + 40, len(payload) - 40)
    blit = bytes(record) + payload

    head = bytearray(88)
    struct.pack_into("<II", head, 0, 1, 88)
    struct.pack_into("<4i", head, 24, 0, 0, 100, 100)
    struct.pack_into("<I", head, 40, EMF_SIGNATURE)
    struct.pack_into("<I", head, 52, 2 + len(extra))

    tail = bytearray()
    for record_type, size in extra:
        chunk = bytearray(size)
        struct.pack_into("<II", chunk, 0, record_type, size)
        tail += chunk
    eof = bytearray(20)
    struct.pack_into("<II", eof, 0, 14, 20)
    return bytes(head) + blit + bytes(tail) + bytes(eof)


def rows_of(png: bytes) -> tuple:
    """Decode a PNG this module produced: (width, height, colour type, rows)."""
    import zlib

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    offset, width, height, colour, data = 8, 0, 0, None, bytearray()
    while offset < len(png):
        length = struct.unpack_from(">I", png, offset)[0]
        tag = png[offset + 4:offset + 8]
        chunk = png[offset + 8:offset + 8 + length]
        if tag == b"IHDR":
            width, height = struct.unpack_from(">II", chunk, 0)
            colour = chunk[9]
        elif tag == b"IDAT":
            data += chunk
        offset += 12 + length

    raw = zlib.decompress(bytes(data))
    channels = 3 if colour == 2 else 4
    stride = width * channels + 1
    rows = [
        tuple(raw[r * stride + 1:(r + 1) * stride])
        for r in range(height)
    ]
    return width, height, colour, rows


class InspectTest(unittest.TestCase):
    def test_publishers_empty_placeholder_is_recognised_as_empty(self):
        info = metafile.inspect(PUBLISHER_STUB)
        self.assertEqual(info.kind, "emf")
        self.assertTrue(info.valid)
        self.assertEqual(info.drawing_records, 0)
        self.assertTrue(info.is_empty)

    def test_real_artwork_is_not_empty(self):
        info = metafile.inspect(REAL_ARTWORK)
        self.assertTrue(info.valid)
        self.assertEqual(info.drawing_records, 2)
        self.assertFalse(info.is_empty)

    def test_state_only_records_do_not_count_as_drawing(self):
        # SAVEDC/SELECTOBJECT/CREATEPEN/RESTOREDC paint nothing; a metafile
        # of nothing but these is still a placeholder.
        info = metafile.inspect(emf((17, 12), (21, 12), (22, 28), (18, 12), (14, 20)))
        self.assertEqual(info.drawing_records, 0)
        self.assertTrue(info.is_empty)

    def test_garbage_is_invalid_and_therefore_empty(self):
        for data in (b"", b"hello", b"\x00" * 200, b"\xff" * 88):
            with self.subTest(length=len(data)):
                info = metafile.inspect(data)
                self.assertFalse(info.valid)
                self.assertTrue(info.is_empty)

    def test_a_zero_size_record_does_not_hang(self):
        # A record claiming size 0 would loop forever without the guard.
        broken = bytearray(emf((54, 40), (14, 20)))
        struct.pack_into("<I", broken, 88 + 4, 0)
        info = metafile.inspect(bytes(broken))
        self.assertTrue(info.valid)

    def test_placeable_wmf_is_detected(self):
        data = struct.pack("<I", metafile.WMF_PLACEABLE_KEY) + bytes(60)
        self.assertEqual(metafile.inspect(data).kind, "wmf")


class StandardWmfTest(unittest.TestCase):
    """A WMF with no Aldus header is still a WMF.

    Publisher writes plenty of them, and one newsletter repeats a single
    logo across 64 frames this way. Read as an EMF they fail the " EMF"
    signature check, come back invalid, and are indistinguishable from an
    empty placeholder -- so real artwork was dropped silently.
    """

    LOGO = wmf(
        (META_SETWINDOWORG, 10),
        (META_SETWINDOWEXT, 10),
        (META_CREATEBRUSHINDIRECT, 14),
        (META_SELECTOBJECT, 8),
        (META_CREATEPENINDIRECT, 18),
        (META_POLYGON, 528),
        (META_POLYPOLYGON, 1336),
        (META_DELETEOBJECT, 8),
        (META_EOF, 6),
    )

    def test_it_is_recognised_as_a_wmf(self):
        info = metafile.inspect(self.LOGO)
        self.assertEqual(info.kind, "wmf")
        self.assertTrue(info.valid)

    def test_its_artwork_is_counted_and_it_is_not_empty(self):
        info = metafile.inspect(self.LOGO)
        self.assertEqual(info.drawing_records, 2)  # the polygon and polypolygon
        self.assertFalse(info.is_empty)

    def test_state_only_records_leave_it_empty(self):
        data = wmf(
            (META_SETWINDOWORG, 10),
            (META_CREATEPENINDIRECT, 18),
            (META_SELECTOBJECT, 8),
            (META_DELETEOBJECT, 8),
            (META_EOF, 6),
        )
        info = metafile.inspect(data)
        self.assertTrue(info.valid)
        self.assertEqual(info.drawing_records, 0)
        self.assertTrue(info.is_empty)

    def test_a_placeable_wmf_counts_the_same_records(self):
        # Same records, 22 bytes further in. Both spellings must agree.
        plain = metafile.inspect(self.LOGO)
        placeable = metafile.inspect(
            wmf(
                (META_SETWINDOWORG, 10), (META_SETWINDOWEXT, 10),
                (META_CREATEBRUSHINDIRECT, 14), (META_SELECTOBJECT, 8),
                (META_CREATEPENINDIRECT, 18), (META_POLYGON, 528),
                (META_POLYPOLYGON, 1336), (META_DELETEOBJECT, 8),
                (META_EOF, 6), placeable=True,
            )
        )
        self.assertEqual(placeable.drawing_records, plain.drawing_records)

    def test_bounds_are_read_only_where_they_exist(self):
        # A standard WMF header has no bounding box; offset 6 holds mtSize,
        # so reading a rectangle there yields nonsense.
        self.assertEqual(
            (metafile.inspect(self.LOGO).width_units,
             metafile.inspect(self.LOGO).height_units),
            (0, 0),
        )
        placeable = metafile.inspect(wmf((META_EOF, 6), placeable=True))
        self.assertEqual((placeable.width_units, placeable.height_units), (120, 80))

    def test_garbage_is_not_mistaken_for_a_wmf(self):
        for data in (b"", b"hi", bytes(18), b"\xff" * 40):
            with self.subTest(length=len(data)):
                self.assertTrue(metafile.inspect(data).is_empty)

    def test_a_zero_size_record_does_not_hang(self):
        broken = bytearray(self.LOGO)
        struct.pack_into("<I", broken, 18, 0)
        self.assertTrue(metafile.inspect(bytes(broken)).valid)


class EmbeddedBitmapTest(unittest.TestCase):
    """Publisher wraps a pasted photograph in a metafile envelope.

    Both real EMFs in the sample set are a single EMR_STRETCHDIBITS around
    an uncompressed bitmap that is 100% of the file. Unwrapping that needs
    no external tool and no resampling.
    """

    def test_a_wrapped_bitmap_becomes_a_png(self):
        pixels = bytes([10, 20, 30, 0] * 4)  # 2x2, 32bpp
        result = metafile.embedded_bitmap(emf_with_dib(dib(2, 2, pixels)))
        self.assertIsNotNone(result)
        payload, mime = result
        self.assertEqual(mime, "image/png")
        self.assertEqual(rows_of(payload)[:2], (2, 2))

    def test_the_undefined_fourth_byte_is_never_treated_as_alpha(self):
        # BI_RGB has no alpha channel, and in the real files every one of
        # those bytes is zero. Read as alpha the photograph disappears.
        pixels = bytes([10, 20, 30, 0] * 4)
        payload, _ = metafile.embedded_bitmap(emf_with_dib(dib(2, 2, pixels)))
        width, height, colour, rows = rows_of(payload)
        self.assertEqual(colour, 2, "must be RGB, not RGBA")
        self.assertEqual(rows[0][:3], (30, 20, 10))  # BGR -> RGB

    def test_bottom_up_rows_are_flipped(self):
        # 1px wide, 2 rows: DIB stores the bottom row first.
        bottom = bytes([1, 1, 1, 0])
        top = bytes([2, 2, 2, 0])
        payload, _ = metafile.embedded_bitmap(emf_with_dib(dib(1, 2, bottom + top)))
        _, _, _, rows = rows_of(payload)
        self.assertEqual(rows[0][:3], (2, 2, 2))
        self.assertEqual(rows[1][:3], (1, 1, 1))

    def test_a_top_down_dib_is_not_flipped(self):
        first = bytes([2, 2, 2, 0])
        second = bytes([1, 1, 1, 0])
        payload, _ = metafile.embedded_bitmap(
            emf_with_dib(dib(1, -2, first + second))
        )
        _, _, _, rows = rows_of(payload)
        self.assertEqual(rows[0][:3], (2, 2, 2))

    def test_twentyfour_bit_bitmaps_work_too(self):
        # 24bpp rows are padded to a 4-byte boundary; 1px needs 1 pad byte.
        pixels = bytes([10, 20, 30, 0])
        payload, _ = metafile.embedded_bitmap(emf_with_dib(dib(1, 1, pixels, bits=24)))
        _, _, colour, rows = rows_of(payload)
        self.assertEqual((colour, rows[0][:3]), (2, (30, 20, 10)))

    def test_a_metafile_that_draws_more_than_the_bitmap_is_declined(self):
        # Unwrapping would silently throw the other artwork away.
        pixels = bytes([10, 20, 30, 0] * 4)
        data = emf_with_dib(dib(2, 2, pixels), (54, 40))  # plus a polygon
        self.assertIsNone(metafile.embedded_bitmap(data))

    def test_compression_we_cannot_read_is_declined(self):
        pixels = bytes([10, 20, 30, 0] * 4)
        for compression in (1, 3, 4, 5):  # RLE8, BITFIELDS, JPEG, PNG
            with self.subTest(compression=compression):
                data = emf_with_dib(dib(2, 2, pixels, compression=compression))
                self.assertIsNone(metafile.embedded_bitmap(data))

    def test_an_unsupported_depth_is_declined(self):
        pixels = bytes([0, 1, 2, 3])
        for bits in (1, 4, 8, 16):
            with self.subTest(bits=bits):
                data = emf_with_dib(dib(2, 2, pixels, bits=bits))
                self.assertIsNone(metafile.embedded_bitmap(data))

    def test_a_stub_has_no_bitmap_to_unwrap(self):
        self.assertIsNone(metafile.embedded_bitmap(PUBLISHER_STUB))
        self.assertIsNone(metafile.embedded_bitmap(REAL_ARTWORK))

    def test_truncated_pixel_data_is_declined_rather_than_padded(self):
        short = dib(4, 4, bytes([1, 2, 3, 0] * 2))  # claims 4x4, carries 2px
        self.assertIsNone(metafile.embedded_bitmap(emf_with_dib(short)))


class ToolResolutionTest(unittest.TestCase):
    """A helper found in the working directory is not a system tool.

    On Windows shutil.which searches the current directory before PATH, so
    an archive shipping its own magick.exe next to the .pub files would get
    it executed by anyone converting in place.
    """

    def test_a_tool_in_the_working_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as work:
            planted = Path(work) / "pub2idml-fake-tool"
            planted.write_text("#!/bin/sh\nexit 0\n")
            planted.chmod(0o755)

            previous = os.getcwd()
            os.chdir(work)
            try:
                # Reachable only because it sits in the working directory.
                self.assertIsNone(metafile._resolve_tool("pub2idml-fake-tool"))
                # And still refused when the working directory is on PATH.
                os.environ["PATH"] = work + os.pathsep + os.environ["PATH"]
                self.assertIsNone(metafile._resolve_tool("pub2idml-fake-tool"))
            finally:
                os.chdir(previous)

    def test_a_real_system_tool_still_resolves(self):
        found = metafile._resolve_tool("sh" if os.name != "nt" else "cmd")
        self.assertIsNotNone(found)
        self.assertTrue(os.path.isabs(found))

    def test_a_missing_tool_is_none(self):
        self.assertIsNone(metafile._resolve_tool("definitely-not-installed-xyzzy"))


class RasterisationTest(unittest.TestCase):
    def test_an_empty_metafile_is_never_sent_to_a_converter(self):
        self.assertIsNone(metafile.to_png(PUBLISHER_STUB, 100.0, 100.0))

    def test_wmf_is_declined(self):
        data = struct.pack("<I", metafile.WMF_PLACEABLE_KEY) + bytes(60)
        self.assertIsNone(metafile.to_png(data, 100.0, 100.0))


if __name__ == "__main__":
    unittest.main()
