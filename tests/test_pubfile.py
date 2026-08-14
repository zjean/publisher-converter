"""Structure read from the .pub itself, and what is done with it.

This is the one place the converter looks at the binary directly rather
than going through libmspub, so the tests care about two things: that it
reads the right thing, and that it never costs a conversion when it
cannot.
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

from pubidml import convert, model, pubfile

REPO = Path(__file__).resolve().parent.parent
SAMPLES = REPO / "files"

needs_samples = unittest.skipUnless(
    (SAMPLES / "MISSAL MARIANA E PEDRO.pub").exists(), "sample .pub files absent"
)


def page_with(*items) -> model.Page:
    page = model.Page(width=612.0, height=792.0)
    page.items.extend(items)
    return page


def frame_saying(text: str) -> model.TextFrame:
    frame = model.TextFrame(x=10.0, y=700.0, width=400.0, height=20.0)
    paragraph = model.Paragraph()
    paragraph.spans.append(model.Span(text=text))
    frame.story.paragraphs.append(paragraph)
    return frame


def structure_for(page_count: int, *, fields: bool, shapes: int = 1):
    master = pubfile.PageStructure(seq=263, is_master=True, shape_count=shapes)
    s = pubfile.FileStructure(masters={263: master}, has_fields=fields)
    for i in range(page_count):
        s.pages.append(pubfile.PageStructure(seq=300 + i, applied_master=263, shape_count=2))
    return s


class RobustnessTest(unittest.TestCase):
    """A file this cannot read must convert exactly as it did before."""

    def test_unreadable_input_returns_none_rather_than_raising(self):
        work = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        for name, payload in (
            ("empty.pub", b""),
            ("garbage.pub", b"not an OLE file at all"),
            ("truncated.pub", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32),
            ("random.pub", bytes(range(256)) * 8),
        ):
            with self.subTest(name=name):
                target = work / name
                target.write_bytes(payload)
                self.assertIsNone(pubfile.read_structure(target))

    def test_a_missing_file_returns_none(self):
        self.assertIsNone(pubfile.read_structure(Path("/nonexistent/nope.pub")))


@needs_samples
class RealFileTest(unittest.TestCase):
    def test_masters_are_found(self):
        expected = {
            "MISSAL MARIANA E PEDRO.pub": (15, 2, True),
            "Cantico_dei_Cantici.pub": (4, 2, False),
            "Bus Meeting Zones & Luggage JLW.pub": (1, 1, True),
            "Blank Note Card (100_1502 Snail) (2 up).pub": (1, 1, False),
        }
        for name, (pages, masters, fields) in expected.items():
            with self.subTest(name=name):
                s = pubfile.read_structure(SAMPLES / name)
                self.assertIsNotNone(s)
                self.assertEqual(len(s.pages), pages)
                self.assertEqual(len(s.masters), masters)
                self.assertEqual(s.has_fields, fields)

    def test_wordart_is_read_out_of_a_real_file(self):
        # The synthetic streams above prove the record layout; this proves
        # the layout is the one Publisher actually writes.
        structure = pubfile.read_structure(SAMPLES / "Cantico_dei_Cantici.pub")
        self.assertIsNotNone(structure)
        found = {art.text for art in structure.wordart}
        self.assertIn("Il Cantico dei Cantici", found)
        art = next(a for a in structure.wordart if a.text == "Il Cantico dei Cantici")
        self.assertEqual(art.font, "Arial Black")
        self.assertAlmostEqual(art.size, 28.0)
        self.assertFalse(art.fitted)

    def test_a_file_with_no_wordart_reports_none(self):
        structure = pubfile.read_structure(SAMPLES / "MISSAL MARIANA E PEDRO.pub")
        self.assertEqual(structure.wordart, [])

    def test_page_count_matches_what_libmspub_emits(self):
        # The whole approach depends on these lining up index by index.
        if not convert.PUBDUMP.exists():
            self.skipTest("pubdump not built")
        for source in sorted(SAMPLES.glob("*.pub")):
            with self.subTest(source=source.name):
                s = pubfile.read_structure(source)
                document = convert.parse_document(source)
                self.assertEqual(len(s.pages), len(document.pages))

    def test_every_page_names_the_master_it_applies(self):
        s = pubfile.read_structure(SAMPLES / "MISSAL MARIANA E PEDRO.pub")
        for index in range(len(s.pages)):
            with self.subTest(page=index):
                self.assertIsNotNone(s.master_for(index))


class BackgroundDetectionTest(unittest.TestCase):
    def test_a_whole_page_rectangle_is_a_background(self):
        page = page_with()
        self.assertTrue(convert._is_page_background(
            model.Rectangle(x=0.0, y=0.0, width=612.0, height=792.0), page))

    def test_ordinary_shapes_are_not(self):
        page = page_with()
        for item in (
            model.Rectangle(x=0.0, y=0.0, width=100.0, height=100.0),
            model.Rectangle(x=50.0, y=50.0, width=612.0, height=792.0),
            model.TextFrame(x=0.0, y=0.0, width=612.0, height=792.0),
        ):
            with self.subTest(item=item):
                self.assertFalse(convert._is_page_background(item, page))

    def test_master_items_step_over_backgrounds(self):
        background = model.Rectangle(x=0.0, y=0.0, width=612.0, height=792.0)
        footer = frame_saying(" #")
        own = model.Rectangle(x=10.0, y=10.0, width=50.0, height=50.0)
        page = page_with(background, footer, own)
        self.assertEqual(convert._master_items(page, 1), [footer])


class PageNumberTest(unittest.TestCase):
    """A footer holding a page-number field stays put and gets numbered.

    It cannot move onto a master: one copy there cannot read 1 on one page
    and 2 on the next. Everything beside it still moves.
    """

    def _document(self, pages: int, text: str = " #") -> model.Document:
        document = model.Document()
        for _ in range(pages):
            document.pages.append(page_with(frame_saying(text),
                                            model.Rectangle(width=10.0, height=10.0)))
        return document

    def footers(self, document):
        return [
            next(i for i in p.items if isinstance(i, model.TextFrame))
            .story.paragraphs[0].spans[0].text
            for p in document.pages
        ]

    def test_the_placeholder_becomes_the_real_page_number(self):
        document = self._document(3)
        convert._apply_master_pages(document, structure_for(3, fields=True))
        self.assertEqual(self.footers(document), [" 1", " 2", " 3"])
        self.assertTrue(any("page-number field" in w for w in document.warnings))

    def test_the_numbered_frame_is_not_moved_onto_a_master(self):
        document = self._document(3)
        convert._apply_master_pages(document, structure_for(3, fields=True))
        self.assertEqual(document.masters, [])
        for page in document.pages:
            self.assertIsNone(page.master)

    def test_without_a_field_table_the_hash_is_ordinary_master_content(self):
        # No TOKN chunk means the '#' was typed, so the frame is repeated
        # content like any other and belongs on the master, text intact.
        document = self._document(3)
        convert._apply_master_pages(document, structure_for(3, fields=False))
        self.assertEqual(len(document.masters), 1)
        text = document.masters[0].items[0].story.paragraphs[0].spans[0].text
        self.assertEqual(text, " #")
        for page in document.pages:
            self.assertEqual(page.master, "A")

    def test_no_structure_at_all_changes_nothing(self):
        document = self._document(2)
        convert._apply_master_pages(document, None)
        self.assertEqual(self.footers(document), [" #", " #"])
        self.assertEqual(document.masters, [])

    def test_a_page_count_mismatch_changes_nothing(self):
        # Nothing can be attributed if the two halves cannot be lined up.
        document = self._document(3)
        convert._apply_master_pages(document, structure_for(5, fields=True))
        self.assertEqual(self.footers(document), [" #", " #", " #"])
        self.assertEqual(document.masters, [])
        self.assertEqual(document.warnings, [])

    def test_a_hash_outside_master_content_is_left_alone(self):
        document = model.Document()
        for _ in range(2):
            document.pages.append(page_with(frame_saying("nothing here"),
                                            frame_saying("Suite #3")))
        convert._apply_master_pages(document, structure_for(2, fields=True, shapes=1))
        remaining = [
            p.items[-1].story.paragraphs[0].spans[0].text for p in document.pages
        ]
        self.assertEqual(remaining, ["Suite #3", "Suite #3"])

    def test_surrounding_text_survives(self):
        document = self._document(2, text="Page # of many")
        convert._apply_master_pages(document, structure_for(2, fields=True))
        self.assertEqual(self.footers(document), ["Page 1 of many", "Page 2 of many"])


class MasterExtractionTest(unittest.TestCase):
    """Content Publisher held once should end up stored once."""

    def _document(self, pages: int, master_text: str = "Running header"):
        document = model.Document()
        for _ in range(pages):
            header = frame_saying(master_text)
            rule = model.Rectangle(x=10.0, y=730.0, width=400.0, height=2.0)
            own = model.TextFrame(x=50.0, y=100.0, width=200.0, height=50.0)
            document.pages.append(page_with(header, rule, own))
        return document

    def test_repeated_content_moves_onto_one_master(self):
        document = self._document(3)
        convert._apply_master_pages(document, structure_for(3, fields=False, shapes=2))
        self.assertEqual(len(document.masters), 1)
        self.assertEqual(len(document.masters[0].items), 2)
        for page in document.pages:
            self.assertEqual(page.master, "A")
            self.assertEqual(len(page.items), 1, "only the page's own item should remain")
        self.assertTrue(any("master page" in w for w in document.warnings))

    def test_the_master_takes_the_page_size(self):
        document = self._document(2)
        convert._apply_master_pages(document, structure_for(2, fields=False, shapes=2))
        self.assertAlmostEqual(document.masters[0].width, 612.0)
        self.assertAlmostEqual(document.masters[0].height, 792.0)

    def test_a_page_number_frame_stays_on_the_page(self):
        # One copy on a master cannot read 1 on one page and 2 on the next.
        document = self._document(3, master_text="Page #")
        convert._apply_master_pages(document, structure_for(3, fields=True, shapes=2))
        kept = [p.items[0].story.paragraphs[0].spans[0].text for p in document.pages]
        self.assertEqual(kept, ["Page 1", "Page 2", "Page 3"])
        # The rule beside it has no field, so it still moves.
        self.assertEqual(len(document.masters), 1)
        self.assertEqual(len(document.masters[0].items), 1)
        for page in document.pages:
            self.assertEqual(len(page.items), 2)

    def test_a_facing_pair_becomes_two_masters(self):
        # One Publisher master can cover a left and a right page. They look
        # different, so they become two IDML masters rather than blocking
        # extraction entirely.
        document = self._document(4)
        for even in (1, 3):
            document.pages[even].items[1].x = 200.0
        convert._apply_master_pages(document, structure_for(4, fields=False, shapes=2))
        self.assertEqual(len(document.masters), 2)
        self.assertEqual([p.master for p in document.pages], ["A", "B", "A", "B"])

    def test_more_than_two_layouts_per_master_moves_nothing(self):
        # A Publisher master covers at most a facing pair, so three
        # different layouts means the attribution is wrong.
        document = self._document(3)
        document.pages[1].items[1].x = 200.0
        document.pages[2].items[1].x = 400.0
        convert._apply_master_pages(document, structure_for(3, fields=False, shapes=2))
        self.assertEqual(document.masters, [])
        for page in document.pages:
            self.assertIsNone(page.master)
            self.assertEqual(len(page.items), 3)

    def test_pages_of_different_sizes_get_their_own_master(self):
        # The sheet is part of a master's identity, so content is never
        # lifted onto a page of the wrong dimensions.
        document = self._document(2)
        document.pages[1].width = 400.0
        convert._apply_master_pages(document, structure_for(2, fields=False, shapes=2))
        self.assertEqual(len(document.masters), 2)
        self.assertAlmostEqual(document.masters[0].width, 612.0)
        self.assertAlmostEqual(document.masters[1].width, 400.0)

    def test_resources_reach_the_document_through_a_master(self):
        document = model.Document()
        master = model.Master(name="A")
        frame = frame_saying("x")
        frame.story.paragraphs[0].spans[0].font = "Master Only Font"
        frame.story.paragraphs[0].spans[0].color = (1, 2, 3)
        master.items.append(frame)
        document.masters.append(master)
        self.assertIn("Master Only Font", document.fonts)
        self.assertIn((1, 2, 3), document.colors)


def _block(id_: int, type_: int, payload: bytes = b"") -> bytes:
    return bytes([id_, type_]) + payload


def _u32(id_: int, value: int) -> bytes:
    return _block(id_, 0x20, struct.pack("<I", value))


def _container(id_: int, type_: int, children) -> bytes:
    body = b"".join(children)
    # A container's length counts itself, so the payload opens with 4 + body.
    return _block(id_, type_, struct.pack("<I", len(body) + 4) + body)


def _chunk(children) -> bytes:
    body = b"".join(children)
    return struct.pack("<I", len(body) + 4) + body


EMU_PER_POINT = 12700


def table_chunk(column_widths, row_heights, cells_seqnum: int) -> bytes:
    """A TABLE chunk, measured in points and written out in EMU."""
    sizes = [round(v * EMU_PER_POINT) for v in list(column_widths) + list(row_heights)]
    running, entries = 0, []
    for size in sizes:
        running += size
        entries.append(_container(0x00, 0x88, [_u32(0x01, running), _u32(0x02, size)]))
    return _chunk([
        _u32(0x66, len(row_heights)),
        _u32(0x67, len(column_widths)),
        _block(0x6B, 0x70, struct.pack("<I", cells_seqnum)),
        _container(0x6D, 0x90, entries),
    ])


def cells_chunk(*cells) -> bytes:
    """A CELLS chunk. Each cell is (row, column, {side id: EMU})."""
    records = []
    for row, column, insets in cells:
        fields = [_u32(0x01, row), _u32(0x02, row), _u32(0x03, column), _u32(0x04, column)]
        fields += [_u32(side, value) for side, value in sorted(insets.items())]
        records.append(_container(0x00, 0x88, fields))
    return _chunk([
        _block(0x01, 0x18, struct.pack("<H", len(cells))),
        _container(0x02, 0xA0, records),
    ])


class CellInsetReadingTest(unittest.TestCase):
    """The part of a cell record libmspub leaves marked as a question."""

    def read(self, *chunks):
        """Lay chunks out in one buffer and read them as the file's tables."""
        contents, refs = b"\x00" * 8, []
        for seq, (kind, payload) in enumerate(chunks):
            refs.append((seq, kind, len(contents)))
            contents += payload
        return pubfile._read_tables(contents, refs)

    def one_table(self, *cells, widths=(72.0, 36.0), heights=(18.0,)):
        tables = self.read(
            (0x10, table_chunk(widths, heights, cells_seqnum=1)),
            (0x63, cells_chunk(*cells)),
        )
        signature = pubfile.table_signature(list(widths), list(heights))
        self.assertIn(signature, tables)
        return tables[signature]

    def test_the_four_insets_arrive_in_points(self):
        # 36576 EMU is 0.04in, Publisher's own default, on all four sides.
        table = self.one_table((0, 0, {0x0A: 36576, 0x0B: 36576, 0x0C: 36576, 0x0D: 36576}))
        self.assertEqual(
            [round(v, 4) for v in table.insets[(0, 0)]], [2.88, 2.88, 2.88, 2.88]
        )

    def test_the_sides_are_read_as_left_top_right_bottom(self):
        table = self.one_table((0, 0, {0x0A: 12700, 0x0B: 25400, 0x0C: 38100, 0x0D: 50800}))
        self.assertEqual(table.insets[(0, 0)], (1.0, 2.0, 3.0, 4.0))

    def test_a_side_left_out_is_zero_not_a_default(self):
        table = self.one_table((0, 0, {0x0A: 9525, 0x0B: 9525, 0x0C: 9525}))
        self.assertEqual([round(v, 4) for v in table.insets[(0, 0)]], [0.75, 0.75, 0.75, 0.0])

    def test_a_cell_is_keyed_by_the_row_and_column_it_starts_in(self):
        table = self.one_table((3, 1, {0x0A: 12700}), heights=(18.0, 18.0, 18.0, 18.0))
        self.assertIn((3, 1), table.insets)

    def test_a_table_whose_cells_chunk_is_missing_is_skipped(self):
        tables = self.read((0x10, table_chunk((72.0,), (18.0,), cells_seqnum=9)))
        self.assertEqual(tables, {})

    def test_a_table_with_no_grid_is_skipped(self):
        tables = self.read((0x10, _chunk([_u32(0x66, 2)])), (0x63, cells_chunk()))
        self.assertEqual(tables, {})

    def test_two_tables_drawing_one_grid_disagreeing_are_both_dropped(self):
        tables = self.read(
            (0x10, table_chunk((72.0,), (18.0,), cells_seqnum=1)),
            (0x63, cells_chunk((0, 0, {0x0A: 12700}))),
            (0x10, table_chunk((72.0,), (18.0,), cells_seqnum=3)),
            (0x63, cells_chunk((0, 0, {0x0A: 25400}))),
        )
        self.assertEqual(list(tables.values()), [None])

    def test_two_tables_drawing_one_grid_agreeing_are_kept(self):
        tables = self.read(
            (0x10, table_chunk((72.0,), (18.0,), cells_seqnum=1)),
            (0x63, cells_chunk((0, 0, {0x0A: 12700}))),
            (0x10, table_chunk((72.0,), (18.0,), cells_seqnum=3)),
            (0x63, cells_chunk((0, 0, {0x0A: 12700}))),
        )
        self.assertEqual([t.insets[(0, 0)] for t in tables.values()], [(1.0, 0.0, 0.0, 0.0)])

    def test_a_signature_survives_the_trip_through_libmspub(self):
        # Our widths come from EMU; libmspub's come from the same EMU by way
        # of four-decimal inches. The signature has to ignore that gap.
        emu = 2472814
        ours = emu / 12700.0
        theirs = round(emu / 914400.0, 4) * 72.0
        self.assertNotEqual(ours, theirs)
        self.assertEqual(
            pubfile.table_signature([ours], [ours]),
            pubfile.table_signature([theirs], [theirs]),
        )

    def test_a_truncated_cells_chunk_does_not_raise(self):
        payload = cells_chunk((0, 0, {0x0A: 12700}))
        for cut in range(1, len(payload)):
            with self.subTest(cut=cut):
                self.read(
                    (0x10, table_chunk((72.0,), (18.0,), cells_seqnum=1)),
                    (0x63, payload[:cut]),
                )


class CellInsetApplicationTest(unittest.TestCase):
    """Getting the insets onto the cells libmspub reported."""

    def document_with_table(self):
        table = model.Table(
            x=0.0, y=0.0, width=108.0, height=18.0,
            column_widths=[72.0, 36.0],
            row_heights=[18.0],
        )
        table.cells = [
            model.TableCell(row=0, column=0),
            model.TableCell(row=0, column=1),
        ]
        document = model.Document(pages=[page_with(table)])
        return document, table

    def structure_with(self, insets, widths=(72.0, 36.0), heights=(18.0,)):
        signature = pubfile.table_signature(list(widths), list(heights))
        return pubfile.FileStructure(
            tables={signature: pubfile.TableStructure(insets=insets)}
        )

    def test_a_matched_table_gets_its_padding(self):
        document, table = self.document_with_table()
        convert._apply_cell_insets(
            document, self.structure_with({(0, 0): (1.0, 2.0, 3.0, 4.0)})
        )
        self.assertEqual(table.cells[0].insets, model.CellInsets(1.0, 2.0, 3.0, 4.0))

    def test_a_cell_the_file_does_not_mention_is_left_alone(self):
        document, table = self.document_with_table()
        convert._apply_cell_insets(
            document, self.structure_with({(0, 0): (1.0, 1.0, 1.0, 1.0)})
        )
        self.assertIsNone(table.cells[1].insets)

    def test_a_table_of_another_shape_is_not_matched(self):
        document, table = self.document_with_table()
        convert._apply_cell_insets(
            document, self.structure_with({(0, 0): (1.0, 1.0, 1.0, 1.0)}, widths=(99.0,))
        )
        self.assertIsNone(table.cells[0].insets)

    def test_an_ambiguous_grid_is_not_applied(self):
        document, table = self.document_with_table()
        signature = pubfile.table_signature([72.0, 36.0], [18.0])
        convert._apply_cell_insets(
            document, pubfile.FileStructure(tables={signature: None})
        )
        self.assertIsNone(table.cells[0].insets)

    def test_a_table_inside_a_group_is_reached(self):
        document, table = self.document_with_table()
        group = model.Group(children=[table])
        document.pages[0].items = [group]
        convert._apply_cell_insets(
            document, self.structure_with({(0, 0): (1.0, 2.0, 3.0, 4.0)})
        )
        self.assertEqual(table.cells[0].insets, model.CellInsets(1.0, 2.0, 3.0, 4.0))

    def test_no_structure_at_all_changes_nothing(self):
        document, table = self.document_with_table()
        convert._apply_cell_insets(document, None)
        self.assertIsNone(table.cells[0].insets)


def _escher(rec_type: int, payload: bytes, version: int = 0, instance: int = 0) -> bytes:
    return struct.pack("<HHI", version | (instance << 4), rec_type, len(payload)) + payload


def _escher_container(rec_type: int, children) -> bytes:
    return _escher(rec_type, b"".join(children), version=0x0F)


def _escher_values(rec_type: int, pairs) -> bytes:
    """An (id, value) record. CLIENT_ANCHOR and CLIENT_DATA repeat length."""
    body = b"".join(struct.pack("<HI", key, value & 0xFFFFFFFF) for key, value in pairs)
    return _escher(rec_type, struct.pack("<I", len(body) + 4) + body)


def _escher_properties(entries) -> bytes:
    """A property table: entries first, then every complex payload in order."""
    table, payloads = b"", b""
    for pid, value in entries:
        if isinstance(value, bytes):
            table += struct.pack("<HI", pid | 0x8000, len(value))
            payloads += value
        else:
            table += struct.pack("<HI", pid, value & 0xFFFFFFFF)
    return _escher(0xF00B, table + payloads, instance=len(entries))


def wordart_shape(
    text="Kerkdiensten", font="Monotype Corsiva", size=20.0, rotation=None,
    box=(-100, -50, 100, -20), anchor=True,
) -> bytes:
    """One Escher shape container carrying WordArt, as Publisher writes it."""
    entries = []
    if text is not None:
        entries.append((0x00C0, text.encode("utf-16-le") + b"\x00\x00"))
    if font is not None:
        entries.append((0x00C5, font.encode("utf-16-le") + b"\x00\x00"))
    if size is not None:
        entries.append((0x00C3, int(size * 65536)))
    if rotation is not None:
        entries.append((0x0004, int(rotation * 65536)))
    children = [_escher_properties(entries)]
    if anchor:
        children.append(_escher_values(0xF010, [
            (0x2001, box[0] * 12700), (0x2002, box[1] * 12700),
            (0x2003, box[2] * 12700), (0x2004, box[3] * 12700),
        ]))
    return _escher_container(0xF004, children)


class WordArtReadingTest(unittest.TestCase):
    """The half of a WordArt shape libmspub has no constants for."""

    def read(self, *shapes):
        return pubfile._wordart_shapes(b"".join(shapes))

    def one(self, **kwargs):
        found = self.read(wordart_shape(**kwargs))
        self.assertEqual(len(found), 1)
        return found[0]

    def test_the_words_the_font_and_the_size_are_read(self):
        art = self.one(text="Kerkdiensten", font="Monotype Corsiva", size=20.0)
        self.assertEqual(art.text, "Kerkdiensten")
        self.assertEqual(art.font, "Monotype Corsiva")
        self.assertAlmostEqual(art.size, 20.0)
        self.assertFalse(art.fitted)

    def test_the_band_is_measured_from_the_centre_of_the_page(self):
        art = self.one(box=(-100, -50, 100, -20))
        self.assertAlmostEqual(art.width, 200.0)
        self.assertAlmostEqual(art.height, 30.0)
        self.assertAlmostEqual(art.centre_x, 0.0)
        self.assertAlmostEqual(art.centre_y, -35.0)

    def test_a_shape_stating_no_size_is_fitted_to_its_band(self):
        art = self.one(size=None, box=(0, 0, 200, 40))
        self.assertTrue(art.fitted)
        self.assertAlmostEqual(art.size, 40.0 / 1.33, places=4)

    def test_rotation_is_read_as_signed_fixed_point(self):
        art = self.one(rotation=-12.192230224609375)
        self.assertAlmostEqual(art.rotation, -12.192230224609375)

    def test_a_shape_with_no_rotation_is_not_turned(self):
        self.assertEqual(self.one().rotation, 0.0)

    def test_a_shape_with_no_anchor_cannot_be_placed_and_is_skipped(self):
        self.assertEqual(self.read(wordart_shape(anchor=False)), [])

    def test_a_shape_with_no_text_is_an_ordinary_shape(self):
        self.assertEqual(self.read(wordart_shape(text=None)), [])

    def test_a_band_with_no_area_is_skipped(self):
        self.assertEqual(self.read(wordart_shape(box=(0, 0, 0, 40))), [])

    def test_several_shapes_are_all_found(self):
        found = self.read(
            wordart_shape(text="First", box=(0, 0, 100, 20)),
            wordart_shape(text="Second", box=(0, 40, 100, 60)),
        )
        self.assertEqual([art.text for art in found], ["First", "Second"])

    def test_a_shape_after_a_dg_container_is_still_found(self):
        # A DG container is followed by four bytes of tail, which desyncs
        # any walk that does not know about it -- and then everything after
        # the first page's shapes is lost.
        stream = (
            _escher_container(0xF002, [wordart_shape(text="Inside")])
            + b"\x00\x00\x00\x00"
            + wordart_shape(text="After", box=(0, 40, 100, 60))
        )
        self.assertEqual([art.text for art in self.read(stream)], ["Inside", "After"])

    def test_a_truncated_stream_does_not_raise(self):
        payload = wordart_shape()
        for cut in range(1, len(payload)):
            with self.subTest(cut=cut):
                pubfile._wordart_shapes(payload[:cut])

    def test_a_stream_of_rubbish_does_not_raise(self):
        pubfile._wordart_shapes(bytes(range(256)) * 4)


class WordArtRecoveryTest(unittest.TestCase):
    """Putting the words back on the guides libmspub did report."""

    def guides(self, x=100.0, y=200.0, width=200.0, height=30.0, **style) -> model.Path:
        """The two-edge filled path a WordArt arrives as."""
        return model.Path(
            x=x, y=y, width=width, height=height,
            ops=[
                ("M", x, y), ("L", x + width, y), ("Z",),
                ("M", x, y + height), ("L", x + width, y + height), ("Z",),
            ],
            style=model.GraphicStyle(fill=(0, 51, 128), **style),
        )

    def structure_with(self, *wordart) -> pubfile.FileStructure:
        return pubfile.FileStructure(wordart=list(wordart))

    def art(self, **kwargs):
        # A page of 612 x 792 puts a band at (100, 200) 200 x 30 here.
        defaults = dict(
            text="Kerkdiensten", font="Monotype Corsiva", size=20.0,
            centre_x=100.0 + 100.0 - 306.0, centre_y=200.0 + 15.0 - 396.0,
            width=200.0, height=30.0,
        )
        defaults.update(kwargs)
        return pubfile.WordArt(**defaults)

    def document_with(self, *items) -> model.Document:
        return model.Document(pages=[page_with(*items)])

    def test_the_guide_path_becomes_a_text_frame(self):
        path = self.guides()
        document = self.document_with(path)
        convert._recover_wordart(document, self.structure_with(self.art()))
        items = document.pages[0].items
        self.assertEqual(len(items), 1)
        frame = items[0]
        self.assertIsInstance(frame, model.TextFrame)
        span = frame.story.paragraphs[0].spans[0]
        self.assertEqual(span.text, "Kerkdiensten")
        self.assertEqual(span.font, "Monotype Corsiva")
        self.assertAlmostEqual(span.size_pt, 20.0)

    def test_the_frame_lands_on_the_band(self):
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(self.art()))
        frame = document.pages[0].items[0]
        self.assertAlmostEqual(frame.x, 100.0)
        self.assertAlmostEqual(frame.y, 200.0)
        self.assertAlmostEqual(frame.width, 200.0)
        self.assertAlmostEqual(frame.height, 30.0)

    def test_the_words_take_the_colour_of_the_shape(self):
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(self.art()))
        frame = document.pages[0].items[0]
        self.assertEqual(frame.story.paragraphs[0].spans[0].color, (0, 51, 128))
        # ... and the frame itself stays unfilled, or the box paints over them.
        self.assertIsNone(frame.style.fill)

    def test_a_shadow_on_the_shape_stays_with_the_words(self):
        shadow = model.Shadow(color=(192, 192, 192), offset_x=2.0, offset_y=2.0)
        document = self.document_with(self.guides(shadow=shadow))
        convert._recover_wordart(document, self.structure_with(self.art()))
        self.assertEqual(document.pages[0].items[0].style.shadow, shadow)

    def test_rotation_carries_over(self):
        document = self.document_with(self.guides())
        convert._recover_wordart(
            document, self.structure_with(self.art(rotation=-12.19))
        )
        self.assertAlmostEqual(document.pages[0].items[0].rotation, -12.19)

    def test_each_line_becomes_a_paragraph(self):
        document = self.document_with(self.guides())
        convert._recover_wordart(
            document, self.structure_with(self.art(text="Ter herinnering\r\naan"))
        )
        frame = document.pages[0].items[0]
        self.assertEqual(
            ["".join(s.text for s in p.spans) for p in frame.story.paragraphs],
            ["Ter herinnering", "aan"],
        )

    def test_a_path_that_matches_nothing_is_left_alone(self):
        path = self.guides()
        document = self.document_with(path)
        convert._recover_wordart(
            document, self.structure_with(self.art(centre_x=999.0))
        )
        self.assertIs(document.pages[0].items[0], path)

    def test_two_candidates_at_one_place_are_an_ambiguity_not_a_guess(self):
        path = self.guides()
        document = self.document_with(path)
        convert._recover_wordart(
            document, self.structure_with(self.art(), self.art(text="Other"))
        )
        self.assertIs(document.pages[0].items[0], path)

    def test_an_ordinary_shape_is_never_replaced(self):
        rectangle = model.Rectangle(
            x=100.0, y=200.0, width=200.0, height=30.0,
            style=model.GraphicStyle(fill=(0, 0, 0)),
        )
        document = self.document_with(rectangle)
        convert._recover_wordart(document, self.structure_with(self.art()))
        self.assertIs(document.pages[0].items[0], rectangle)

    def test_guides_inside_a_group_are_reached(self):
        path = self.guides()
        group = model.Group(children=[path])
        document = self.document_with(group)
        convert._recover_wordart(document, self.structure_with(self.art()))
        self.assertIsInstance(group.children[0], model.TextFrame)

    def test_guides_on_a_master_are_reached(self):
        path = self.guides()
        document = model.Document(pages=[page_with()])
        document.masters.append(
            model.Master(name="A", width=612.0, height=792.0, items=[path])
        )
        convert._recover_wordart(document, self.structure_with(self.art()))
        self.assertIsInstance(document.masters[0].items[0], model.TextFrame)

    def test_a_recovered_headline_is_reported(self):
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(self.art()))
        self.assertTrue(any("1 WordArt headline" in w for w in document.warnings))

    def test_a_fitted_size_is_reported_as_such(self):
        document = self.document_with(self.guides())
        convert._recover_wordart(
            document, self.structure_with(self.art(fitted=True))
        )
        self.assertTrue(any("sized from that band" in w for w in document.warnings))

    def test_a_shape_with_nowhere_to_go_is_named_rather_than_dropped(self):
        document = self.document_with()
        convert._recover_wordart(
            document, self.structure_with(self.art(text="I venerdì di Avvento"))
        )
        warning = " ".join(document.warnings)
        self.assertIn("not placed", warning)
        self.assertIn("I venerdì di Avvento", warning)

    def test_recovered_guides_no_longer_count_as_unrenderable(self):
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(self.art()))
        convert._check_unrenderable_paths(document)
        self.assertFalse(any("enclose no area" in w for w in document.warnings))

    def test_guides_that_stay_are_still_reported_as_unrenderable(self):
        document = self.document_with(self.guides())
        convert._check_unrenderable_paths(document)
        self.assertTrue(any("enclose no area" in w for w in document.warnings))

    def test_no_structure_at_all_changes_nothing(self):
        path = self.guides()
        document = self.document_with(path)
        convert._recover_wordart(document, None)
        self.assertIs(document.pages[0].items[0], path)


@needs_samples
@unittest.skipUnless(convert.PUBDUMP.exists(), "pubdump not built")
class EndToEndTest(unittest.TestCase):
    def test_the_missal_footers_number_one_to_fifteen(self):
        source = SAMPLES / "MISSAL MARIANA E PEDRO.pub"
        document = convert.parse_document(source)
        convert._apply_master_pages(document, pubfile.read_structure(source))
        footers = []
        for page in document.pages:
            frame = next(i for i in page.items if isinstance(i, model.TextFrame))
            footers.append("".join(
                s.text for p in frame.story.paragraphs for s in p.spans
            ).strip())
        self.assertEqual(footers, [str(n) for n in range(1, 16)])

    def test_a_document_without_page_numbers_is_untouched(self):
        source = SAMPLES / "Cantico_dei_Cantici.pub"
        document = convert.parse_document(source)
        before = [s.text for p in document.pages
                  for i in model._walk(p.items) if isinstance(i, model.TextFrame)
                  for par in i.story.paragraphs for s in par.spans]
        convert._apply_master_pages(document, pubfile.read_structure(source))
        after = [s.text for p in document.pages
                 for i in model._walk(p.items) if isinstance(i, model.TextFrame)
                 for par in i.story.paragraphs for s in par.spans]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
