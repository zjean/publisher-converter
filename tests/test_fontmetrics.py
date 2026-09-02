"""Metrics read out of a font file, and the synthetic fonts to test with.

The builder below writes real sfnt bytes rather than mocking the reader,
the way `test_pubfile.py` builds real Escher records: a reader tested
against a mock only proves the mock agrees with itself.
"""
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def name_table(family, subfamily, platform=3, language=0x0409,
               postscript=None, typo_family=None, typo_subfamily=None):
    # 16 and 17 only where the caller asks for them: a face that needs no
    # split between the two namings leaves them out, and most do.
    entries = [(1, family), (2, subfamily)]
    for nid, value in ((6, postscript), (16, typo_family), (17, typo_subfamily)):
        if value is not None:
            entries.append((nid, value))
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


def hhea_table(num_h_metrics, ascender=800, descender=-200, line_gap=0):
    return (
        b"\x00" * 4
        + struct.pack(">hhh", ascender, descender, line_gap)
        + b"\x00" * 24
        + struct.pack(">H", num_h_metrics)
    )


def os2_table(win_ascent=900, win_descent=300):
    """Only as far as usWinDescent, which is all `_win_metrics` reads."""
    return b"\x00" * 74 + struct.pack(">HH", win_ascent, win_descent) + b"\x00" * 4


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
    # idDelta is declared int16 but applied modulo 65536, so it is written
    # here as the low sixteen bits rather than as a signed number. The
    # terminating segment alone needs it: mapping 0xFFFF to glyph 0 means a
    # delta of -65535, which will not fit in an int16 at all.
    deltas = b"".join(struct.pack(">H", (s[2] - s[0]) & 0xFFFF) for s in segs)
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
               glyphs=None, long_loca=True, cmap_format=4, platform=3,
               postscript=None, typo_family=None, typo_subfamily=None,
               win_ascent=900, win_descent=300, ascender=800, descender=-200,
               line_gap=0, os2=True):
    """glyphs: {char: (advance, (xMin, yMin, xMax, yMax))}, glyph ids from 1.

    `os2=False` builds a face with no OS/2 table at all, which is the shape
    a line's height cannot be read from.
    """
    glyphs = {"A": (600, (50, 0, 550, 700))} if glyphs is None else glyphs
    chars = sorted(glyphs)
    mapping = {ord(c): i + 1 for i, c in enumerate(chars)}
    advances = [0] + [glyphs[c][0] for c in chars]
    boxes = [None] + [glyphs[c][1] for c in chars]
    glyf, loca = glyf_and_loca(boxes, long_loca)
    tables = {
        "head": head_table(upem, long_loca),
        "name": name_table(
            family, subfamily, platform,
            postscript=postscript,
            typo_family=typo_family,
            typo_subfamily=typo_subfamily,
        ),
        "maxp": maxp_table(len(advances)),
        "hhea": hhea_table(len(advances), ascender, descender, line_gap),
        "hmtx": hmtx_table(advances),
        "cmap": cmap_table(mapping, cmap_format),
        "loca": loca,
        "glyf": glyf,
    }
    if os2:
        tables["OS/2"] = os2_table(win_ascent, win_descent)
    return _table_directory(tables)


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
        ink, above, width, advance = self.face().measure("A")
        self.assertAlmostEqual(ink, 0.700)
        self.assertAlmostEqual(above, 0.700)
        self.assertAlmostEqual(width, 0.600)
        self.assertAlmostEqual(advance, 0.600)

    def test_ink_spans_the_tallest_and_deepest_glyph_of_the_string(self):
        # 700 up and 200 down is 900 units of a 1000-unit em. This is the
        # descender case the old 0.70 constant got wrong by construction.
        ink, above, width, advance = self.face().measure("Ay")
        self.assertAlmostEqual(ink, 0.900)
        self.assertAlmostEqual(width, 1.100)
        self.assertAlmostEqual(advance, 0.550)
        # And of those 900 units, 700 are above the baseline. That split is
        # where a headline's baseline goes inside its band, so a descender
        # has to move it rather than only make the ink taller.
        self.assertAlmostEqual(above, 0.700)

    def test_the_baseline_share_follows_the_tallest_glyph_not_the_first(self):
        tall = fontmetrics.faces(build_font(glyphs={
            "a": (500, (20, -100, 480, 500)),
            "l": (300, (40, 0, 260, 900)),
        }))[0]
        ink, above, _width, _advance = tall.measure("al")
        self.assertAlmostEqual(ink, 1.000)
        self.assertAlmostEqual(above, 0.900)

    def test_a_short_loca_font_measures_the_same(self):
        ink, above, _width, _advance = self.face(long_loca=False).measure("A")
        self.assertAlmostEqual(ink, 0.700)
        self.assertAlmostEqual(above, 0.700)

    def test_a_format_12_cmap_is_read(self):
        ink, _above, _width, _advance = self.face(cmap_format=12).measure("A")
        self.assertAlmostEqual(ink, 0.700)

    def test_units_per_em_scales_the_result(self):
        big = fontmetrics.faces(
            build_font(upem=2000, glyphs={"A": (1200, (100, 0, 1100, 1400))})
        )[0]
        ink, above, width, _advance = big.measure("A")
        self.assertAlmostEqual(ink, 0.700)
        self.assertAlmostEqual(above, 0.700)
        self.assertAlmostEqual(width, 0.600)

    def test_a_face_covering_none_of_the_string_measures_nothing(self):
        # Corsiva Hebrew is real and does exactly this: it parses, it has a
        # cmap, and it has no Latin letters at all. Measuring it would put a
        # headline at whatever punctuation happened to match.
        self.assertIsNone(self.face().measure("שלום"))

    def test_characters_the_face_lacks_are_skipped_not_counted(self):
        ink, _above, width, advance = self.face().measure("Aא")
        self.assertAlmostEqual(ink, 0.700)
        self.assertAlmostEqual(width, 0.600)
        self.assertAlmostEqual(advance, 0.600)


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


class LineMetricsTest(unittest.TestCase):
    """One line of a face: what Publisher calls a "space" of line spacing."""

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

    def test_a_line_is_the_win_metrics_added_up(self):
        # GDI's tmHeight, which is what a GDI application stacks lines by.
        self.write("kerk.ttf", family="Kerk", upem=1000,
                   win_ascent=900, win_descent=300)
        metrics = fontmetrics.line_metrics("Kerk", False, False)
        self.assertAlmostEqual(metrics.height, 1.2)
        self.assertAlmostEqual(metrics.ascent, 0.9)

    def test_the_line_gap_the_win_metrics_have_not_taken_up_is_added(self):
        # hhea spans 1000 units, the win metrics 1200, and the gap is 300:
        # 200 of the gap is already inside the win metrics, so 100 is left.
        self.write("kerk.ttf", family="Kerk", upem=1000,
                   win_ascent=900, win_descent=300,
                   ascender=800, descender=-200, line_gap=300)
        metrics = fontmetrics.line_metrics("Kerk", False, False)
        self.assertAlmostEqual(metrics.height, 1.3)

    def test_a_gap_the_win_metrics_already_cover_adds_nothing(self):
        # Calibri's shape: its gap is exactly what its win metrics add over
        # its hhea pair, so GDI reports no external leading at all.
        self.write("kerk.ttf", family="Kerk", upem=1000,
                   win_ascent=900, win_descent=300,
                   ascender=800, descender=-200, line_gap=200)
        metrics = fontmetrics.line_metrics("Kerk", False, False)
        self.assertAlmostEqual(metrics.height, 1.2)

    def test_the_external_leading_never_goes_negative(self):
        self.write("kerk.ttf", family="Kerk", upem=1000,
                   win_ascent=900, win_descent=300,
                   ascender=700, descender=-100, line_gap=0)
        metrics = fontmetrics.line_metrics("Kerk", False, False)
        self.assertAlmostEqual(metrics.height, 1.2)

    def test_a_face_with_no_os2_table_has_no_line_to_read(self):
        # The caller falls back to 120% for these, which is all it can do.
        self.write("kerk.ttf", family="Kerk", os2=False)
        self.assertIsNone(fontmetrics.line_metrics("Kerk", False, False))

    def test_a_font_this_machine_lacks_has_no_line_to_read(self):
        self.write("kerk.ttf", family="Kerk")
        self.assertIsNone(fontmetrics.line_metrics("Pristina", False, False))

    def test_no_font_named_at_all_has_no_line_to_read(self):
        self.assertIsNone(fontmetrics.line_metrics(None, False, False))

    def test_the_bold_face_is_measured_when_bold_is_asked_for(self):
        self.write("plain.ttf", family="Kerk", subfamily="Regular",
                   win_ascent=900, win_descent=300)
        self.write("bold.ttf", family="Kerk", subfamily="Bold",
                   win_ascent=1000, win_descent=300)
        self.assertAlmostEqual(
            fontmetrics.line_metrics("Kerk", True, False).height, 1.3)

    def test_resetting_the_index_forgets_the_measurements_too(self):
        self.write("kerk.ttf", family="Kerk", win_ascent=900, win_descent=300)
        self.assertAlmostEqual(fontmetrics.line_metrics("Kerk", False, False).height, 1.2)
        (self.root / "kerk.ttf").write_bytes(
            build_font(family="Kerk", win_ascent=1100, win_descent=300))
        fontmetrics.reset_index()
        self.assertAlmostEqual(fontmetrics.line_metrics("Kerk", False, False).height, 1.4)


class NamingTest(unittest.TestCase):
    """What to call a face, for a reader that indexes by typographic name.

    The case throughout is Calibri Light, which is a family of its own in
    the legacy naming Publisher writes and the 'Light' style of 'Calibri'
    everywhere else. Passing Publisher's name through is what leaves
    Affinity reporting a font it has installed as missing.
    """

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

    def light(self, subfamily="Regular", typo_subfamily="Light", **kwargs):
        """A face named the way Calibri Light names itself."""
        return dict(
            family="Kerk Light", subfamily=subfamily,
            typo_family="Kerk", typo_subfamily=typo_subfamily,
            postscript="Kerk-Light", **kwargs,
        )

    def test_a_face_states_both_of_its_namings(self):
        self.write("light.ttf", **self.light())
        face = fontmetrics.find_face("Kerk Light", bold=False, italic=False)
        self.assertEqual(face.family, "Kerk Light")
        self.assertEqual(face.typo_family, "Kerk")
        self.assertEqual(face.typo_subfamily, "Light")
        self.assertEqual(face.postscript, "Kerk-Light")

    def test_a_face_that_needs_no_split_states_only_the_legacy_naming(self):
        self.write("plain.ttf", family="Kerk", subfamily="Regular")
        face = fontmetrics.find_face("Kerk", bold=False, italic=False)
        self.assertIsNone(face.typo_family)
        self.assertIsNone(face.typo_subfamily)

    def test_a_folded_family_is_named_by_family_and_style(self):
        self.write("light.ttf", **self.light())
        naming = fontmetrics.naming("Kerk Light", bold=False, italic=False)
        self.assertEqual(naming.family, "Kerk")
        self.assertEqual(naming.style, "Light")
        self.assertEqual(naming.postscript, "Kerk-Light")
        self.assertTrue(naming.folded)

    def test_the_italic_of_a_folded_family_keeps_its_own_style_name(self):
        self.write("light.ttf", **self.light())
        self.write("lightitalic.ttf", **self.light(
            subfamily="Italic", typo_subfamily="Light Italic"
        ))
        naming = fontmetrics.naming("Kerk Light", bold=False, italic=True)
        self.assertEqual((naming.family, naming.style), ("Kerk", "Light Italic"))

    def test_a_family_that_needs_no_folding_is_left_alone(self):
        self.write("plain.ttf", family="Kerk", subfamily="Regular",
                   postscript="Kerk-Regular")
        naming = fontmetrics.naming("Kerk", bold=False, italic=False)
        self.assertEqual((naming.family, naming.style), ("Kerk", "Regular"))
        self.assertEqual(naming.postscript, "Kerk-Regular")
        self.assertFalse(naming.folded)
        self.assertFalse(naming.faux_bold)

    def test_a_font_this_machine_lacks_is_not_named_at_all(self):
        # None means unverifiable, not wrong: the file's own name is very
        # likely right on the machine that has the font.
        self.write("plain.ttf", family="Kerk")
        self.assertIsNone(fontmetrics.naming("Pristina", bold=False, italic=False))

    def test_bold_on_a_folded_weight_is_a_face_that_was_never_drawn(self):
        # Calibri Light Bold is in no Calibri release, so Publisher strokes
        # the outline instead of setting the run in a bold face.
        self.write("light.ttf", **self.light())
        naming = fontmetrics.naming("Kerk Light", bold=True, italic=False)
        self.assertEqual((naming.family, naming.style), ("Kerk", "Light"))
        self.assertTrue(naming.faux_bold)

    def test_a_bold_missing_only_here_is_not_treated_as_undrawn(self):
        # The family did not fold, so the bold is absent from this machine
        # rather than from the design, and saying so is the report's job.
        self.write("plain.ttf", family="Kerk", subfamily="Regular")
        naming = fontmetrics.naming("Kerk", bold=True, italic=False)
        self.assertFalse(naming.faux_bold)

    def test_a_real_bold_of_a_folded_family_is_not_stroked(self):
        self.write("light.ttf", **self.light())
        self.write("bold.ttf", family="Kerk Light", subfamily="Bold",
                   typo_family="Kerk", typo_subfamily="Light Bold")
        naming = fontmetrics.naming("Kerk Light", bold=True, italic=False)
        self.assertEqual(naming.style, "Light Bold")
        self.assertFalse(naming.faux_bold)

    def test_italic_on_a_family_with_none_is_a_slant_to_be_drawn(self):
        # Blackadder ITC, Segoe Script and Mystical Woods each ship one
        # slant, and Publisher's PDF shears all three rather than
        # substituting a face.
        self.write("plain.ttf", family="Kerk Script", subfamily="Regular")
        naming = fontmetrics.naming("Kerk Script", bold=False, italic=True)
        self.assertEqual(naming.style, "Regular")
        self.assertTrue(naming.faux_italic)

    def test_a_family_with_a_real_italic_is_not_sheared(self):
        self.write("plain.ttf", family="Kerk", subfamily="Regular")
        self.write("italic.ttf", family="Kerk", subfamily="Italic")
        naming = fontmetrics.naming("Kerk", bold=False, italic=True)
        self.assertEqual(naming.style, "Italic")
        self.assertFalse(naming.faux_italic)

    def test_an_oblique_counts_as_the_italic_it_is(self):
        self.write("oblique.ttf", family="Kerk", subfamily="Oblique")
        naming = fontmetrics.naming("Kerk", bold=False, italic=True)
        self.assertFalse(naming.faux_italic)

    def test_the_italic_of_a_folded_weight_is_not_sheared_as_well(self):
        self.write("light.ttf", **self.light())
        self.write("lightitalic.ttf", **self.light(
            subfamily="Italic", typo_subfamily="Light Italic"
        ))
        naming = fontmetrics.naming("Kerk Light", bold=False, italic=True)
        self.assertFalse(naming.faux_italic)

    def test_an_upright_run_is_never_sheared(self):
        self.write("plain.ttf", family="Kerk Script", subfamily="Regular")
        naming = fontmetrics.naming("Kerk Script", bold=False, italic=False)
        self.assertFalse(naming.faux_italic)

    def test_a_face_is_found_by_either_of_its_namings(self):
        self.write("light.ttf", **self.light())
        self.assertIsNotNone(fontmetrics.find_face("Kerk Light", False, False))
        self.assertIsNotNone(fontmetrics.find_face("Kerk", False, False))

    def test_asking_by_the_typographic_name_needs_no_renaming(self):
        self.write("light.ttf", **self.light())
        naming = fontmetrics.naming("Kerk", bold=False, italic=False)
        self.assertEqual(naming.family, "Kerk")
        self.assertFalse(naming.folded)

    def test_the_pair_a_font_states_outright_wins_over_a_recovered_one(self):
        # Both files claim the 'Kerk'/'Regular' slot -- one by its own
        # legacy names, one only through its typographic pair. The face
        # that states it is the face that has to answer for it.
        self.write("plain.ttf", family="Kerk", subfamily="Regular",
                   postscript="Kerk-Regular")
        self.write("odd.ttf", family="Kerk Oddity", subfamily="Bold",
                   typo_family="Kerk", typo_subfamily="Regular",
                   postscript="Kerk-Oddity")
        face = fontmetrics.find_face("Kerk", bold=False, italic=False)
        self.assertEqual(face.postscript, "Kerk-Regular")


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
        self.assertAlmostEqual(found.ink_above_baseline_per_em, 0.700)
        self.assertAlmostEqual(found.width_per_em, 1.100)
        self.assertAlmostEqual(found.mean_advance_per_em, 0.550)

    def test_a_baked_family_is_used_when_the_font_is_not_installed(self):
        with mock.patch.dict(
            fontmetrics.BAKED, {"pristina": (0.62, 0.48, 0.41, 0.44)},
            clear=False,
        ):
            found = fontmetrics.measure("Pristina", False, False, "Kerkbode")
        self.assertEqual(found.source, "table")
        self.assertFalse(found.exact)
        self.assertAlmostEqual(found.ink_per_em, 0.62)
        self.assertAlmostEqual(found.ink_above_baseline_per_em, 0.48)
        self.assertAlmostEqual(found.mean_advance_per_em, 0.41)
        self.assertAlmostEqual(found.width_per_em, 0.44 * len("Kerkbode"))

    def test_an_unknown_font_falls_back_to_the_averages(self):
        found = fontmetrics.measure("Nothing Here", False, False, "Kerkbode")
        self.assertEqual(found.source, "average")
        self.assertFalse(found.exact)
        self.assertAlmostEqual(found.ink_per_em, 0.70)
        self.assertAlmostEqual(found.ink_above_baseline_per_em, 0.55)
        self.assertAlmostEqual(found.mean_advance_per_em, 0.50)
        self.assertAlmostEqual(found.width_per_em, 0.55 * len("Kerkbode"))

    def test_a_font_stating_no_name_falls_back_to_the_averages(self):
        found = fontmetrics.measure(None, False, False, "Kerkbode")
        self.assertEqual(found.source, "average")

    def test_a_font_without_the_string_s_glyphs_falls_through(self):
        # Found, parses, states a cmap, covers none of the string.
        (self.root / "hebrew.ttf").write_bytes(build_font(
            family="Corsiva Hebrew", glyphs={"א": (500, (0, 0, 400, 600))}
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
        # The baseline is borrowed with the ink: there are no boxes to find
        # it in either.
        self.assertAlmostEqual(found.ink_above_baseline_per_em, 0.55)


class BakedTableTest(unittest.TestCase):
    """The faces a machine without Office still has to size correctly.

    Monotype Corsiva sets 40 of the corpus's 48 headlines and Pristina 6,
    and both ship with Office rather than with an operating system -- so
    the machine converting a document is quite likely not to have them.
    Emptying this table would silently put those headlines back on the
    global averages.
    """

    def test_the_corpus_headline_faces_are_baked(self):
        for family in ("monotype corsiva", "pristina", "comic sans ms",
                       "arial black"):
            with self.subTest(family=family):
                self.assertIn(family, fontmetrics.BAKED)

    def test_every_baked_entry_is_four_plausible_ratios(self):
        for family, entry in fontmetrics.BAKED.items():
            with self.subTest(family=family):
                ink, above, advance, per_glyph = entry
                # An em of ink is normal for type with both ascenders and
                # descenders; twice an em is a misread table.
                self.assertTrue(0.3 < ink < 2.0, ink)
                self.assertTrue(0.1 < advance < 1.5, advance)
                self.assertTrue(0.1 < per_glyph < 1.5, per_glyph)
                # And the part above the baseline is part of the ink, not
                # a second measurement of the whole of it.
                self.assertTrue(0 < above <= ink, (above, ink))
