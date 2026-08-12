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
from dataclasses import dataclass
from typing import Optional

EMF_SIGNATURE = 0x464D4520  # " EMF"
WMF_PLACEABLE_KEY = 0x9AC6CDD7

EMR_HEADER = 1
EMR_EOF = 14

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


def inspect(data: bytes) -> MetafileInfo:
    """Classify a metafile without invoking any external tool."""
    if len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == WMF_PLACEABLE_KEY:
        return _inspect_wmf(data)
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


def _inspect_wmf(data: bytes) -> MetafileInfo:
    # Placeable WMF: 22-byte aldus header, then the standard WMF header.
    info = MetafileInfo(kind="wmf")
    if len(data) < 40:
        return info
    info.valid = True
    left, top, right, bottom = struct.unpack_from("<4h", data, 6)
    info.width_units = abs(right - left)
    info.height_units = abs(bottom - top)

    offset = 22 + 18
    while offset + 6 <= len(data):
        size_words, function = struct.unpack_from("<IH", data, offset)
        if size_words < 3:
            break
        info.record_count += 1
        if function != 0x0000:  # META_EOF
            info.drawing_records += 1
        else:
            break
        offset += size_words * 2

    return info


def converters_available() -> bool:
    return bool(shutil.which("emf2svg-conv")) and bool(
        shutil.which("magick") or shutil.which("convert")
    )


def to_png(data: bytes, width_pt: float, height_pt: float, dpi: int = 300) -> Optional[bytes]:
    """Rasterise a metafile to PNG, or return None if that is not possible.

    Requires emf2svg-conv and ImageMagick. WMF is not handled: emf2svg-conv
    reads EMF only.
    """
    info = inspect(data)
    if info.kind != "emf" or info.is_empty or not converters_available():
        return None

    magick = shutil.which("magick") or shutil.which("convert")
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
                ["emf2svg-conv", "-i", emf_path, "-o", svg_path],
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
