"""Windows metafile (EMF/WMF) inspection and rasterisation.

Publisher embeds clip-art and drawing objects as Windows metafiles, which
have no IDML equivalent. Two things matter here.

First, most of these are empty. Publisher writes a 128-byte EMF stub —
header plus EMR_EOF, `rclBounds` set to the degenerate (0, 0, -1, -1) —
wherever a picture placeholder once sat. Warning about those is crying
wolf: there is no artwork to lose. `inspect` separates the stubs from real
content by counting drawing records, using no external tools.

Second, metafiles that *do* carry artwork can be rasterised if
`emf2svg-conv` and ImageMagick are installed, giving a PNG that IDML can
carry. Both are optional; without them the asset is preserved on disk and
reported rather than silently dropped.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from typing import List, Optional, Tuple

EMF_SIGNATURE = 0x464D4520  # " EMF"
WMF_PLACEABLE_KEY = 0x9AC6CDD7

# A placeable WMF prefixes the standard header with a 22-byte Aldus header;
# only that prefix carries a bounding box.
WMF_ALDUS_HEADER = 22
WMF_HEADER = 18

EMR_HEADER = 1
EMR_EOF = 14
EMR_STRETCHDIBITS = 81

META_EOF = 0x0000

BI_RGB = 0

METAFILE_MIME_TYPES = {
    "image/emf",
    "image/x-emf",
    "image/wmf",
    "image/x-wmf",
    "application/x-msmetafile",
}

# Records that only set up state; a metafile containing nothing else
# paints no pixels.
_NON_DRAWING_RECORDS = {
    EMR_HEADER,
    EMR_EOF,
    9,   # EMR_SETMAPPERFLAGS
    17,  # EMR_SAVEDC
    18,  # EMR_RESTOREDC
    19,  # EMR_SETWORLDTRANSFORM
    20,  # EMR_MODIFYWORLDTRANSFORM
    21,  # EMR_SELECTOBJECT
    22,  # EMR_CREATEPEN
    23,  # EMR_CREATEBRUSHINDIRECT
    24,  # EMR_DELETEOBJECT
    33,  # EMR_SETMAPMODE
}


@dataclass
class MetafileInfo:
    valid: bool = False
    kind: str = "unknown"  # "emf" | "wmf"
    record_count: int = 0
    drawing_records: int = 0
    width_units: int = 0
    height_units: int = 0

    @property
    def is_empty(self) -> bool:
        """True when the metafile paints nothing."""
        return not self.valid or self.drawing_records == 0


# WMF records that only set up state. As on the EMF side this is an
# allowlist, so an unrecognised record counts as drawing: mistaking artwork
# for a placeholder loses it silently, while the reverse only warns.
_WMF_NON_DRAWING_RECORDS = {
    META_EOF,
    0x001E,  # META_SAVEDC
    0x0035,  # META_REALIZEPALETTE
    0x00F7,  # META_CREATEPALETTE
    0x0102,  # META_SETBKMODE
    0x0103,  # META_SETMAPMODE
    0x0104,  # META_SETROP2
    0x0106,  # META_SETPOLYFILLMODE
    0x0107,  # META_SETSTRETCHBLTMODE
    0x0108,  # META_SETTEXTCHAREXTRA
    0x0127,  # META_RESTOREDC
    0x012C,  # META_SELECTCLIPREGION
    0x012D,  # META_SELECTOBJECT
    0x012E,  # META_SETTEXTALIGN
    0x01F0,  # META_DELETEOBJECT
    0x01F9,  # META_CREATEPATTERNBRUSH
    0x0201,  # META_SETBKCOLOR
    0x0209,  # META_SETTEXTCOLOR
    0x020B,  # META_SETWINDOWORG
    0x020C,  # META_SETWINDOWEXT
    0x020D,  # META_SETVIEWPORTORG
    0x020E,  # META_SETVIEWPORTEXT
    0x020F,  # META_OFFSETWINDOWORG
    0x0211,  # META_OFFSETVIEWPORTORG
    0x0214,  # META_MOVETO
    0x0220,  # META_OFFSETCLIPRGN
    0x0231,  # META_SETMAPPERFLAGS
    0x0234,  # META_SELECTPALETTE
    0x02FA,  # META_CREATEPENINDIRECT
    0x02FB,  # META_CREATEFONTINDIRECT
    0x02FC,  # META_CREATEBRUSHINDIRECT
    0x0410,  # META_SCALEWINDOWEXT
    0x0412,  # META_SCALEVIEWPORTEXT
    0x0416,  # META_INTERSECTCLIPRECT
    0x0626,  # META_ESCAPE
    0x06FF,  # META_CREATEREGION
}


def _is_standard_wmf(data: bytes) -> bool:
    """True for a WMF carrying no Aldus header.

    Publisher writes these freely and they have no signature to match on,
    only a header shaped a particular way: a type of 1 or 2, a header size
    of exactly 9 words, and a 1.0 or 3.0 version. An EMF cannot be confused
    with one -- its leading record type is the 32-bit value 1, which puts a
    zero where the header size would be.
    """
    if len(data) < WMF_HEADER:
        return False
    kind, header_words, version = struct.unpack_from("<HHH", data, 0)
    return kind in (1, 2) and header_words == 9 and version in (0x0100, 0x0300)


def inspect(data: bytes) -> MetafileInfo:
    """Classify a metafile without invoking any external tool."""
    if len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == WMF_PLACEABLE_KEY:
        return _inspect_wmf(data, placeable=True)
    if _is_standard_wmf(data):
        return _inspect_wmf(data, placeable=False)
    return _inspect_emf(data)


def _inspect_emf(data: bytes) -> MetafileInfo:
    info = MetafileInfo(kind="emf")
    if len(data) < 88:
        return info

    record_type, header_size = struct.unpack_from("<II", data, 0)
    signature = struct.unpack_from("<I", data, 40)[0]
    if record_type != EMR_HEADER or signature != EMF_SIGNATURE:
        return info

    info.valid = True
    info.record_count = struct.unpack_from("<I", data, 52)[0]
    frame = struct.unpack_from("<4i", data, 24)
    info.width_units = max(0, frame[2] - frame[0])
    info.height_units = max(0, frame[3] - frame[1])

    offset = header_size
    while offset + 8 <= len(data):
        kind, size = struct.unpack_from("<II", data, offset)
        if size < 8:
            break
        if kind not in _NON_DRAWING_RECORDS:
            info.drawing_records += 1
        if kind == EMR_EOF:
            break
        offset += size

    return info


def _inspect_wmf(data: bytes, placeable: bool) -> MetafileInfo:
    info = MetafileInfo(kind="wmf")
    offset = (WMF_ALDUS_HEADER if placeable else 0) + WMF_HEADER
    if len(data) < offset:
        return info
    info.valid = True

    # Only the Aldus header has a bounding box. The standard header holds
    # mtSize where a rectangle would be, so reading one there is nonsense;
    # the frame the picture is placed in supplies the geometry anyway.
    if placeable:
        left, top, right, bottom = struct.unpack_from("<4h", data, 6)
        info.width_units = abs(right - left)
        info.height_units = abs(bottom - top)

    while offset + 6 <= len(data):
        size_words, function = struct.unpack_from("<IH", data, offset)
        if size_words < 3:
            break
        info.record_count += 1
        if function == META_EOF:
            break
        if function not in _WMF_NON_DRAWING_RECORDS:
            info.drawing_records += 1
        offset += size_words * 2

    return info


def _emf_records(data: bytes):
    """Yield (offset, type, size) for each EMF record after the header."""
    header_size = struct.unpack_from("<I", data, 4)[0]
    offset = header_size
    while offset + 8 <= len(data):
        kind, size = struct.unpack_from("<II", data, offset)
        if size < 8:
            return
        yield offset, kind, size
        if kind == EMR_EOF:
            return
        offset += size


def embedded_bitmap(data: bytes) -> Optional[Tuple[bytes, str]]:
    """Unwrap a metafile that only wraps a bitmap, as (payload, mime type).

    Pasting a photograph into Publisher stores it as a metafile whose sole
    drawing record blits a bitmap -- both real EMFs in the sample set are
    one EMR_STRETCHDIBITS around an uncompressed DIB that accounts for
    100% of the file. That needs no external tool and no resampling, where
    the emf2svg route rewrites a 509 ppi photograph through SVG.

    Returns None whenever the file is anything more than a bitmap in an
    envelope, so real drawing records are never quietly discarded.
    """
    info = inspect(data)
    if info.kind != "emf" or not info.valid or info.drawing_records != 1:
        return None

    for offset, kind, size in _emf_records(data):
        if kind in _NON_DRAWING_RECORDS:
            continue
        if kind != EMR_STRETCHDIBITS or offset + 64 > len(data):
            return None

        header_at, header_bytes, pixels_at, pixel_bytes = struct.unpack_from(
            "<IIII", data, offset + 48
        )
        if header_bytes < 40 or pixel_bytes <= 0:
            return None
        header_start, pixel_start = offset + header_at, offset + pixels_at
        if (header_start + header_bytes > min(len(data), offset + size)
                or pixel_start + pixel_bytes > min(len(data), offset + size)):
            return None

        png = _dib_to_png(
            data[header_start:header_start + header_bytes],
            data[pixel_start:pixel_start + pixel_bytes],
        )
        return (png, "image/png") if png else None

    return None


def _dib_to_png(header: bytes, pixels: bytes) -> Optional[bytes]:
    """Re-encode an uncompressed BGR(X) device-independent bitmap as a PNG.

    Only BI_RGB at 24 or 32 bits is handled; anything else returns None and
    leaves the caller to try an external converter.

    The fourth byte of a 32-bit BI_RGB pixel is explicitly undefined -- it
    is *not* an alpha channel, and in the sample files every one of them is
    zero. Read as alpha the whole photograph would come out transparent, so
    it is dropped and the output is plain RGB.
    """
    if len(header) < 40:
        return None
    width, height, _planes, bits, compression = struct.unpack_from("<iiHHI", header, 4)
    if compression != BI_RGB or bits not in (24, 32) or width <= 0 or height == 0:
        return None

    step = bits // 8
    stride = ((bits * width + 31) // 32) * 4
    rows = abs(height)
    if len(pixels) < stride * rows:
        return None

    scanlines: List[bytes] = []
    for row in range(rows):
        # A positive height means the rows are stored bottom-up.
        source = (rows - 1 - row) if height > 0 else row
        start = source * stride
        line = bytearray(width * 3)
        for column in range(width):
            blue, green, red = pixels[start:start + 3]
            line[column * 3:column * 3 + 3] = bytes((red, green, blue))
            start += step
        scanlines.append(bytes(line))

    x_per_metre, y_per_metre = struct.unpack_from("<ii", header, 24)
    return _encode_png(width, rows, scanlines, x_per_metre, y_per_metre)


def _encode_png(
    width: int, height: int, scanlines: List[bytes],
    x_per_metre: int = 0, y_per_metre: int = 0,
) -> bytes:
    """Write 8-bit RGB scanlines as a PNG, using only the standard library."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    parts = [
        b"\x89PNG\r\n\x1a\n",
        # bit depth 8, colour type 2 (RGB), no interlace
        chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
    ]
    # Carry the bitmap's own resolution so the links panel reports what the
    # picture really is rather than a flat 72 ppi.
    if x_per_metre > 0 and y_per_metre > 0:
        parts.append(
            chunk(b"pHYs", struct.pack(">IIB", x_per_metre, y_per_metre, 1))
        )
    raw = b"".join(b"\x00" + line for line in scanlines)
    parts.append(chunk(b"IDAT", zlib.compress(raw, 6)))
    parts.append(chunk(b"IEND", b""))
    return b"".join(parts)


def _resolve_tool(name: str) -> Optional[str]:
    """Locate a helper binary, refusing one that sits in the working directory.

    On Windows shutil.which prepends the current directory to the search
    path (via NeedCurrentDirectoryForExePath), and passing an explicit
    `path=` does not suppress that — the insert happens either way. Since
    the documented workflow is to cd into a folder of .pub files and
    convert in place, an archive that shipped its own magick.exe alongside
    the documents would get that binary executed. A helper found in the
    working directory is not a system tool, so it is refused rather than
    run; the caller then reports the artwork as unconvertible.
    """
    found = shutil.which(name)
    if not found:
        return None
    found = os.path.abspath(found)
    # Compare resolved directories, not the raw strings: /var is a symlink
    # to /private/var on macOS, so the two spellings of one directory would
    # otherwise not match. The *directory* is resolved rather than the file,
    # because a planted symlink pointing at some other binary is still a
    # binary the working directory chose.
    here = os.path.realpath(os.getcwd())
    if os.path.normcase(os.path.realpath(os.path.dirname(found))) == os.path.normcase(here):
        return None
    return found


def _magick() -> Optional[str]:
    return _resolve_tool("magick") or _resolve_tool("convert")


def converters_available() -> bool:
    return bool(_resolve_tool("emf2svg-conv")) and bool(_magick())


def to_png(data: bytes, width_pt: float, height_pt: float, dpi: int = 300) -> Optional[bytes]:
    """Rasterise a metafile to PNG, or return None if that is not possible.

    Requires emf2svg-conv and ImageMagick. WMF is not handled: emf2svg-conv
    reads EMF only.
    """
    info = inspect(data)
    if info.kind != "emf" or info.is_empty:
        return None

    emf2svg = _resolve_tool("emf2svg-conv")
    magick = _magick()
    if not emf2svg or not magick:
        return None

    width_px = max(1, min(10000, round(width_pt / 72.0 * dpi))) if width_pt else 1000
    height_px = max(1, min(10000, round(height_pt / 72.0 * dpi))) if height_pt else 1000

    with tempfile.TemporaryDirectory() as work:
        emf_path = os.path.join(work, "in.emf")
        svg_path = os.path.join(work, "out.svg")
        png_path = os.path.join(work, "out.png")

        with open(emf_path, "wb") as handle:
            handle.write(data)

        try:
            step = subprocess.run(
                [emf2svg, "-i", emf_path, "-o", svg_path],
                capture_output=True,
                timeout=60,
            )
            if step.returncode != 0 or not os.path.exists(svg_path):
                return None

            step = subprocess.run(
                [
                    magick,
                    "-background", "none",
                    "-density", str(dpi),
                    svg_path,
                    "-resize", f"{width_px}x{height_px}",
                    png_path,
                ],
                capture_output=True,
                timeout=120,
            )
            if step.returncode != 0 or not os.path.exists(png_path):
                return None

            with open(png_path, "rb") as handle:
                return handle.read()
        except (subprocess.TimeoutExpired, OSError):
            return None
