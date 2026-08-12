"""Pixel dimensions and stored resolution, read from the image bytes.

IDML records two resolutions per placed picture. `ActualPpi` is what the
file itself claims — a scan saved at 300 dpi says 300 — and `EffectivePpi`
is what it works out to once placed at a given size on the page. Those two
are what tell an operator whether the artwork will hold up in print: a
photo at 42 effective ppi is going to look soft however good the original
was.

Publisher does not report either, and libmspub passes on only the pixels,
so both are derived here: the dimensions and the stored density come out
of the image's own header. Only the formats IDML can carry are handled;
anything unrecognised reports nothing rather than a plausible guess, and
the writer then falls back to a bare 72.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

# JPEG start-of-frame markers carrying dimensions. The arithmetic and
# progressive variants differ only in entropy coding; the frame header is
# the same shape. DHT/DAC/RST/SOI/EOI are excluded deliberately.
_JPEG_SOF = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

_INCH_PER_METRE = 0.0254


@dataclass
class ImageInfo:
    width_px: int = 0
    height_px: int = 0
    # Resolution the file declares, in pixels per inch; 0.0 when it
    # declares none, which is the common case for web-sourced artwork.
    ppi_x: float = 0.0
    ppi_y: float = 0.0

    @property
    def known(self) -> bool:
        return self.width_px > 0 and self.height_px > 0


def inspect(data: bytes) -> ImageInfo:
    """Read dimensions and stored density, or return an empty ImageInfo."""
    try:
        if data[:2] == b"\xff\xd8":
            return _jpeg(data)
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return _png(data)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return _gif(data)
        if data[:2] == b"BM":
            return _bmp(data)
        if data[:4] in (b"II*\x00", b"MM\x00*"):
            return _tiff(data)
    except (struct.error, IndexError, ValueError, ZeroDivisionError):
        # A malformed header is not worth failing a conversion over; the
        # picture still places, it just carries no resolution metadata.
        return ImageInfo()
    return ImageInfo()


def _jpeg(data: bytes) -> ImageInfo:
    info = ImageInfo()
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker == 0xD9:
            break
        length = struct.unpack_from(">H", data, offset + 2)[0]
        segment = data[offset + 4: offset + 2 + length]

        if marker in _JPEG_SOF and len(segment) >= 5:
            info.height_px, info.width_px = struct.unpack_from(">HH", segment, 1)
            # Dimensions are the goal; density may already have been read
            # from an earlier APP segment, which always precedes the frame.
            if info.ppi_x:
                return info
        elif marker == 0xE0 and segment[:5] == b"JFIF\x00" and not info.ppi_x:
            units, x_density, y_density = struct.unpack_from(">BHH", segment, 7)
            if x_density and y_density:
                if units == 1:                      # already per inch
                    info.ppi_x, info.ppi_y = float(x_density), float(y_density)
                elif units == 2:                    # per centimetre
                    info.ppi_x, info.ppi_y = x_density * 2.54, y_density * 2.54
        elif marker == 0xE1 and segment[:6] == b"Exif\x00\x00" and not info.ppi_x:
            exif = _tiff(segment[6:])
            info.ppi_x, info.ppi_y = exif.ppi_x, exif.ppi_y

        offset += 2 + length
    return info


def _png(data: bytes) -> ImageInfo:
    info = ImageInfo()
    info.width_px, info.height_px = struct.unpack_from(">II", data, 16)
    offset = 8
    while offset + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, offset)
        if kind == b"pHYs" and offset + 8 + 9 <= len(data):
            x_ppm, y_ppm, unit = struct.unpack_from(">IIB", data, offset + 8)
            if unit == 1 and x_ppm and y_ppm:       # 1 means metres
                info.ppi_x = x_ppm * _INCH_PER_METRE
                info.ppi_y = y_ppm * _INCH_PER_METRE
            break
        if kind in (b"IDAT", b"IEND"):
            break
        offset += 12 + length
    return info


def _gif(data: bytes) -> ImageInfo:
    width, height = struct.unpack_from("<HH", data, 6)
    return ImageInfo(width_px=width, height_px=height)


def _bmp(data: bytes) -> ImageInfo:
    header_size = struct.unpack_from("<I", data, 14)[0]
    if header_size < 40:                            # BITMAPCOREHEADER
        width, height = struct.unpack_from("<hh", data, 18)
        return ImageInfo(width_px=width, height_px=abs(height))
    width, height = struct.unpack_from("<ii", data, 18)
    info = ImageInfo(width_px=width, height_px=abs(height))
    x_ppm, y_ppm = struct.unpack_from("<ii", data, 38)
    if x_ppm > 0 and y_ppm > 0:
        info.ppi_x = x_ppm * _INCH_PER_METRE
        info.ppi_y = y_ppm * _INCH_PER_METRE
    return info


def _tiff(data: bytes) -> ImageInfo:
    endian = "<" if data[:2] == b"II" else ">"
    first_ifd = struct.unpack_from(endian + "I", data, 4)[0]
    if first_ifd + 2 > len(data):
        return ImageInfo()

    count = struct.unpack_from(endian + "H", data, first_ifd)[0]
    fields = {}
    for index in range(count):
        entry = first_ifd + 2 + index * 12
        if entry + 12 > len(data):
            break
        tag, kind, _n = struct.unpack_from(endian + "HHI", data, entry)
        if kind in (3, 4):                          # SHORT / LONG, inline
            fields[tag] = struct.unpack_from(
                endian + ("H" if kind == 3 else "I"), data, entry + 8
            )[0]
        elif kind == 5:                             # RATIONAL, by offset
            at = struct.unpack_from(endian + "I", data, entry + 8)[0]
            if at + 8 <= len(data):
                num, den = struct.unpack_from(endian + "II", data, at)
                fields[tag] = num / den if den else 0.0

    info = ImageInfo(
        width_px=int(fields.get(256, 0)), height_px=int(fields.get(257, 0))
    )
    unit = fields.get(296, 2)                       # 2 = inch, 3 = centimetre
    x_res, y_res = fields.get(282, 0.0), fields.get(283, 0.0)
    if x_res and y_res:
        scale = 2.54 if unit == 3 else 1.0
        info.ppi_x, info.ppi_y = x_res * scale, y_res * scale
    return info
