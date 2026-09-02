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


# A face carries two family names, and which one a program reads decides
# whether it can find the font at all. IDs 1 and 2 are the legacy pair,
# where a family holds at most four styles, so anything outside regular /
# bold / italic / bold-italic has to be split off into a family of its own
# -- 'Calibri Light'. IDs 16 and 17 are the typographic pair, which has no
# such limit and calls the same file the 'Light' style of 'Calibri'. A face
# that needs the split states both; one that does not leaves 16 and 17 out.
_NAME_FAMILY, _NAME_SUBFAMILY = 1, 2
_NAME_POSTSCRIPT = 6
_NAME_TYPO_FAMILY, _NAME_TYPO_SUBFAMILY = 16, 17


def _win_metrics(buf: bytes, tables: Dict[str, Tuple[int, int]]):
    """usWinAscent and usWinDescent, or (None, None) where unreadable.

    These two are what Windows adds up to make a line: GDI reports their
    sum as `tmHeight`. They sit at a fixed offset in every OS/2 version,
    but the table itself is optional and old ones are short, so both the
    presence and the length are checked.
    """
    found = tables.get("OS/2")
    if not found:
        return None, None
    offset, length = found
    if length < 78 or offset + 78 > len(buf):
        return None, None
    ascent, descent = struct.unpack_from(">HH", buf, offset + 74)
    if not ascent + descent:
        return None, None
    return ascent, descent


def _external_leading(buf: bytes, tables: Dict[str, Tuple[int, int]],
                      win_height: int) -> int:
    """GDI's `tmExternalLeading` -- the gap it adds outside the line box.

    GDI does not simply pass `hhea`'s line gap through: it gives back
    whatever of the gap the win metrics have not already swallowed, which
    is `lineGap - ((usWinAscent + usWinDescent) - (ascender - descender))`,
    floored at zero. Calibri's comes out at exactly nothing -- its gap of
    452 units is precisely what its win metrics add over its `hhea` pair --
    which is why the corpus, being almost all Calibri, cannot tell this
    term from its absence. Times New Roman's is 87 units, and including it
    is what puts 12pt of it on the 13.8pt line Word has always set it on.
    """
    found = tables.get("hhea")
    if not found:
        return 0
    offset, length = found
    if length < 12 or offset + 12 > len(buf):
        return 0
    ascender, descender, line_gap = struct.unpack_from(">hhh", buf, offset + 4)
    return max(0, line_gap - (win_height - (ascender - descender)))


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
        self.postscript = names.get(_NAME_POSTSCRIPT)
        self.win_ascent, self.win_descent = _win_metrics(buf, self.tables)
        self.external_leading = (
            0 if self.win_ascent is None
            else _external_leading(buf, self.tables, self.win_ascent + self.win_descent)
        )
        # Left as None where the face states no split, which is most of
        # them: absent means 'the legacy names are the whole story'.
        self.typo_family = names.get(_NAME_TYPO_FAMILY)
        self.typo_subfamily = names.get(_NAME_TYPO_SUBFAMILY)
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
        """(ink, ink above the baseline, width, mean advance), per em, or None.

        None means this face covers none of the string. That is not a
        malformed font: Corsiva Hebrew parses cleanly, states a cmap and
        has no Latin letters at all, and measuring a headline against the
        punctuation that happened to match would be worse than declining.

        The ink and the part of it above the baseline are two numbers
        rather than one because a headline needs both: the ink says what
        point size fills the band, and the share of it above the baseline
        says where in the band the baseline goes -- which is the only way
        to ask a reader for the line Publisher drew (`README`, *A recovered
        headline states its own first baseline*).
        """
        glyphs = [self.cmap.get(ord(char)) for char in text]
        glyphs = [glyph for glyph in glyphs if glyph]
        if not glyphs:
            return None
        width = sum(self.advance(glyph) for glyph in glyphs)
        boxes = [self.bbox(glyph) for glyph in glyphs]
        boxes = [box for box in boxes if box]
        top = max(b[3] for b in boxes) if boxes else None
        ink = top - min(b[1] for b in boxes) if boxes else None
        return (
            (ink / self.upem) if ink else None,
            (top / self.upem) if ink else None,
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
    _namings.clear()
    _line_metrics.clear()


def _build_index() -> Dict[str, Dict[str, Tuple[Path, int]]]:
    """Every installed face, by family and subfamily.

    Only the table directory and the name table are read here: an index
    over a few hundred files has to be cheap, and the glyph tables are
    read later, for the one font a headline actually names.
    """
    # A face goes in under both of its namings, so it is found by whichever
    # one the caller has: Publisher states the legacy pair and a reader
    # lists the typographic one. But every legacy pair is claimed before
    # any typographic pair is, in two passes rather than one, because the
    # files are walked in name order and a single pass would let a font
    # claim a slot with its recovered name before the font whose own legacy
    # names *are* that slot is even read.
    legacy: List[Tuple[str, str, Path, int]] = []
    typographic: List[Tuple[str, str, Path, int]] = []
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
                if face.family:
                    legacy.append(
                        (face.family, face.subfamily, path, position)
                    )
                if face.typo_family:
                    typographic.append(
                        (face.typo_family, face.typo_subfamily or face.subfamily,
                         path, position)
                    )

    found: Dict[str, Dict[str, Tuple[Path, int]]] = {}
    for family, subfamily, path, position in legacy + typographic:
        by_style = found.setdefault(family.casefold(), {})
        by_style.setdefault(subfamily.casefold(), (path, position))
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


@dataclass(frozen=True)
class Naming:
    """What to call a face, for a reader that indexes by typographic name.

    Publisher is a GDI application and names a run's font by the legacy
    family alone, so it writes 'Calibri Light' + bold. Affinity and macOS
    index by the typographic family, where that file is 'Calibri' + the
    'Light' style and no family called 'Calibri Light' exists at all --
    which is what a reader means when it reports the font as missing.
    """

    #: The family a reader lists the face under.
    family: str
    #: The style within that family, as the face names it ('Light Italic').
    style: str
    #: The face's own PostScript name, read from the file rather than
    #: guessed by squeezing the spaces out of the family.
    postscript: str
    #: True where the run asked for bold, the family folded into a weight,
    #: and no bold of that weight exists to set it in. Publisher draws
    #: those by stroking the outline; see `_FAUX_BOLD_EM` in `convert`.
    faux_bold: bool = False
    #: True where the run asked for italic and the family has no italic at
    #: all. Publisher draws those by shearing the glyphs; see
    #: `_FAUX_ITALIC_DEGREES` in `convert`.
    faux_italic: bool = False
    #: True where this is not the name that was asked for, i.e. where
    #: passing the file's own name through is what loses the font.
    folded: bool = False


_namings: Dict[Tuple[str, bool, bool], Optional[Naming]] = {}


def naming(family: str, bold: bool, italic: bool) -> Optional[Naming]:
    """How a reader names the face this file asks for, or None if absent.

    None means the font is not installed here, which is not the same as
    the name being wrong: a name that cannot be checked is passed through
    as the file states it, because on the machine that has the font it is
    very likely right.
    """
    if not family:
        return None
    key = (family.casefold(), bold, italic)
    if key not in _namings:
        _namings[key] = _resolve_naming(family, bold, italic)
    return _namings[key]


def _resolve_naming(family: str, bold: bool, italic: bool) -> Optional[Naming]:
    face = find_face(family, bold, italic)
    if face is None or not face.family:
        return None
    typographic = face.typo_family or face.family
    style = face.typo_subfamily or face.subfamily
    # Against the name asked for, not against the face's other name: asking
    # by the typographic name already is not a name in need of changing.
    folded = typographic.casefold() != family.casefold()
    return Naming(
        family=typographic,
        style=style,
        postscript=face.postscript or typographic.replace(" ", ""),
        # A family that folded has its weight in the style name, so bold on
        # top of it is a face that was never drawn -- Calibri Light Bold
        # does not exist in any Calibri release. Where the names agree the
        # missing bold is only missing here, on this machine, and saying so
        # is the report's job rather than something to paint over.
        faux_bold=bold and folded and "bold" not in style.casefold(),
        # No such caution is needed for the italic: where the corpus asks
        # for one the family has none anywhere -- Blackadder ITC, Segoe
        # Script and Mystical Woods each ship a single slant -- and
        # Publisher's own PDF shears all three rather than substituting.
        faux_italic=italic and not _is_italic(style),
        folded=folded,
    )


def _is_italic(style: str) -> bool:
    folded = style.casefold()
    return "italic" in folded or "oblique" in folded


@dataclass(frozen=True)
class LineMetrics:
    """What one line of a face measures, in ems, and where its baseline sits.

    Publisher is a GDI application, and one "space" of its line spacing is
    the line GDI reports for the face -- `tmHeight` plus
    `tmExternalLeading`, which is usWinAscent + usWinDescent + whatever of
    the font's line gap those two have not already taken up. It is not the
    flat 120% of the type size that InDesign and Affinity both call Auto.

    Calibri's is 1.2207 em, so every line of it is 1.7% tighter in Affinity
    than Publisher drew it, and a column of forty accumulates half a line
    of the difference. Measured against Publisher's own PDFs, which agree
    with this to a hundredth of a point in every case the corpus offers:

    | set as                              | Publisher | 1.2 em |  this |
    |-------------------------------------|-----------|--------|-------|
    | 0.85 sp, 10.0008pt Calibri (1336)   |     10.38 |  10.20 | 10.38 |
    | 0.90 sp, 10.0008pt Calibri (1337)   |     11.00 |  10.80 | 10.99 |
    | 0.90 sp, 7.9992pt Calibri Light     |      8.80 |   8.64 |  8.79 |
    | 1 sp, 10.0008pt Calibri (1337 p30)  |     12.20 |  12.00 | 12.21 |

    Every one of those is Calibri, because every multi-line paragraph the
    corpus sets is: its Arial and Times New Roman are table cells, whose
    baselines are a row height rather than a leading, and its Segoe Script
    and Monotype Corsiva are headlines. So the *rule* is measured and the
    *reach* of it is inferred -- from GDI being what Publisher asks, and
    from the same rule putting 12pt Times New Roman on the 13.8pt line Word
    has always set it on. Worth a look on the first non-Calibri paragraph
    to come through with a stated line spacing.
    """

    #: Baseline to baseline for one space of spacing.
    height: float
    #: Frame top to the first baseline, for one space of spacing. Publisher
    #: stacks each line as a box of `height` and hangs the baseline this far
    #: into it, which is why its first baseline moves with the line spacing.
    #: Nothing writes this yet -- see `backlog.md` on the first baseline.
    ascent: float


def line_metrics(family: Optional[str], bold: bool, italic: bool) -> Optional[LineMetrics]:
    """One line of the face this run is set in, or None if it cannot be read."""
    key = ((family or "").casefold(), bold, italic)
    if key not in _line_metrics:
        _line_metrics[key] = _resolve_line_metrics(family, bold, italic)
    return _line_metrics[key]


def _resolve_line_metrics(family, bold, italic) -> Optional[LineMetrics]:
    if not family:
        return None
    face = find_face(family, bold, italic)
    if face is None or face.win_ascent is None:
        return None
    total = face.win_ascent + face.win_descent + face.external_leading
    return LineMetrics(height=total / face.upem, ascent=face.win_ascent / face.upem)


_line_metrics: Dict[Tuple[str, bool, bool], Optional[LineMetrics]] = {}


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

# How much of that ink sits above the baseline. The four faces below put
# between 0.74 and 0.85 of theirs there, so a headline of unknown face is
# given the middle of that: 0.79 of the 0.70 above. It decides where the
# baseline goes inside the band, so being out by a twentieth of an em
# moves a headline by a twentieth of its band -- visible, but nothing
# like the alternative, which is a reader placing the baseline by the
# font's own ascent and hiding the headline altogether.
_AVERAGE_INK_ABOVE_BASELINE_PER_EM = 0.55

# Faces measured once on a machine that has them, so a document converts
# the same way everywhere. `research/font_metrics.py` prints these.
# Entries are (ink per em, ink above the baseline per em, mean advance per
# em, em per glyph).
#
# These four are the corpus's headline faces -- Monotype Corsiva sets 40
# of its 48 WordArt shapes and Pristina 6 -- and both ship with Office
# rather than with either operating system, so the machine converting a
# document is quite likely not to have them. Measured over the corpus's
# own headline words, which is why the ink runs high: 'Verjaardagen'
# descends and a single sample word would not have shown that.
BAKED: Dict[str, Tuple[float, float, float, float]] = {
    # Monotype Corsiva Regular, measured over 7 headline(s)
    "monotype corsiva": (0.894, 0.688, 0.382, 0.382),
    # Pristina Regular, measured over 7 headline(s)
    "pristina": (1.100, 0.814, 0.347, 0.347),
    # Comic Sans MS Regular, measured over 7 headline(s)
    "comic sans ms": (0.965, 0.781, 0.510, 0.510),
    # Arial Black Regular, measured over 7 headline(s)
    "arial black": (0.843, 0.718, 0.598, 0.598),
}


@dataclass(frozen=True)
class Metrics:
    """What a string measures, per em, and where the numbers came from."""

    ink_per_em: float
    #: How much of that ink sits above the baseline, per em. A headline is
    #: sized so its ink fills the band, so this is where in the band the
    #: baseline falls -- and stating that is what stops a reader placing
    #: the baseline by the font's own ascent, which is taller than the
    #: band and hides the headline.
    ink_above_baseline_per_em: float
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
        ink_above_baseline_per_em=_AVERAGE_INK_ABOVE_BASELINE_PER_EM,
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
                ink, above, width, advance = found
                return Metrics(
                    # A CFF face states advances but no outlines, so the
                    # width is real and only the ink has to be borrowed --
                    # and the baseline inside it with the ink, because
                    # there are no boxes to find it in either.
                    ink_per_em=ink if ink else _AVERAGE_INK_PER_EM,
                    ink_above_baseline_per_em=(
                        above if ink else _AVERAGE_INK_ABOVE_BASELINE_PER_EM
                    ),
                    width_per_em=width,
                    mean_advance_per_em=advance,
                    source="font",
                )
        baked = BAKED.get(family.casefold())
        if baked:
            ink, above, advance, per_glyph = baked
            return Metrics(
                ink_per_em=ink,
                ink_above_baseline_per_em=above,
                width_per_em=per_glyph * max(len(text), 1),
                mean_advance_per_em=advance,
                source="table",
            )
    return _averages(text)
