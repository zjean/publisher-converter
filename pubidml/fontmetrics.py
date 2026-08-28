"""What a string measures in the font it is set in.

A WordArt headline is stretched to its band, so converting one means
working back from the band to a point size -- and that needs to know how
much of an em the font actually inks and how wide its glyphs actually
are. Those were averages until now (0.70 of an em inked, 0.55 of an em
per glyph, half an em per advance), measured off two rendered headlines
and applied to every font. This reads the font instead.

Standard library only, like the rest of the converter, so the sfnt
tables are read here rather than by a font library: `head` for the em,
`name` for the family, `cmap` for the glyphs a string uses, `hmtx` for
their advances and `glyf` for the box each one inks.
"""
import os
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class FontError(Exception):
    """A font file that cannot be read as far as the metrics need."""


def _tables(buf: bytes, base: int) -> Dict[str, Tuple[int, int]]:
    """Every table's offset and length, by tag.

    Offsets are from the start of the file, not of the font, which is what
    makes a .ttc readable by pointing this at each font's base in turn.
    """
    if len(buf) < base + 12:
        raise FontError("truncated sfnt header")
    count = struct.unpack_from(">H", buf, base + 4)[0]
    if len(buf) < base + 12 + 16 * count:
        raise FontError("truncated table directory")
    found = {}
    for i in range(count):
        tag, _checksum, offset, length = struct.unpack_from(
            ">4sIII", buf, base + 12 + 16 * i
        )
        if offset + length > len(buf):
            raise FontError("table runs past the end of the file")
        found[tag.decode("latin1")] = (offset, length)
    return found


def _read_names(buf: bytes, offset: int) -> Dict[int, str]:
    """nameID to text, preferring the Windows English record.

    Platform 0 (Unicode) and platform 3 (Windows) both hold UTF-16BE;
    only platform 1 (Macintosh) is a byte encoding. Decoding a platform 0
    record as mac-roman is what turns 'Times New Roman' into
    ' T i m e s   N e w   R o m a n', so the platform picks the codec.
    """
    _format, count, storage = struct.unpack_from(">HHH", buf, offset)
    found: Dict[int, str] = {}
    preferred: set = set()
    for i in range(count):
        platform, _encoding, language, name_id, length, at = struct.unpack_from(
            ">6H", buf, offset + 6 + 12 * i
        )
        start = offset + storage + at
        raw = buf[start:start + length]
        if len(raw) != length:
            continue
        try:
            text = raw.decode("mac-roman") if platform == 1 else raw.decode("utf-16-be")
        except (UnicodeDecodeError, ValueError):
            continue
        text = text.replace("\x00", "").strip()
        if not text:
            continue
        windows_english = platform == 3 and language == 0x0409
        if name_id not in found or (windows_english and name_id not in preferred):
            found[name_id] = text
            if windows_english:
                preferred.add(name_id)
    return found


_NAME_FAMILY, _NAME_SUBFAMILY = 1, 2


class Face:
    """One font inside a file: its names, its em, and its tables."""

    def __init__(self, buf: bytes, base: int = 0):
        self.buf = buf
        self.tables = _tables(buf, base)
        for required in ("head", "name", "hhea", "hmtx", "cmap"):
            if required not in self.tables:
                raise FontError(f"no {required} table")
        head = self.tables["head"][0]
        self.upem = struct.unpack_from(">H", buf, head + 18)[0]
        if not self.upem:
            raise FontError("unitsPerEm is zero")
        self.long_loca = struct.unpack_from(">h", buf, head + 50)[0] == 1
        names = _read_names(buf, self.tables["name"][0])
        self.family = names.get(_NAME_FAMILY)
        self.subfamily = names.get(_NAME_SUBFAMILY) or "Regular"
        self._num_h_metrics = struct.unpack_from(
            ">H", buf, self.tables["hhea"][0] + 34
        )[0]
        self._cmap: Optional[Dict[int, int]] = None


def faces(buf: bytes) -> List[Face]:
    """Every font in a file, unpacking a .ttc collection into its members."""
    try:
        if buf[:4] == b"ttcf":
            count = struct.unpack_from(">I", buf, 8)[0]
            return [
                Face(buf, struct.unpack_from(">I", buf, 12 + 4 * i)[0])
                for i in range(count)
            ]
        return [Face(buf, 0)]
    except struct.error as error:
        raise FontError(f"truncated font: {error}") from error
