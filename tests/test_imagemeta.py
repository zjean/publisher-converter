"""Pixel dimensions and stored density, read from image headers.

Wrong numbers here are worse than none: an operator checking whether a
photo will hold up in print reads the effective resolution and nothing
else. Every fixture is built byte by byte so the expected values are
known rather than assumed.
"""

from __future__ import annotations

import struct
import unittest
import zlib

from pubidml import imagemeta


def jpeg(width, height, density=None, units=1):
    out = bytearray(b"\xff\xd8")
    if density:
        segment = b"JFIF\x00" + bytes([1, 2, units]) + struct.pack(">HH", *density) + b"\x00\x00"
        out += b"\xff\xe0" + struct.pack(">H", len(segment) + 2) + segment
    frame = bytes([8]) + struct.pack(">HH", height, width) + bytes([3])
    out += b"\xff\xc0" + struct.pack(">H", len(frame) + 2) + frame
    out += b"\xff\xd9"
    return bytes(out)


def png(width, height, ppm=None):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    if ppm:
        out += chunk(b"pHYs", struct.pack(">IIB", ppm[0], ppm[1], 1))
    out += chunk(b"IDAT", b"\x00")
    out += chunk(b"IEND", b"")
    return out


def gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 3


def bmp(width, height, ppm=None):
    header = bytearray(54)
    header[:2] = b"BM"
    struct.pack_into("<I", header, 14, 40)
    struct.pack_into("<ii", header, 18, width, height)
    if ppm:
        struct.pack_into("<ii", header, 38, ppm[0], ppm[1])
    return bytes(header)


def tiff(width, height, resolution=None, unit=2):
    fields = [(256, 4, width), (257, 4, height)]
    body = b""
    if resolution:
        fields += [(282, 5, None), (283, 5, None), (296, 3, unit)]
    header_len = 8
    ifd_len = 2 + len(fields) * 12 + 4
    rational_at = header_len + ifd_len
    out = bytearray(b"II*\x00" + struct.pack("<I", header_len))
    out += struct.pack("<H", len(fields))
    slot = 0
    for tag, kind, value in fields:
        if kind == 5:
            offset = rational_at + slot * 8
            slot += 1
            out += struct.pack("<HHII", tag, 5, 1, offset)
        elif kind == 3:
            out += struct.pack("<HHIHH", tag, 3, 1, value, 0)
        else:
            out += struct.pack("<HHII", tag, 4, 1, value)
    out += struct.pack("<I", 0)
    if resolution:
        out += struct.pack("<II", resolution[0], 1)
        out += struct.pack("<II", resolution[1], 1)
    return bytes(out)


class DimensionsTest(unittest.TestCase):
    def test_every_supported_format(self):
        cases = [
            ("jpeg", jpeg(4000, 3000), 4000, 3000),
            ("png", png(176, 96), 176, 96),
            ("gif", gif(320, 240), 320, 240),
            ("bmp", bmp(640, 480), 640, 480),
            ("tiff", tiff(800, 600), 800, 600),
        ]
        for label, data, width, height in cases:
            with self.subTest(format=label):
                info = imagemeta.inspect(data)
                self.assertTrue(info.known)
                self.assertEqual((info.width_px, info.height_px), (width, height))

    def test_a_bottom_up_bmp_reports_positive_height(self):
        self.assertEqual(imagemeta.inspect(bmp(64, -32)).height_px, 32)


class DensityTest(unittest.TestCase):
    def test_jfif_density_in_inches(self):
        info = imagemeta.inspect(jpeg(4000, 3000, density=(480, 480), units=1))
        self.assertAlmostEqual(info.ppi_x, 480.0)
        self.assertAlmostEqual(info.ppi_y, 480.0)

    def test_jfif_density_in_centimetres_is_converted(self):
        info = imagemeta.inspect(jpeg(100, 100, density=(100, 100), units=2))
        self.assertAlmostEqual(info.ppi_x, 254.0)

    def test_jfif_density_with_no_units_is_an_aspect_ratio_not_a_resolution(self):
        # units == 0 means the numbers express a pixel aspect ratio; reading
        # them as ppi would invent a resolution the file never claimed.
        info = imagemeta.inspect(jpeg(100, 100, density=(1, 1), units=0))
        self.assertEqual(info.ppi_x, 0.0)

    def test_png_phys_is_converted_from_metres(self):
        info = imagemeta.inspect(png(176, 96, ppm=(3780, 3780)))
        self.assertAlmostEqual(info.ppi_x, 96.012, places=2)

    def test_bmp_pels_per_metre(self):
        info = imagemeta.inspect(bmp(64, 64, ppm=(3780, 3780)))
        self.assertAlmostEqual(info.ppi_x, 96.012, places=2)

    def test_tiff_resolution(self):
        info = imagemeta.inspect(tiff(800, 600, resolution=(300, 300)))
        self.assertAlmostEqual(info.ppi_x, 300.0)

    def test_tiff_resolution_in_centimetres(self):
        info = imagemeta.inspect(tiff(800, 600, resolution=(100, 100), unit=3))
        self.assertAlmostEqual(info.ppi_x, 254.0)

    def test_absent_density_reports_zero_rather_than_a_guess(self):
        for data in (png(10, 10), gif(10, 10), bmp(10, 10), jpeg(10, 10)):
            with self.subTest(fmt=data[:4]):
                self.assertEqual(imagemeta.inspect(data).ppi_x, 0.0)


class RobustnessTest(unittest.TestCase):
    def test_garbage_reports_nothing_rather_than_raising(self):
        for data in (b"", b"hello", b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"\xff" * 200):
            with self.subTest(length=len(data)):
                self.assertFalse(imagemeta.inspect(data).known)

    def test_a_truncated_jpeg_does_not_hang_or_raise(self):
        full = jpeg(4000, 3000, density=(480, 480))
        for cut in range(2, len(full)):
            with self.subTest(cut=cut):
                imagemeta.inspect(full[:cut])

    def test_a_metafile_is_not_mistaken_for_a_raster(self):
        self.assertFalse(imagemeta.inspect(b"\x01\x00\x00\x00" + b"\x00" * 200).known)


class EffectiveResolutionTest(unittest.TestCase):
    """The number an operator actually reads before sending to print."""

    def test_effective_resolution_is_pixels_over_placed_inches(self):
        from pubidml import idml
        # 4000 px placed across 360 pt (5 inches) is 800 ppi.
        attrs = idml._resolution(jpeg(4000, 3000, density=(480, 480)), 360.0, 270.0)
        self.assertEqual(attrs["ActualPpi"], "480 480")
        self.assertEqual(attrs["EffectivePpi"].split()[0], "800")

    def test_effective_is_truthful_even_when_the_file_declares_no_density(self):
        from pubidml import idml
        attrs = idml._resolution(png(600, 600), 72.0, 72.0)
        self.assertEqual(attrs["ActualPpi"], "72 72")
        self.assertEqual(attrs["EffectivePpi"], "600 600")

    def test_a_stretched_picture_reports_each_axis_separately(self):
        from pubidml import idml
        # A 128x128 square in a 222x34 banner is low across, fine down.
        attrs = idml._resolution(jpeg(128, 128), 222.0, 34.0)
        across, down = (int(v) for v in attrs["EffectivePpi"].split())
        self.assertLess(across, 50)
        self.assertGreater(down, 250)

    def test_an_unreadable_image_falls_back_rather_than_failing(self):
        from pubidml import idml
        attrs = idml._resolution(b"not an image", 100.0, 100.0)
        self.assertEqual(attrs, {"ActualPpi": "72 72", "EffectivePpi": "72 72"})


if __name__ == "__main__":
    unittest.main()
