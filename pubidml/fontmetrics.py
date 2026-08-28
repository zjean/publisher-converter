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
from dataclasses import dataclass
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


def _cmap_format_4(buf: bytes, at: int) -> Dict[int, int]:
    segments = struct.unpack_from(">H", buf, at + 6)[0] // 2
    ends = at + 14
    starts = ends + segments * 2 + 2
    deltas = starts + segments * 2
    ranges = deltas + segments * 2
    table: Dict[int, int] = {}
    for i in range(segments):
        end = struct.unpack_from(">H", buf, ends + 2 * i)[0]
        start = struct.unpack_from(">H", buf, starts + 2 * i)[0]
        delta = struct.unpack_from(">h", buf, deltas + 2 * i)[0]
        offset = struct.unpack_from(">H", buf, ranges + 2 * i)[0]
        if start == 0xFFFF:
            continue
        for code in range(start, min(end, 0xFFFE) + 1):
            if offset == 0:
                glyph = (code + delta) & 0xFFFF
            else:
                at_glyph = ranges + 2 * i + offset + 2 * (code - start)
                if at_glyph + 2 > len(buf):
                    continue
                glyph = struct.unpack_from(">H", buf, at_glyph)[0]
                if glyph:
                    glyph = (glyph + delta) & 0xFFFF
            if glyph:
                table[code] = glyph
    return table


def _cmap_format_12(buf: bytes, at: int) -> Dict[int, int]:
    count = struct.unpack_from(">I", buf, at + 12)[0]
    table: Dict[int, int] = {}
    for i in range(count):
        start, end, glyph = struct.unpack_from(">III", buf, at + 16 + 12 * i)
        # A group covering the whole plane is a corrupt font, not a font
        # with a million glyphs; expanding it would hang the converter.
        if end - start > 0x10000:
            continue
        for code in range(start, end + 1):
            table[code] = glyph + (code - start)
    return table


def _cmap_format_6(buf: bytes, at: int) -> Dict[int, int]:
    first, count = struct.unpack_from(">HH", buf, at + 6)
    table = {}
    for i in range(count):
        glyph = struct.unpack_from(">H", buf, at + 10 + 2 * i)[0]
        if glyph:
            table[first + i] = glyph
    return table


_CMAP_READERS = {4: _cmap_format_4, 12: _cmap_format_12, 6: _cmap_format_6}
# Which subtable to prefer. A font may carry several, and the first one
# listed is not always the one with the coverage: the pick is by
# preference, and a subtable that reads as empty falls through to the next.
_CMAP_PREFERENCE = ((3, 10), (0, 4), (0, 6), (3, 1), (0, 3), (0, 1), (0, 0), (3, 0))


def _cmap_lookup(buf: bytes, offset: int) -> Dict[int, int]:
    count = struct.unpack_from(">H", buf, offset + 2)[0]
    subtables: Dict[tuple, int] = {}
    for i in range(count):
        platform, encoding, at = struct.unpack_from(">HHI", buf, offset + 4 + 8 * i)
        subtables.setdefault((platform, encoding), offset + at)
    order = [key for key in _CMAP_PREFERENCE if key in subtables]
    order += [key for key in subtables if key not in _CMAP_PREFERENCE]
    for key in order:
        at = subtables[key]
        if at + 4 > len(buf):
            continue
        reader = _CMAP_READERS.get(struct.unpack_from(">H", buf, at)[0])
        if reader is None:
            continue
        try:
            table = reader(buf, at)
        except struct.error:
            continue
        if table:
            return table
    raise FontError("no readable unicode cmap")


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

    @property
    def cmap(self) -> Dict[int, int]:
        if self._cmap is None:
            self._cmap = _cmap_lookup(self.buf, self.tables["cmap"][0])
        return self._cmap

    def advance(self, glyph: int) -> int:
        """One glyph's advance width, in font units.

        `hmtx` states the last advance once and lets every glyph after it
        share it, which is how a monospaced tail is stored, so a glyph past
        the end of the metrics array takes the last one rather than none.
        """
        offset, length = self.tables["hmtx"]
        index = min(glyph, max(self._num_h_metrics - 1, 0))
        at = offset + 4 * index
        if at + 2 > offset + length:
            return 0
        return struct.unpack_from(">H", self.buf, at)[0]

    def bbox(self, glyph: int) -> Optional[Tuple[int, int, int, int]]:
        """The box one glyph inks, or None for a glyph that inks nothing.

        A composite glyph states its own box in the same header as a simple
        one, so nothing here has to follow the components.
        """
        if "glyf" not in self.tables or "loca" not in self.tables:
            return None
        loca = self.tables["loca"][0]
        try:
            if self.long_loca:
                start, end = struct.unpack_from(">II", self.buf, loca + 4 * glyph)
            else:
                short_start, short_end = struct.unpack_from(
                    ">HH", self.buf, loca + 2 * glyph
                )
                start, end = short_start * 2, short_end * 2
        except struct.error:
            return None
        if end <= start:
            return None
        at = self.tables["glyf"][0] + start
        try:
            return struct.unpack_from(">hhhh", self.buf, at + 2)
        except struct.error:
            return None

    def measure(self, text: str):
        """(ink, width, mean advance) for `text`, per em, or None.

        None means this face covers none of the string. That is not a
        malformed font: Corsiva Hebrew parses cleanly, states a cmap and
        has no Latin letters at all, and measuring a headline against the
        punctuation that happened to match would be worse than declining.
        """
        glyphs = [self.cmap.get(ord(char)) for char in text]
        glyphs = [glyph for glyph in glyphs if glyph]
        if not glyphs:
            return None
        width = sum(self.advance(glyph) for glyph in glyphs)
        boxes = [self.bbox(glyph) for glyph in glyphs]
        boxes = [box for box in boxes if box]
        ink = max(b[3] for b in boxes) - min(b[1] for b in boxes) if boxes else None
        return (
            (ink / self.upem) if ink else None,
            width / self.upem,
            width / len(glyphs) / self.upem,
        )


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


# Where each platform keeps its fonts. Publisher's own headline faces --
# Monotype Corsiva and Pristina, between them 46 of the corpus's 48
# headlines -- ship with Office on Windows, which is where the converter
# runs as an .exe; on a Mac they are only present if someone installed
# them.
_FONT_DIRECTORIES = {
    "darwin": (
        "/System/Library/Fonts",
        "/System/Library/Fonts/Supplemental",
        "/Library/Fonts",
        "~/Library/Fonts",
    ),
    "win32": (
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    ),
}
_FONT_DIRECTORIES_DEFAULT = (
    "/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts",
    "~/.local/share/fonts",
)
_FONT_SUFFIXES = frozenset({".ttf", ".ttc", ".otf", ".otc"})


def font_directories() -> List[Path]:
    """Where to look for installed fonts on this platform."""
    import sys
    paths = _FONT_DIRECTORIES.get(sys.platform, _FONT_DIRECTORIES_DEFAULT)
    return [Path(p).expanduser() for p in paths if p]


# family (casefolded) -> subfamily (casefolded) -> path and index in file.
_index: Optional[Dict[str, Dict[str, Tuple[Path, int]]]] = None


def reset_index() -> None:
    """Forget the installed fonts, so the next lookup walks the disk again."""
    global _index
    _index = None


def _build_index() -> Dict[str, Dict[str, Tuple[Path, int]]]:
    """Every installed face, by family and subfamily.

    Only the table directory and the name table are read here: an index
    over a few hundred files has to be cheap, and the glyph tables are
    read later, for the one font a headline actually names.
    """
    found: Dict[str, Dict[str, Tuple[Path, int]]] = {}
    for directory in font_directories():
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _FONT_SUFFIXES:
                continue
            try:
                data = path.read_bytes()
                members = faces(data)
            except (OSError, FontError, struct.error, ValueError):
                continue
            for position, face in enumerate(members):
                if not face.family:
                    continue
                by_style = found.setdefault(face.family.casefold(), {})
                by_style.setdefault(face.subfamily.casefold(), (path, position))
    return found


def _style_names(bold: bool, italic: bool) -> List[str]:
    """Subfamily names to try, best first.

    A file that asks for bold and finds only the regular face is measured
    against regular: the metrics come out slightly narrow, which errs
    toward a headline that fits rather than one that overflows its band.
    """
    if bold and italic:
        wanted = ["bold italic", "bolditalic", "bold oblique", "bold", "italic"]
    elif bold:
        wanted = ["bold"]
    elif italic:
        wanted = ["italic", "oblique"]
    else:
        wanted = []
    return wanted + ["regular", "book", "roman", "normal"]


def find_face(family: str, bold: bool, italic: bool) -> Optional[Face]:
    """The installed face a headline names, or None if it is not there.

    The family has to match exactly. 'Corsiva Hebrew' ships with macOS and
    'Monotype Corsiva' does not, and they share a word but not a single
    Latin glyph, so a loose match would set 40 of the corpus's headlines
    from a font that cannot draw them.
    """
    global _index
    if not family:
        return None
    if _index is None:
        _index = _build_index()
    by_style = _index.get(family.casefold())
    if not by_style:
        return None
    for name in _style_names(bold, italic):
        if name in by_style:
            path, position = by_style[name]
            break
    else:
        path, position = next(iter(by_style.values()))
    try:
        return faces(path.read_bytes())[position]
    except (OSError, FontError, struct.error, IndexError):
        return None


# What a headline face averages, for a font this machine cannot read.
# Measured off two rendered headlines -- Monotype Corsiva at 20 pt inking
# 14.1 pt and a substituted Pristina at 30.1 pt inking 20.8 pt -- and
# checked against Publisher's own page, where a dropped initial inks 39.7
# pt of a 40.1 pt band. These were the only numbers the converter had
# before it could read a font; they are now the last resort.
#
# The ink and the glyph width do not agree (0.55 against an average
# advance of 0.50) because the width is deliberately generous: a headline
# sized by height alone overflows its band and wraps, and overset text is
# hidden rather than drawn. That asymmetry is preserved exactly, because
# it is what the output looks like today.
_AVERAGE_INK_PER_EM = 0.70
_AVERAGE_EM_PER_GLYPH = 0.55
_AVERAGE_EM_PER_ADVANCE = 0.50

# Faces measured once on a machine that has them, so a document converts
# the same way everywhere. `research/font_metrics.py` prints these.
# Entries are (ink per em, mean advance per em, em per glyph).
#
# These four are the corpus's headline faces -- Monotype Corsiva sets 40
# of its 48 WordArt shapes and Pristina 6 -- and both ship with Office
# rather than with either operating system, so the machine converting a
# document is quite likely not to have them. Measured over the corpus's
# own headline words, which is why the ink runs high: 'Verjaardagen'
# descends and a single sample word would not have shown that.
BAKED: Dict[str, Tuple[float, float, float]] = {
    # Monotype Corsiva Regular, measured over 7 headline(s)
    "monotype corsiva": (0.894, 0.382, 0.382),
    # Pristina Regular, measured over 7 headline(s)
    "pristina": (1.100, 0.347, 0.347),
    # Comic Sans MS Regular, measured over 7 headline(s)
    "comic sans ms": (0.965, 0.510, 0.510),
    # Arial Black Regular, measured over 7 headline(s)
    "arial black": (0.843, 0.598, 0.598),
}


@dataclass(frozen=True)
class Metrics:
    """What a string measures, per em, and where the numbers came from."""

    ink_per_em: float
    width_per_em: float
    mean_advance_per_em: float
    #: 'font' read from the font itself, 'table' a baked family average,
    #: 'average' the global constants above.
    source: str

    @property
    def exact(self) -> bool:
        """True only when these came from the font this text is set in.

        Only an exact measurement earns a horizontal scale: condensing
        glyphs by a ratio worked out from a guessed width is a
        confident-looking wrong answer.
        """
        return self.source == "font"


def _averages(text: str) -> Metrics:
    return Metrics(
        ink_per_em=_AVERAGE_INK_PER_EM,
        width_per_em=_AVERAGE_EM_PER_GLYPH * max(len(text), 1),
        mean_advance_per_em=_AVERAGE_EM_PER_ADVANCE,
        source="average",
    )


def measure(family: Optional[str], bold: bool, italic: bool, text: str) -> Metrics:
    """What `text` measures in the font that sets it.

    Three tiers, best first: the font itself if this machine has it, a
    baked average for the family if it does not, and the global averages
    if neither. A font that is installed but covers none of the string --
    Corsiva Hebrew asked for Latin -- counts as not having it.
    """
    if not text:
        text = " "
    if family:
        face = find_face(family, bold, italic)
        if face is not None:
            try:
                found = face.measure(text)
            except (FontError, struct.error):
                found = None
            if found is not None:
                ink, width, advance = found
                return Metrics(
                    # A CFF face states advances but no outlines, so the
                    # width is real and only the ink has to be borrowed.
                    ink_per_em=ink if ink else _AVERAGE_INK_PER_EM,
                    width_per_em=width,
                    mean_advance_per_em=advance,
                    source="font",
                )
        baked = BAKED.get(family.casefold())
        if baked:
            ink, advance, per_glyph = baked
            return Metrics(
                ink_per_em=ink,
                width_per_em=per_glyph * max(len(text), 1),
                mean_advance_per_em=advance,
                source="table",
            )
    return _averages(text)
