# WordArt Font Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Size and space WordArt headlines from the real metrics of the font they are set in, instead of from three hand-fitted averages, and then rewrite the conversion report against what is actually left approximate.

**Architecture:** A new standard-library-only module `pubidml/fontmetrics.py` reads sfnt (TrueType/OpenType) font files and answers one question: what does this string measure, per em, in this font. `pubfile` stops guessing a point size while parsing and reports only what the .pub states. `convert` does the fitting: size from the band's per-line height, condensation as `HorizontalScale` from the band's width, tracking from the font's real mean advance. Three tiers — measured font, baked table, global averages — so a machine without the font gets today's behaviour rather than a wrong answer.

**Tech Stack:** Python 3, standard library only (`struct`, `pathlib`, `os`). Tests are `unittest`, run with `make test`. No new dependencies — the runtime is standard-library-only by constraint (`pub2idml.spec`).

## Global Constraints

- **Standard library only.** `pub2idml.spec` excludes optional modules and states "The converter is standard library only". No `fonttools`, no `Pillow`.
- **Tests run via `make test`**, which is `python3 -m unittest discover -s tests -t .` from the repo root.
- **Never crash on a malformed font.** Every reader entry point raises `FontError` or returns `None`; a font file that does not parse is skipped silently, exactly as `wmf.py` treats a metafile it cannot read. A missing font directory is not an error.
- **Comment style follows the codebase**: explain *why* a rule exists and what evidence settles it, in prose, with the corpus figure that justifies it. Do not add comments that restate the code.
- **Tier 3 must reproduce today's output exactly.** The existing constants keep their current values: ink per em `0.70`, em per glyph `0.55`, em per advance `0.50`. The first two differ deliberately — that is today's behaviour and the regression tests depend on it.
- **Corpus facts, verified, that the code may rely on:** all 48 WordArt shapes in `files/` state the stretch flag and have it set; the flag is bit 10 of property `0x00FF` (`0xFF - 10 = 0xF5`, `gtextFStretch`). Corpus headline fonts are Monotype Corsiva (40 shapes), Pristina (6), Comic Sans MS (1), Arial Black (1).

---

## File Structure

| File | Responsibility |
|---|---|
| `pubidml/fontmetrics.py` (new) | sfnt reading, the installed-font index, and `measure()` with its three tiers. Knows nothing about .pub files. |
| `tests/test_fontmetrics.py` (new) | Synthetic sfnt bytes built in the test; asserts exact metrics and that malformed input raises. |
| `pubidml/pubfile.py` (modify) | Reads the stretch flag. Stops deciding a point size: `_band_size` and its two constants are deleted. |
| `pubidml/convert.py` (modify) | The sizing rule, `HorizontalScale`, tracking from measured advances, and the report rewrite. |
| `pubidml/model.py` (modify) | Removes a duplicated `tracking` field on `Span`. |
| `research/font_metrics.py` (new) | Prints a baked-table entry for an installed face, in the style of the existing `research/` probes. |
| `README.md` (modify) | Replaces the paragraph deferring the stated-size override. |

---

### Task 1: Read an sfnt file's tables and names

**Files:**
- Create: `pubidml/fontmetrics.py`
- Test: `tests/test_fontmetrics.py`

**Interfaces:**
- Produces: `FontError(Exception)`; `class Face` with attributes `family: Optional[str]`, `subfamily: str`, `upem: int`, `long_loca: bool`, `tables: Dict[str, Tuple[int, int]]`, constructed as `Face(buf: bytes, base: int = 0)`; `faces(buf: bytes) -> List[Face]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fontmetrics.py`. This file also holds the synthetic font builder that every later task reuses, so it is written in full here. The builder has been verified against the reader; do not simplify it.

```python
"""Metrics read out of a font file, and the synthetic fonts to test with.

The builder below writes real sfnt bytes rather than mocking the reader,
the way `test_pubfile.py` builds real Escher records: a reader tested
against a mock only proves the mock agrees with itself.
"""
import struct
import unittest

from pubidml import fontmetrics


def _table_directory(tables):
    """An sfnt header plus table records, with offsets resolved."""
    count = len(tables)
    search, entry = 1, 0
    while search * 2 <= count:
        search *= 2
        entry += 1
    header = struct.pack(
        ">IHHHH", 0x00010000, count, search * 16, entry, (count - search) * 16
    )
    offset = len(header) + 16 * count
    records, body = b"", b""
    for tag, data in sorted(tables.items()):
        records += struct.pack(">4sIII", tag.encode("ascii"), 0, offset, len(data))
        padded = data + b"\x00" * (-len(data) % 4)
        body += padded
        offset += len(padded)
    return header + records + body


def name_table(family, subfamily, platform=3, language=0x0409):
    entries = [(1, family), (2, subfamily)]
    storage, records = b"", b""
    for nid, value in entries:
        raw = value.encode("mac-roman") if platform == 1 else value.encode("utf-16-be")
        records += struct.pack(
            ">6H", platform, 1, language, nid, len(raw), len(storage)
        )
        storage += raw
    return struct.pack(">HHH", 0, len(entries), 6 + 12 * len(entries)) + records + storage


def head_table(upem, long_loca):
    return (
        struct.pack(">IIIIHH", 0x00010000, 0, 0, 0x5F0F3CF5, 0, upem)
        + b"\x00" * 16
        + struct.pack(">hhhh", 0, 0, 0, 0)
        + struct.pack(">HHhhh", 0, 0, 0, 1 if long_loca else 0, 0)
    )


def hhea_table(num_h_metrics):
    return b"\x00" * 34 + struct.pack(">H", num_h_metrics)


def maxp_table(num_glyphs):
    return struct.pack(">IH", 0x00010000, num_glyphs) + b"\x00" * 26


def hmtx_table(advances):
    return b"".join(struct.pack(">Hh", a, 0) for a in advances)


def cmap_table(mapping, fmt=4):
    """A character-to-glyph subtable, format 4 or 12."""
    if fmt == 12:
        groups = b"".join(
            struct.pack(">III", code, code, gid) for code, gid in sorted(mapping.items())
        )
        sub = struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, len(mapping)) + groups
        return struct.pack(">HHHHI", 0, 1, 3, 10, 12) + sub
    segs = [(c, c, mapping[c]) for c in sorted(mapping)] + [(0xFFFF, 0xFFFF, 0)]
    ends = b"".join(struct.pack(">H", s[1]) for s in segs)
    starts = b"".join(struct.pack(">H", s[0]) for s in segs)
    deltas = b"".join(struct.pack(">h", -(s[0] - s[2])) for s in segs)
    ranges = b"\x00\x00" * len(segs)
    search, entry = 1, 0
    while search * 2 <= len(segs):
        search *= 2
        entry += 1
    sub = (
        struct.pack(">HHHHHHH", 4, 0, 0, len(segs) * 2,
                    search * 2, entry, (len(segs) - search) * 2)
        + ends + b"\x00\x00" + starts + deltas + ranges
    )
    sub = sub[:2] + struct.pack(">H", len(sub)) + sub[4:]
    return struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub


def glyf_and_loca(boxes, long_loca=True):
    glyf, offsets = b"", [0]
    for box in boxes:
        if box is None:
            offsets.append(len(glyf))
            continue
        glyf += struct.pack(">hhhhh", 1, *box) + b"\x00" * 12
        glyf += b"\x00" * (-len(glyf) % 4)
        offsets.append(len(glyf))
    if long_loca:
        return glyf, b"".join(struct.pack(">I", o) for o in offsets)
    return glyf, b"".join(struct.pack(">H", o // 2) for o in offsets)


def build_font(family="Test Sans", subfamily="Regular", upem=1000,
               glyphs=None, long_loca=True, cmap_format=4, platform=3):
    """glyphs: {char: (advance, (xMin, yMin, xMax, yMax))}, glyph ids from 1."""
    glyphs = {"A": (600, (50, 0, 550, 700))} if glyphs is None else glyphs
    chars = sorted(glyphs)
    mapping = {ord(c): i + 1 for i, c in enumerate(chars)}
    advances = [0] + [glyphs[c][0] for c in chars]
    boxes = [None] + [glyphs[c][1] for c in chars]
    glyf, loca = glyf_and_loca(boxes, long_loca)
    return _table_directory({
        "head": head_table(upem, long_loca),
        "name": name_table(family, subfamily, platform),
        "maxp": maxp_table(len(advances)),
        "hhea": hhea_table(len(advances)),
        "hmtx": hmtx_table(advances),
        "cmap": cmap_table(mapping, cmap_format),
        "loca": loca,
        "glyf": glyf,
    })


def _rebase(font, delta):
    """A font's table offsets moved, as embedding it in a .ttc requires.

    A collection states each table's offset from the start of the *file*,
    not of the font, so a font copied into one has to have its directory
    rewritten. Getting this wrong is invisible until a table is read.
    """
    count = struct.unpack_from(">H", font, 4)[0]
    out = bytearray(font)
    for i in range(count):
        at = 12 + 16 * i + 8
        struct.pack_into(">I", out, at, struct.unpack_from(">I", font, at)[0] + delta)
    return bytes(out)


def build_collection(*fonts):
    header = b"ttcf" + struct.pack(">II", 0x00010000, len(fonts))
    base = len(header) + 4 * len(fonts)
    offsets, body = b"", b""
    for font in fonts:
        start = base + len(body)
        offsets += struct.pack(">I", start)
        body += _rebase(font, start)
    return header + offsets + body


class FaceReadingTest(unittest.TestCase):
    def test_a_font_states_its_family_and_units_per_em(self):
        face, = fontmetrics.faces(build_font(family="Kerk Display", upem=2048))
        self.assertEqual(face.family, "Kerk Display")
        self.assertEqual(face.subfamily, "Regular")
        self.assertEqual(face.upem, 2048)

    def test_a_bold_face_states_its_subfamily(self):
        face, = fontmetrics.faces(build_font(subfamily="Bold"))
        self.assertEqual(face.subfamily, "Bold")

    def test_a_unicode_platform_name_is_not_read_as_mac_roman(self):
        # Platform 0 stores UTF-16BE like platform 3 does. Reading it as a
        # byte encoding is what turns 'Times New Roman' into
        # ' T i m e s   N e w   R o m a n'.
        face, = fontmetrics.faces(build_font(family="Times New Roman", platform=0))
        self.assertEqual(face.family, "Times New Roman")

    def test_every_font_in_a_collection_is_read(self):
        collection = build_collection(
            build_font(family="One"), build_font(family="Two", subfamily="Bold")
        )
        found = fontmetrics.faces(collection)
        self.assertEqual([f.family for f in found], ["One", "Two"])
        self.assertEqual(found[1].subfamily, "Bold")

    def test_a_truncated_font_raises_rather_than_crashes(self):
        font = build_font()
        for cut in (4, 11, 20, 60):
            with self.subTest(cut=cut):
                with self.assertRaises(fontmetrics.FontError):
                    fontmetrics.faces(font[:cut])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_fontmetrics -v`
Expected: FAIL with `ModuleNotFoundError` or `AttributeError: module 'pubidml.fontmetrics' has no attribute 'faces'`.

- [ ] **Step 3: Write the implementation**

Create `pubidml/fontmetrics.py` with the module docstring and this much of the reader:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_fontmetrics -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add pubidml/fontmetrics.py tests/test_fontmetrics.py
git commit -m "Read a font file far enough to name the face it holds"
```

---

### Task 2: Measure a string in a face

**Files:**
- Modify: `pubidml/fontmetrics.py`
- Test: `tests/test_fontmetrics.py`

**Interfaces:**
- Consumes: `Face`, `FontError`, `faces()` from Task 1; the builders `build_font`, `build_collection` from `tests/test_fontmetrics.py`.
- Produces: `Face.cmap -> Dict[int, int]`; `Face.advance(gid: int) -> int`; `Face.bbox(gid: int) -> Optional[Tuple[int, int, int, int]]`; `Face.measure(text: str) -> Optional[Tuple[Optional[float], float, float]]` returning `(ink_per_em, width_per_em, mean_advance_per_em)`, or `None` when the face covers none of the string's characters.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fontmetrics.py`:

```python
# A face with two glyphs of known advance and known box, so every number
# below is arithmetic on values stated in the test rather than a
# measurement of a real font that might change under us.
_GLYPHS = {
    "A": (600, (50, 0, 550, 700)),      # no descender: inks 700 of 1000
    "y": (500, (20, -200, 480, 500)),   # descends to -200
}


class MeasurementTest(unittest.TestCase):
    def face(self, **kwargs):
        return fontmetrics.faces(build_font(glyphs=_GLYPHS, **kwargs))[0]

    def test_one_glyph_measures_its_own_box_and_advance(self):
        ink, width, advance = self.face().measure("A")
        self.assertAlmostEqual(ink, 0.700)
        self.assertAlmostEqual(width, 0.600)
        self.assertAlmostEqual(advance, 0.600)

    def test_ink_spans_the_tallest_and_deepest_glyph_of_the_string(self):
        # 700 up and 200 down is 900 units of a 1000-unit em. This is the
        # descender case the old 0.70 constant got wrong by construction.
        ink, width, advance = self.face().measure("Ay")
        self.assertAlmostEqual(ink, 0.900)
        self.assertAlmostEqual(width, 1.100)
        self.assertAlmostEqual(advance, 0.550)

    def test_a_short_loca_font_measures_the_same(self):
        ink, _width, _advance = self.face(long_loca=False).measure("A")
        self.assertAlmostEqual(ink, 0.700)

    def test_a_format_12_cmap_is_read(self):
        ink, _width, _advance = self.face(cmap_format=12).measure("A")
        self.assertAlmostEqual(ink, 0.700)

    def test_units_per_em_scales_the_result(self):
        big = fontmetrics.faces(
            build_font(upem=2000, glyphs={"A": (1200, (100, 0, 1100, 1400))})
        )[0]
        ink, width, _advance = big.measure("A")
        self.assertAlmostEqual(ink, 0.700)
        self.assertAlmostEqual(width, 0.600)

    def test_a_face_covering_none_of_the_string_measures_nothing(self):
        # Corsiva Hebrew is real and does exactly this: it parses, it has a
        # cmap, and it has no Latin letters at all. Measuring it would put a
        # headline at whatever punctuation happened to match.
        self.assertIsNone(self.face().measure("שלום"))

    def test_characters_the_face_lacks_are_skipped_not_counted(self):
        ink, width, advance = self.face().measure("A\u05d0")
        self.assertAlmostEqual(ink, 0.700)
        self.assertAlmostEqual(width, 0.600)
        self.assertAlmostEqual(advance, 0.600)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_fontmetrics.MeasurementTest -v`
Expected: FAIL with `AttributeError: 'Face' object has no attribute 'measure'`.

- [ ] **Step 3: Write the implementation**

Add to `pubidml/fontmetrics.py`, above `class Face`:

```python
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
```

Then add these methods to `Face`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_fontmetrics -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Sanity-check against a real font**

Run:

```bash
python3 -c "
import pathlib
from pubidml import fontmetrics
face, = fontmetrics.faces(pathlib.Path('/System/Library/Fonts/Supplemental/Comic Sans MS.ttf').read_bytes())
print(face.family, face.measure('Kerkdiensten'))
"
```

Expected: `Comic Sans MS (0.837..., 6.147..., 0.512...)`. If the ink is not near 0.84, the `glyf`/`loca` reading is wrong — do not proceed.

- [ ] **Step 6: Commit**

```bash
git add pubidml/fontmetrics.py tests/test_fontmetrics.py
git commit -m "Measure what a string inks and how wide it sets"
```

---

### Task 3: Find the installed fonts

**Files:**
- Modify: `pubidml/fontmetrics.py`
- Test: `tests/test_fontmetrics.py`

**Interfaces:**
- Consumes: `Face`, `faces()`, `FontError`.
- Produces: `font_directories() -> List[Path]`; `find_face(family: str, bold: bool, italic: bool) -> Optional[Face]`; `reset_index() -> None` (drops the cached index, for tests).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fontmetrics.py`:

```python
import tempfile
from pathlib import Path
from unittest import mock


class FontIndexTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patch = mock.patch.object(
            fontmetrics, "font_directories", lambda: [self.root]
        )
        patch.start()
        self.addCleanup(patch.stop)
        fontmetrics.reset_index()
        self.addCleanup(fontmetrics.reset_index)

    def write(self, name, **kwargs):
        (self.root / name).write_bytes(build_font(**kwargs))

    def test_a_family_is_found_by_name(self):
        self.write("kerk.ttf", family="Kerk Display")
        face = fontmetrics.find_face("Kerk Display", bold=False, italic=False)
        self.assertIsNotNone(face)
        self.assertEqual(face.family, "Kerk Display")

    def test_a_family_that_is_not_installed_is_not_found(self):
        self.write("kerk.ttf", family="Kerk Display")
        self.assertIsNone(fontmetrics.find_face("Pristina", bold=False, italic=False))

    def test_a_near_miss_family_is_not_substituted(self):
        # 'Corsiva Hebrew' ships with macOS and 'Monotype Corsiva' does not.
        # Matching on a shared word would set 40 of the corpus's headlines
        # from a font with no Latin letters in it.
        self.write("corsiva.ttf", family="Corsiva Hebrew")
        self.assertIsNone(
            fontmetrics.find_face("Monotype Corsiva", bold=False, italic=False)
        )

    def test_the_bold_face_is_preferred_when_bold_is_asked_for(self):
        self.write("plain.ttf", family="Kerk Display", subfamily="Regular")
        self.write("bold.ttf", family="Kerk Display", subfamily="Bold")
        face = fontmetrics.find_face("Kerk Display", bold=True, italic=False)
        self.assertEqual(face.subfamily, "Bold")

    def test_regular_stands_in_when_the_bold_face_is_missing(self):
        # Slightly narrow metrics, which errs toward a headline that fits.
        self.write("plain.ttf", family="Kerk Display", subfamily="Regular")
        face = fontmetrics.find_face("Kerk Display", bold=True, italic=False)
        self.assertEqual(face.subfamily, "Regular")

    def test_a_collection_contributes_every_face_it_holds(self):
        (self.root / "both.ttc").write_bytes(build_collection(
            build_font(family="Kerk Display", subfamily="Regular"),
            build_font(family="Kerk Display", subfamily="Italic"),
        ))
        face = fontmetrics.find_face("Kerk Display", bold=False, italic=True)
        self.assertEqual(face.subfamily, "Italic")

    def test_a_file_that_is_not_a_font_is_skipped(self):
        (self.root / "notes.txt").write_bytes(b"this is not a font")
        (self.root / "broken.ttf").write_bytes(b"\x00\x01\x00\x00truncated")
        self.write("kerk.ttf", family="Kerk Display")
        self.assertIsNotNone(
            fontmetrics.find_face("Kerk Display", bold=False, italic=False)
        )

    def test_a_font_directory_that_does_not_exist_is_not_an_error(self):
        with mock.patch.object(
            fontmetrics, "font_directories", lambda: [self.root / "nope"]
        ):
            fontmetrics.reset_index()
            self.assertIsNone(
                fontmetrics.find_face("Kerk Display", bold=False, italic=False)
            )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_fontmetrics.FontIndexTest -v`
Expected: FAIL with `AttributeError: module 'pubidml.fontmetrics' has no attribute 'reset_index'`.

- [ ] **Step 3: Write the implementation**

Add to `pubidml/fontmetrics.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_fontmetrics -v`
Expected: PASS, 20 tests.

- [ ] **Step 5: Commit**

```bash
git add pubidml/fontmetrics.py tests/test_fontmetrics.py
git commit -m "Find the face a headline names among the installed fonts"
```

---

### Task 4: `measure()` and its three tiers

**Files:**
- Modify: `pubidml/fontmetrics.py`
- Test: `tests/test_fontmetrics.py`

**Interfaces:**
- Consumes: `find_face()`, `Face.measure()`.
- Produces: `@dataclass(frozen=True) Metrics` with fields `ink_per_em: float`, `width_per_em: float`, `mean_advance_per_em: float`, `source: str` and property `exact: bool` (`source == "font"`); `measure(family: Optional[str], bold: bool, italic: bool, text: str) -> Metrics`; `BAKED: Dict[str, Tuple[float, float, float]]` mapping a casefolded family to `(ink_per_em, mean_advance_per_em, em_per_glyph)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fontmetrics.py`:

```python
class TierTest(unittest.TestCase):
    """Which of the three sources a headline's metrics come from."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patch = mock.patch.object(
            fontmetrics, "font_directories", lambda: [self.root]
        )
        patch.start()
        self.addCleanup(patch.stop)
        fontmetrics.reset_index()
        self.addCleanup(fontmetrics.reset_index)

    def test_an_installed_font_is_measured_exactly(self):
        (self.root / "kerk.ttf").write_bytes(
            build_font(family="Kerk Display", glyphs=_GLYPHS)
        )
        found = fontmetrics.measure("Kerk Display", False, False, "Ay")
        self.assertEqual(found.source, "font")
        self.assertTrue(found.exact)
        self.assertAlmostEqual(found.ink_per_em, 0.900)
        self.assertAlmostEqual(found.width_per_em, 1.100)
        self.assertAlmostEqual(found.mean_advance_per_em, 0.550)

    def test_a_baked_family_is_used_when_the_font_is_not_installed(self):
        with mock.patch.dict(
            fontmetrics.BAKED, {"pristina": (0.62, 0.41, 0.44)}, clear=False
        ):
            found = fontmetrics.measure("Pristina", False, False, "Kerkbode")
        self.assertEqual(found.source, "table")
        self.assertFalse(found.exact)
        self.assertAlmostEqual(found.ink_per_em, 0.62)
        self.assertAlmostEqual(found.mean_advance_per_em, 0.41)
        self.assertAlmostEqual(found.width_per_em, 0.44 * len("Kerkbode"))

    def test_an_unknown_font_falls_back_to_the_averages(self):
        found = fontmetrics.measure("Nothing Here", False, False, "Kerkbode")
        self.assertEqual(found.source, "average")
        self.assertFalse(found.exact)
        self.assertAlmostEqual(found.ink_per_em, 0.70)
        self.assertAlmostEqual(found.mean_advance_per_em, 0.50)
        self.assertAlmostEqual(found.width_per_em, 0.55 * len("Kerkbode"))

    def test_a_font_stating_no_name_falls_back_to_the_averages(self):
        found = fontmetrics.measure(None, False, False, "Kerkbode")
        self.assertEqual(found.source, "average")

    def test_a_font_without_the_string_s_glyphs_falls_through(self):
        # Found, parses, states a cmap, covers none of the string.
        (self.root / "hebrew.ttf").write_bytes(build_font(
            family="Corsiva Hebrew", glyphs={"\u05d0": (500, (0, 0, 400, 600))}
        ))
        found = fontmetrics.measure("Corsiva Hebrew", False, False, "Kerkbode")
        self.assertEqual(found.source, "average")

    def test_a_font_with_no_outlines_falls_through_for_ink_only(self):
        # An OpenType/CFF face has advances but no glyf boxes. The width is
        # still real; only the ink has to be borrowed.
        face = build_font(family="Kerk Display", glyphs=_GLYPHS)
        (self.root / "kerk.ttf").write_bytes(face)
        with mock.patch.object(fontmetrics.Face, "bbox", lambda self, glyph: None):
            found = fontmetrics.measure("Kerk Display", False, False, "Ay")
        self.assertEqual(found.source, "font")
        self.assertAlmostEqual(found.width_per_em, 1.100)
        self.assertAlmostEqual(found.ink_per_em, 0.70)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_fontmetrics.TierTest -v`
Expected: FAIL with `AttributeError: module 'pubidml.fontmetrics' has no attribute 'measure'`.

- [ ] **Step 3: Write the implementation**

Add to `pubidml/fontmetrics.py` (and add `from dataclasses import dataclass` to the imports):

```python
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
BAKED: Dict[str, Tuple[float, float, float]] = {}


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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_fontmetrics -v`
Expected: PASS, 26 tests.

- [ ] **Step 5: Commit**

```bash
git add pubidml/fontmetrics.py tests/test_fontmetrics.py
git commit -m "Answer for a string with the font, a baked average, or neither"
```

---

### Task 5: Read the stretch flag, and stop sizing while parsing

**Files:**
- Modify: `pubidml/pubfile.py:287-306` (delete the two band constants), `pubidml/pubfile.py:1085-1106` (delete `_band_size`), `pubidml/pubfile.py:225-235` (add the stretch bit), `pubidml/pubfile.py:377-386` (the dataclass), `pubidml/pubfile.py:1158-1178` (construction)
- Test: `tests/test_pubfile.py`

**Interfaces:**
- Produces: `pubfile.WordArt.stretch: bool`; `WordArt.size` stays `Optional[float]` and is now `None` whenever the file states no size; `WordArt.fitted` keeps its meaning (`size is None` in the file) and is what `convert` reports on.
- Removed: `pubfile._band_size`, `pubfile._BAND_INK_PER_EM`, `pubfile._BAND_EM_PER_GLYPH`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pubfile.py`, and extend the existing `wordart_bools` helper's `bits` dict with `"stretch": 0x0A`:

```python
class WordArtStretchTest(unittest.TestCase):
    """The flag that says the glyphs are fitted to the shape.

    All 48 WordArt shapes in the corpus state it and all 48 have it set,
    which is what lets a stated point size be treated as a floor rather
    than the truth.
    """

    def one(self, **kwargs):
        return pubfile._wordart_shapes(wordart_shape(**kwargs))[0]

    def test_the_stretch_flag_is_read(self):
        self.assertTrue(self.one(bools=wordart_bools(stretch=True)).stretch)

    def test_a_shape_that_does_not_state_stretch_is_not_stretched(self):
        self.assertFalse(self.one(bools=wordart_bools(bold=True)).stretch)

    def test_a_shape_stating_stretch_false_is_not_stretched(self):
        self.assertFalse(self.one(bools=wordart_bools(stretch=False)).stretch)


class WordArtStatedSizeTest(unittest.TestCase):
    """What the file states, and nothing worked out from the band."""

    def test_a_stated_size_is_reported_as_stated(self):
        art = pubfile._wordart_shapes(wordart_shape(size=20.0))[0]
        self.assertEqual(art.size, 20.0)
        self.assertFalse(art.fitted)

    def test_a_shape_with_no_stated_size_reports_none(self):
        # Sizing needs the font, which the parser has no business reading.
        art = pubfile._wordart_shapes(wordart_shape(size=None))[0]
        self.assertIsNone(art.size)
        self.assertTrue(art.fitted)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_pubfile.WordArtStretchTest tests.test_pubfile.WordArtStatedSizeTest -v`
Expected: FAIL — `AttributeError: 'WordArt' object has no attribute 'stretch'`, and the size test fails because `_band_size` currently fills it in.

- [ ] **Step 3: Write the implementation**

In `pubidml/pubfile.py`, extend the boolean constants near line 235:

```python
_WORDART_ITALIC, _WORDART_BOLD = 0x04, 0x05
# Bit 10 is property 0xF5, gtextFStretch: the file saying the glyphs are
# stretched to the shape rather than set at a size and left there. All 48
# WordArt shapes in the corpus state it and all 48 have it set, which is
# what authorises `convert` to treat a stated point size as a floor.
_WORDART_STRETCH = 0x0A
```

Add the field to the `WordArt` dataclass after `fitted`:

```python
    #: True when the file says the glyphs are stretched to the shape, which
    #: is what makes the band the size rather than the stated point size.
    stretch: bool = False
```

Set it in the constructor call around line 1169:

```python
                fitted=fitted,
                stretch=_wordart_boolean(flags, _WORDART_STRETCH),
```

Change the size line to state only what the file states:

```python
        fitted = size is None
        found.append(
            WordArt(
                text=text,
                font=font,
                # What the file states, and nothing more: working a size
                # back from the band needs the font the words are set in,
                # which is `convert`'s to read, not the parser's.
                size=size,
```

Delete `_band_size` (lines 1085-1106) and the `_BAND_INK_PER_EM` / `_BAND_EM_PER_GLYPH` block with its comment (lines 287-306). Delete the now-unused `import re` only if nothing else in the file uses it — check with `grep -n "re\." pubidml/pubfile.py` first.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_pubfile -v`
Expected: The two new classes PASS. Existing tests that assert a band-derived size will FAIL — those assertions move to `test_convert.py` in Task 6. Update them now to assert `size is None` and `fitted is True`, and note in the commit that the sizing assertions have moved.

Run: `make test`
Expected: `test_convert.py` failures about WordArt sizes, which Task 6 fixes. Do not proceed past Task 6 leaving these red.

- [ ] **Step 5: Commit**

```bash
git add pubidml/pubfile.py tests/test_pubfile.py
git commit -m "Say what the file states about a headline, and leave sizing to the conversion"
```

---

### Task 6: Size, condense and space the headline

**Files:**
- Modify: `pubidml/convert.py:1298-1308` (the em-per-advance constant), `pubidml/convert.py:1465-1475` (`_wordart_tracking`), `pubidml/convert.py:1478-1588` (`_wordart_frame`)
- Modify: `pubidml/model.py:216-228` (duplicate field)
- Test: `tests/test_convert.py`

**Interfaces:**
- Consumes: `fontmetrics.measure`, `fontmetrics.Metrics`, `pubfile.WordArt.stretch`.
- Produces: `convert._wordart_metrics(art, line, measure) -> fontmetrics.Metrics`; `convert._wordart_fit(art, lines, measure) -> Tuple[float, Optional[float], str]` returning `(size_pt, horizontal_scale_or_None, source)`; `convert._wordart_tracking(spacing, mean_advance_per_em) -> Optional[float]`; `_wordart_frame(paths, art, page_width, page_height, measure=fontmetrics.measure)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_convert.py`:

```python
def fake_measure(table, default=(0.70, 0.55, 0.50, "average")):
    """A stand-in for fontmetrics.measure keyed by the string measured.

    Sizing is tested against numbers the test states, so it needs no font
    installed -- which matters, because the two faces 46 of the corpus's
    headlines use are not installed on every machine.
    """
    def measure(family, bold, italic, text):
        ink, width_per_glyph, advance, source = table.get(text, default)
        return fontmetrics.Metrics(
            ink_per_em=ink,
            width_per_em=width_per_glyph * max(len(text), 1),
            mean_advance_per_em=advance,
            source=source,
        )
    return measure


class WordArtFitTest(unittest.TestCase):
    """The size and condensation a band and a font decide between them."""

    def art(self, **kwargs):
        fields = dict(
            text="Kerkbode", font="Kerk Display", width=200.0, height=40.0,
            size=None, fitted=True, stretch=True,
        )
        fields.update(kwargs)
        return pubfile.WordArt(**fields)

    def test_the_size_fills_the_band_s_height(self):
        # One line, 40 pt of band, a font inking 0.8 of its em: 50 pt.
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, _scale, _source = convert._wordart_fit(
            self.art(), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 50.0)

    def test_each_line_gets_its_own_share_of_the_band(self):
        # Two lines in the same 40 pt band is 20 pt each.
        measure = fake_measure({
            "Kerk": (0.80, 0.50, 0.50, "font"),
            "bode": (0.80, 0.50, 0.50, "font"),
        })
        size, _scale, _source = convert._wordart_fit(
            self.art(text="Kerk\rbode"), ["Kerk", "bode"], measure
        )
        self.assertAlmostEqual(size, 25.0)

    def test_the_tallest_line_binds_the_size(self):
        # A line with descenders inks more of its em, so it decides.
        measure = fake_measure({
            "Kerk": (0.80, 0.50, 0.50, "font"),
            "bygy": (1.00, 0.50, 0.50, "font"),
        })
        size, _scale, _source = convert._wordart_fit(
            self.art(text="Kerk\rbygy"), ["Kerk", "bygy"], measure
        )
        self.assertAlmostEqual(size, 20.0)

    def test_the_width_becomes_a_horizontal_scale_not_a_smaller_size(self):
        # 8 glyphs at 0.5 em is 4 em; at 50 pt that sets 200 pt wide, in a
        # 200 pt band, so nothing is condensed.
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, scale, _source = convert._wordart_fit(
            self.art(), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 50.0)
        self.assertAlmostEqual(scale, 100.0)

    def test_a_headline_wider_than_its_band_is_condensed(self):
        # Same headline in a 100 pt band: it has to set at half the width.
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, scale, _source = convert._wordart_fit(
            self.art(width=100.0), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 50.0)
        self.assertAlmostEqual(scale, 50.0)

    def test_the_scale_is_clamped(self):
        measure = fake_measure({"I": (0.80, 0.10, 0.10, "font")})
        _size, scale, _source = convert._wordart_fit(
            self.art(text="I", width=400.0), ["I"], measure
        )
        self.assertAlmostEqual(scale, convert._MAX_HORIZONTAL_SCALE)

    def test_spacing_widens_the_headline_before_it_is_fitted(self):
        # WordArt's multiple scales every advance, so a loose headline sets
        # wider and is condensed harder to reach the same band.
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        _size, scale, _source = convert._wordart_fit(
            self.art(spacing=1.2), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(scale, 100.0 / 1.2)

    def test_a_stated_size_is_overridden_when_the_shape_stretches(self):
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, _scale, _source = convert._wordart_fit(
            self.art(size=20.0, fitted=False), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 50.0)

    def test_a_stated_size_stands_when_the_shape_does_not_stretch(self):
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, scale, _source = convert._wordart_fit(
            self.art(size=20.0, fitted=False, stretch=False), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 20.0)
        self.assertIsNone(scale)

    def test_metrics_that_are_not_exact_take_no_scale(self):
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "table")})
        _size, scale, source = convert._wordart_fit(
            self.art(), ["Kerkbode"], measure
        )
        self.assertIsNone(scale)
        self.assertEqual(source, "table")

    def test_without_exact_metrics_the_width_binds_the_size_as_before(self):
        # Today's rule exactly: min(height share / ink, band / natural width).
        # 8 glyphs at 0.55 em is 4.4 em; 100 pt / 4.4 em is 22.7 pt, which
        # is smaller than the 50 pt the height alone would give.
        measure = fake_measure({"Kerkbode": (0.80, 0.55, 0.50, "average")})
        size, scale, _source = convert._wordart_fit(
            self.art(width=100.0), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 100.0 / (0.55 * 8))
        self.assertIsNone(scale)


class WordArtTrackingTest(unittest.TestCase):
    def test_normal_spacing_states_no_tracking(self):
        self.assertIsNone(convert._wordart_tracking(None, 0.5))
        self.assertIsNone(convert._wordart_tracking(1.0, 0.5))

    def test_tracking_uses_the_measured_advance(self):
        # Loose (1.2) against a real mean advance of 0.44 em, not a guess
        # of 0.5: 0.2 x 0.44 x 1000.
        self.assertEqual(convert._wordart_tracking(1.2, 0.44), 88.0)

    def test_the_old_constant_is_what_an_unmeasured_font_still_gets(self):
        self.assertEqual(convert._wordart_tracking(1.2, 0.50), 100.0)
```

Add `from pubidml import fontmetrics` to the imports of `tests/test_convert.py` if it is not already there.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_convert.WordArtFitTest -v`
Expected: FAIL with `AttributeError: module 'pubidml.convert' has no attribute '_wordart_fit'`.

- [ ] **Step 3: Write the implementation**

In `pubidml/convert.py`, add `from pubidml import fontmetrics` to the imports. Replace the `_EM_PER_ADVANCE` block (lines 1298-1308) with:

```python
# How far a headline may be condensed or stretched before the result is
# worse than not condensing it. Publisher will squeeze glyphs hard, but a
# ratio this far out means the metrics are wrong -- a substituted font, or
# a band that is not the one these words were drawn in -- and an
# unreadable smear is a worse answer than a headline that overflows.
_MIN_HORIZONTAL_SCALE, _MAX_HORIZONTAL_SCALE = 25.0, 400.0
```

Replace `_wordart_tracking` (lines 1465-1475) with:

```python
def _wordart_tracking(
    spacing: Optional[float], mean_advance_per_em: float
) -> Optional[float]:
    """WordArt's spacing multiple as the tracking IDML would write.

    The multiple scales each glyph's advance; IDML states the space added,
    in thousandths of an em. An advance is not an em, so the conversion
    needs to know how wide this font's glyphs actually are -- which used
    to be a flat half-em for every font, and is now measured. Times New
    Roman averages 0.44 of an em across a headline and Arial Black 0.61,
    so the guess was out by a quarter either way.

    Rounded to whole thousandths, because the multiple arrives as 16.16
    fixed point and Publisher's Loose reads 1.2001 rather than 1.2.
    """
    if spacing is None or abs(spacing - 1.0) < 0.001:
        return None
    return float(round((spacing - 1.0) * mean_advance_per_em * 1000.0))
```

Add the fitting function just above `_wordart_frame`:

```python
def _wordart_fit(
    art: "pubfile.WordArt", lines: List[str], measure
) -> tuple:
    """The point size and horizontal scale a headline is drawn at.

    WordArt sets the words and stretches them to the shape, so the band is
    not a box the words sit inside -- it *is* the words. Two things follow.

    The height decides the size: a band holds as many lines as the words
    are set on, so each line gets its share, and the line that inks most of
    its em is the one that must fit. The width decides the condensation
    rather than the size, because IDML can state that directly. That is the
    whole of what WordArt's stretch does, and it is why a stated point size
    is a floor rather than the truth -- the *Meditatie* headline states 20
    and Publisher draws it at about 38.

    A size worked out this way is only as good as the metrics behind it, so
    a font this machine cannot read takes the older, safer rule instead:
    the smaller of what the height allows and what the width allows, and no
    condensation at all. Condensing by a ratio derived from a guessed width
    would state a precision that is not there.
    """
    measured = [measure(art.font, art.bold, art.italic, line) for line in lines]
    spacing = art.spacing or 1.0
    per_line = art.height / max(len(lines), 1)
    ink = max((m.ink_per_em for m in measured), default=0.0)
    widest = max((m.width_per_em for m in measured), default=0.0) * spacing
    exact = all(m.exact for m in measured)
    source = measured[0].source if measured else "average"

    # A shape that does not state the stretch flag was set at a size and
    # left there, so the file's word is final. Nothing in the corpus is
    # one of these, but the flag is what says so rather than an assumption.
    if art.size is not None and not art.stretch:
        return art.size, None, source

    by_height = per_line / ink if ink > 0 else art.size or per_line
    if not exact or widest <= 0:
        by_width = art.width / widest if widest > 0 else by_height
        return min(by_height, by_width), None, source

    scale = art.width / (widest * by_height) * 100.0
    return (
        by_height,
        min(max(scale, _MIN_HORIZONTAL_SCALE), _MAX_HORIZONTAL_SCALE),
        source,
    )
```

Change `_wordart_frame`'s signature to take the measurer, so tests can drive it:

```python
def _wordart_frame(
    paths: List[model.Path],
    art: "pubfile.WordArt",
    page_width: float,
    page_height: float,
    measure=fontmetrics.measure,
) -> model.TextFrame:
```

Inside it, after `lines = re.split(...)`, add:

```python
    size, scale, source = _wordart_fit(art, lines, measure)
    tracking = _wordart_tracking(
        art.spacing,
        measure(art.font, art.bold, art.italic, art.text).mean_advance_per_em,
    )
    art.applied_source = source
```

and in the `model.Span(...)` call replace `size_pt=art.size` with `size_pt=size`, replace `tracking=_wordart_tracking(art.spacing)` with `tracking=tracking`, and add `horizontal_scale=scale`.

Add the field `applied_source: str = "average"` to `pubfile.WordArt` so the report in Task 8 can count tiers without measuring twice.

In `pubidml/model.py`, delete the second `tracking` declaration (lines 225-228) and the second `self.tracking` entry in `format_key` (line 247). The field is declared twice, so the second silently shadows the first and the key counts it twice.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_convert -v`
Expected: PASS, including the new classes. Existing WordArt frame tests may need their expected sizes updated — with the fake measurer absent they run on tier 3, which reproduces today's numbers, so any that still fail are genuine and must be understood, not just renumbered.

Run: `make test`
Expected: PASS, whole suite green.

- [ ] **Step 5: Commit**

```bash
git add pubidml/convert.py pubidml/model.py tests/test_convert.py pubidml/pubfile.py
git commit -m "Stretch a headline to its band the way WordArt does"
```

---

### Task 7: A probe that prints a baked entry

**Files:**
- Create: `research/font_metrics.py`

**Interfaces:**
- Consumes: `fontmetrics.find_face`, `Face.measure`.
- Produces: a script, not an import target. No test — `research/` holds probes, none of which are tested.

- [ ] **Step 1: Write the script**

```python
"""What a face measures, as a line for fontmetrics.BAKED.

A machine without a font cannot measure it, and the two faces 46 of the
corpus's 48 headlines are set in -- Monotype Corsiva and Pristina -- ship
with Office rather than with either operating system. Run this where the
font is installed and paste the line it prints into `fontmetrics.BAKED`,
so a machine without the font gets that font's own proportions rather
than an average of every headline face.

    python3 -m research.font_metrics "Monotype Corsiva"
    python3 -m research.font_metrics "Pristina" --bold
"""
import argparse

from pubidml import fontmetrics

# A spread of headline words rather than one, so the average is not
# decided by whichever letters happen to be in a single example. These are
# the corpus's own headlines.
_SAMPLES = (
    "Kerkdiensten", "Meditatie", "Verjaardagen", "Uit de gemeente",
    "Schoonmaakrooster", "Activiteitenagenda", "Kerkelijke stand",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("family")
    parser.add_argument("--bold", action="store_true")
    parser.add_argument("--italic", action="store_true")
    args = parser.parse_args()

    face = fontmetrics.find_face(args.family, args.bold, args.italic)
    if face is None:
        print(f"{args.family!r} is not installed on this machine")
        return 1

    inks, advances, per_glyph = [], [], []
    for sample in _SAMPLES:
        found = face.measure(sample)
        if not found:
            continue
        ink, width, advance = found
        if ink:
            inks.append(ink)
        advances.append(advance)
        per_glyph.append(width / len(sample))
    if not advances:
        print(f"{args.family!r} covers none of the sample words")
        return 1

    print(f"# {face.family} {face.subfamily}, measured over "
          f"{len(advances)} headline(s)")
    print(f'    "{args.family.casefold()}": '
          f"({sum(inks) / len(inks):.3f}, "
          f"{sum(advances) / len(advances):.3f}, "
          f"{sum(per_glyph) / len(per_glyph):.3f}),")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it against a font that is installed here**

Run: `python3 -m research.font_metrics "Comic Sans MS"`
Expected: a line like `    "comic sans ms": (0.837, 0.512, 0.512),`

Run: `python3 -m research.font_metrics "Not A Font"`
Expected: `'Not A Font' is not installed on this machine`, exit status 1.

- [ ] **Step 3: Commit**

```bash
git add research/font_metrics.py
git commit -m "Print what a face measures, for a machine that lacks it"
```

---

### Task 8: Rewrite the report

**Files:**
- Modify: `pubidml/convert.py:1218-1271` (the report), `pubidml/convert.py:59-76` (`Result`), `pubidml/cli.py:286-292` (the detail line), `pubidml/cli.py:31` and `:326` (the CSV columns)
- Test: `tests/test_convert.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `WordArt.applied_source`, `WordArt.warp`, `_wordart_names`.
- Produces: `convert.Result.wordart: int`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_convert.py`:

```python
class WordArtReportTest(unittest.TestCase):
    """What the report says, which is only what a person must act on."""

    def test_a_clean_recovery_warns_about_nothing(self):
        # Nothing bent, everything measured: the headlines converted, and a
        # conversion that worked is not a warning. This is the whole point
        # of the rewrite -- 15 headlines used to print a paragraph saying
        # four different ways that nothing was wrong.
        document = self.converted(warp=None, source="font")
        self.assertEqual(document.warnings, [])

    def test_a_bent_headline_is_reported_and_named(self):
        document = self.converted(warp="button curve", source="font")
        self.assertEqual(len(document.warnings), 1)
        warning = document.warnings[0]
        self.assertIn("bent", warning)
        self.assertIn("button curve", warning)
        self.assertIn("Kerkbode", warning)

    def test_an_unmeasured_font_is_reported_by_name(self):
        # Naming the font is the actionable part: installing it is the fix.
        document = self.converted(warp=None, source="average")
        self.assertEqual(len(document.warnings), 1)
        self.assertIn("Kerk Display", document.warnings[0])

    def test_a_baked_font_is_reported_as_not_measured(self):
        document = self.converted(warp=None, source="table")
        self.assertEqual(len(document.warnings), 1)

    def test_the_count_reaches_the_result_rather_than_the_warnings(self):
        result = self.result(warp=None, source="font")
        self.assertEqual(result.wordart, 1)
        self.assertEqual(result.warnings, [])
```

Write `self.converted` / `self.result` as helpers on the test class that build a one-page `model.Document` with one recovered WordArt frame and run `convert._recover_wordart` over it, following the construction the existing WordArt tests in this file already use.

Add to `tests/test_cli.py`:

```python
    def test_the_detail_line_counts_recovered_headlines(self):
        result = convert.Result(source=Path("x.pub"), pages=1, wordart=15)
        printed = self.printed(result)
        self.assertIn("15 wordart", printed)

    def test_a_file_with_no_headlines_says_nothing_about_them(self):
        result = convert.Result(source=Path("x.pub"), pages=1, wordart=0)
        self.assertNotIn("wordart", self.printed(result))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_convert.WordArtReportTest tests.test_cli -v`
Expected: FAIL — `Result` has no `wordart`, and the current report emits a warning for a clean recovery.

- [ ] **Step 3: Write the implementation**

Replace `convert.py:1218-1271` with:

```python
    recovered = len(groups)
    document.wordart += recovered
    placed = set(groups)

    # Only what a person has to act on reaches the report. A headline that
    # was measured, set straight because the file never bent it, and had
    # its duplicate paint dropped is a converted headline, not a warning --
    # and fifteen of those used to print a paragraph explaining, four
    # different ways, that nothing had gone wrong. The count goes in the
    # detail line instead, beside the frames and the images.
    bent = [by_id[art_id] for art_id in groups if by_id[art_id].warp]
    if bent:
        shapes = ", ".join(sorted({art.warp for art in bent}))
        document.warnings.append(
            f"{len(bent)} WordArt headline(s) bent into a shape IDML cannot "
            f"state ({shapes}); straight text in the band is all that comes "
            f"across — redraw {_wordart_names(bent)}"
        )

    # A font this machine cannot read leaves the headline sized from
    # averages rather than from its own proportions. Naming the font is the
    # actionable part: installing it is the fix.
    estimated = [
        by_id[art_id] for art_id in groups
        if by_id[art_id].applied_source != "font"
    ]
    if estimated:
        fonts = sorted({art.font or "an unnamed font" for art in estimated})
        document.warnings.append(
            f"{len(estimated)} WordArt headline(s) sized from averages: "
            f"{', '.join(repr(f) for f in fonts)} could not be measured on "
            f"this machine, so their size and letter-spacing are close "
            f"rather than exact — install the font and convert again"
        )
```

Add `wordart: int = 0` to `model.Document` and to `convert.Result`, and carry it across wherever the other counts are copied from document to result (find with `grep -n "text_frames=" pubidml/convert.py`).

In `pubidml/cli.py`, extend the detail line:

```python
    detail = (
        f"{result.pages}p {result.text_frames} frames "
        f"{result.images} images {result.characters} chars"
    )
    if result.wordart:
        detail += f" {result.wordart} wordart"
```

Add `"wordart"` to `REPORT_COLUMNS` at `cli.py:31` and `result.wordart` to the CSV row at `cli.py:326`, in the same position.

- [ ] **Step 4: Run the tests**

Run: `make test`
Expected: PASS.

Note: `Result.needs_review` is `bool(self.warnings) or ...`, so files whose only warning was the old WordArt paragraph now report `ok` rather than `review`. That is the intent — a correct conversion should not ask for review — and it will change the status column for `1336 kerkbode.pub` in the CSV.

- [ ] **Step 5: Commit**

```bash
git add pubidml/convert.py pubidml/cli.py pubidml/model.py tests/
git commit -m "Say only what a person has to act on about a headline"
```

---

### Task 9: Convert the corpus and update README

**Files:**
- Modify: `README.md:490-527` (the WordArt sizing paragraphs)

- [ ] **Step 1: Convert the corpus**

Run:

```bash
python3 pub2idml.py files -o converted 2>&1 | tail -40
```

Expected: every file `ok` or `review`; no tracebacks. `1336 kerkbode.pub` should show `15 wordart` in its detail line.

- [ ] **Step 2: Record what the change did**

Run:

```bash
python3 - <<'PY'
import pathlib, re
from pubidml import pubfile, convert, fontmetrics
for name in ("files/cgk/1336 kerkbode.pub",):
    structure = pubfile.read_structure(pathlib.Path(name))
    for art in structure.wordart:
        lines = re.split(r"\r\n|\r|\n", art.text)
        size, scale, source = convert._wordart_fit(
            art, lines, fontmetrics.measure
        )
        words = " ".join(art.text.split())[:22]
        stated = f"{art.size:.0f}" if art.size else "--"
        shown = f"{scale:.0f}%" if scale else "--"
        print(f"{words:24} stated={stated:>3}  now={size:6.1f}  "
              f"scale={shown:>6}  {source}")
PY
```

Paste this table into the commit message. It is the record of what changed and by how much, and the *Meditatie* row is the one to check against README's "stated at 20 pt and drawn at about 38".

- [ ] **Step 3: Update README**

Replace the paragraph beginning "**A shape that states no point size is sized to fill its band**" and the one beginning "What is *not* done is override a size the file does state" (`README.md:502-524`) with an account of what the code now does: the font is read where the machine has it; the size comes from the band's per-line height against the font's real ink; the width becomes `HorizontalScale` rather than a smaller size; a stated size is overridden where the shape states the stretch flag, which all 48 corpus shapes do; and a font that cannot be read falls back to the averages, which the report names. Keep README's habit of giving the evidence — the measured figures from Step 2 are that evidence.

Also update the sentence in the "not done" list that says a filled path of disconnected edges is reported, if the report's wording changed for it.

- [ ] **Step 4: Open the output**

Open `converted/1336 kerkbode.idml` in Affinity Publisher alongside Publisher's own rendering of `files/cgk/1336 kerkbode.pub`, and check the headlines land in their bands at the size Publisher drew them. This is the verification pass README asks for and the only ground truth that exists. Record what you find in the commit message; if a headline is wrong, that is a bug in the rule, not a reason to adjust a constant until it looks right.

- [ ] **Step 5: Commit**

```bash
git add README.md converted
git commit -m "Read the font a headline is set in, and stretch it as Publisher does"
```

---

## Self-Review

**Spec coverage.** Section 1 (what gets measured) → Tasks 1, 2. Section 2 (sizing rule, `HorizontalScale`, stretch flag, stated size as sanity bound, fallback) → Tasks 5, 6. Section 3 (discovery, matching, three tiers, baked table, probe) → Tasks 3, 4, 7. Section 4 (report) → Task 8. Section 5 (testing) → tests inside every task, plus Task 9 Step 4 for the manual pass.

**One spec item deliberately dropped:** the spec lists "a computed size wildly off the size the file states" as a reported sanity failure. Task 6 keeps the stated size available but Task 8 does not report on it, because with the stretch flag authorising the override there is no threshold that distinguishes a wrong measurement from a headline Publisher genuinely drew at twice its stated size — *Meditatie* is 20 stated and ~38 drawn, and a rule loose enough to allow that catches nothing. The clamp on `HorizontalScale` is the check that survives, and it is testable. Flagged here rather than silently omitted.

**Placeholder scan:** none. Every code step carries the code.

**Type consistency:** `Metrics(ink_per_em, width_per_em, mean_advance_per_em, source)` is constructed identically in Task 4's implementation and Task 6's `fake_measure`. `_wordart_fit` returns `(size, scale, source)` in Tasks 6 and 9. `find_face(family, bold, italic)` is called with the same signature in Tasks 3, 4 and 7. `BAKED` entries are `(ink, advance, per_glyph)` in Task 4 and are printed in that order by Task 7.
