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
