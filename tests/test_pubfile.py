"""Structure read from the .pub itself, and what is done with it.

This is the one place the converter looks at the binary directly rather
than going through libmspub, so the tests care about two things: that it
reads the right thing, and that it never costs a conversion when it
cannot.
"""

from __future__ import annotations

import math
import struct
import tempfile
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from pubidml import convert, fontmetrics, model, pubfile
from pubidml.pubfile import _read_stream

REPO = Path(__file__).resolve().parent.parent
SAMPLES = REPO / "files"

needs_samples = unittest.skipUnless(
    (SAMPLES / "MISSAL MARIANA E PEDRO.pub").exists(), "sample .pub files absent"
)

#: The newsletters are the only samples carrying threaded stories or tables,
#: and they are not in the repository -- they are somebody's own documents.
#: So a test reading one is a canary over this machine's corpus, and what the
#: reading *rests* on has to be pinned synthetically to be checked anywhere
#: else. `needs_samples` does not cover these: it asks after a file that is
#: tracked, and would let a newsletter test run and fail on a fresh clone.
NEWSLETTER = SAMPLES / "cgk" / "1336 kerkbode.pub"
needs_newsletter = unittest.skipUnless(
    NEWSLETTER.exists(), f"{NEWSLETTER.name} absent (not tracked)"
)

#: The one file in the corpus whose character runs libmspub misreads, and
#: so the only one where a story arrives cut off. Same caveat as above.
TRUNCATED = SAMPLES / "cgk" / "1337 kerkbode.pub"
needs_truncated = unittest.skipUnless(
    TRUNCATED.exists(), f"{TRUNCATED.name} absent (not tracked)"
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


def cell_records(source: Path):
    """Every table cell of a real .pub, as the raw {block id: value} it is.

    `read_structure` hands back insets already resolved, which is the wrong
    end for asking what the writer chose to state and what it left out.
    """
    contents = pubfile._read_stream(source.read_bytes(), *pubfile._CONTENTS_STREAM)
    if not contents:
        return
    for _seq, kind, offset in pubfile._chunk_references(contents):
        if kind != pubfile._CELLS_CHUNK:
            continue
        for block in pubfile._chunk_blocks(contents, offset):
            if block.id != pubfile._CELL_ARRAY:
                continue
            for record in pubfile._children(contents, block):
                if record.id != pubfile._ARRAY_ENTRY:
                    continue
                yield {sub.id: sub.data for sub in pubfile._children(contents, record)}


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

    def test_a_difat_sector_pointing_at_itself_does_not_spin(self):
        # 1KB is enough: the header states a DIFAT count of 4 billion and
        # the sector it names points back at itself. Without a cycle guard
        # this never returns -- and a worker that never returns costs the
        # whole batch its report, since nothing can kill a thread.
        sector = 512
        data = bytearray(sector * 2)
        data[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        struct.pack_into("<H", data, 30, 9)            # 1 << 9 == 512
        struct.pack_into("<H", data, 32, 6)
        struct.pack_into("<I", data, 48, 0xFFFFFFFE)   # no directory
        struct.pack_into("<I", data, 68, 0)            # DIFAT starts at 0
        struct.pack_into("<I", data, 72, 0xFFFFFFFF)   # ... 4 billion of them
        struct.pack_into("<I", data, sector * 2 - 4, 0)  # which points at 0

        # Run it off-thread so a regression fails the suite instead of
        # hanging it: an unguarded walk would never reach the assertion.
        done = threading.Event()

        def read():
            _read_stream(bytes(data), "Contents")
            done.set()

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(timeout=30)
        self.assertTrue(done.is_set(), "the DIFAT walk did not terminate")


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
        # Unbent, like 47 of the corpus's 48 shapes -- so straight text is
        # not an approximation of this headline, it is the headline.
        self.assertIsNone(art.warp)
        # The corpus's one bent shape, and the one the file gives no
        # libmspub shape for, which is why it is named rather than placed.
        bent = next(a for a in structure.wordart if a.warp)
        self.assertEqual(bent.warp, "button curve")
        self.assertIn("Avvento", bent.text)

    def test_wordart_styling_is_read_out_of_a_real_file(self):
        # The shape properties carry the character formatting, so this is
        # the check that the boolean set is unpacked the way Publisher
        # packs it: a headline stating italic and one stating bold, out of
        # the same file, telling apart.
        source = SAMPLES / "cgk" / "1336 kerkbode.pub"
        if not source.exists():
            self.skipTest("newsletter sample absent")
        by_text = {art.text: art for art in pubfile.read_structure(source).wordart}
        loose = by_text["Meditatie"]
        self.assertTrue(loose.italic)
        self.assertFalse(loose.bold)
        # Publisher's Loose, as 16.16 fixed point.
        self.assertAlmostEqual(loose.spacing, 1.2, places=3)
        heavy = by_text["Pastoraal Contact"]
        self.assertTrue(heavy.bold)
        self.assertFalse(heavy.italic)
        self.assertIsNone(heavy.spacing)

    def test_a_stream_name_two_storages_share_resolves_by_path(self):
        # `1336 kerkbode.pub` holds two streams called CONTENTS: Publisher's
        # text under Quill/QuillSub, and a metafile belonging to an embedded
        # object. Matching on the name alone found the metafile, which read
        # as a Quill stream with no chunks in it at all -- so the document
        # appeared to have no field table and its 27 page numbers stayed '#'.
        source = SAMPLES / "cgk" / "1336 kerkbode.pub"
        if not source.exists():
            self.skipTest("newsletter sample absent")
        data = source.read_bytes()
        quill = pubfile._read_stream(data, *pubfile._QUILL_STREAM)
        self.assertTrue(quill.startswith(b"CHNKINK "), quill[:16])
        self.assertTrue(pubfile.read_structure(source).has_fields)

    def test_tab_stops_are_read_out_of_a_real_file(self):
        # The synthetic streams prove the record layout; this proves it is
        # the layout Publisher writes. Three of this file's 38 tabbed
        # paragraphs state a stop, which is the whole corpus's supply.
        structure = pubfile.read_structure(SAMPLES / "rotated_text.pub")
        stated = [(text, stops) for text, stops in structure.paragraph_stops if stops]
        self.assertEqual(len(structure.paragraph_stops), 38)
        self.assertEqual(
            [stops for _text, stops in stated],
            [
                ((27.4, "left"),),
                ((27.4, "left"),),
                ((106.25, "left"),),
            ],
        )

    @needs_samples
    def test_the_file_states_the_collapsed_frame_libmspub_reports(self):
        # The 5.5 x 5.7pt frame holding 3,785 characters was written up as
        # libmspub reporting a degenerate size. It is not: the .pub's own
        # Escher anchor for that shape states the same box, so the document
        # really does contain a text box collapsed to nothing, and Publisher
        # would have shown it empty too. The two are transposed because the
        # shape is turned -46 degrees, which is inside the 45-135 band where
        # Publisher stores the box before the swap and libmspub swaps back.
        structure = pubfile.read_structure(SAMPLES / "rotated_text.pub")
        anchor = min(structure.anchors, key=lambda a: a.width * a.height)
        self.assertAlmostEqual(anchor.width, 5.67, places=2)
        self.assertAlmostEqual(anchor.height, 5.46, places=2)

    def test_a_file_with_no_wordart_reports_none(self):
        structure = pubfile.read_structure(SAMPLES / "MISSAL MARIANA E PEDRO.pub")
        self.assertEqual(structure.wordart, [])

    def test_page_count_matches_what_libmspub_emits(self):
        # The two halves hold the same number of pages -- but not in the same
        # order (§14), so this is what lets an unsettled page take the master
        # the remaining chunks agree on, and nothing more than that.
        if not convert.PUBDUMP.exists():
            self.skipTest("pubdump not built")
        for source in sorted(SAMPLES.glob("*.pub")):
            with self.subTest(source=source.name):
                s = pubfile.read_structure(source)
                document = convert.parse_document(source)
                self.assertEqual(len(s.pages), len(document.pages))

    def test_every_page_chunk_names_the_master_it_applies(self):
        s = pubfile.read_structure(SAMPLES / "MISSAL MARIANA E PEDRO.pub")
        for chunk in s.pages:
            with self.subTest(chunk=chunk.seq):
                self.assertIsNotNone(s.master_of_chunk(chunk.seq))

    def test_a_cell_states_an_inset_exactly_when_it_has_one(self):
        # This is what makes reading an absent side as zero safe, and it is
        # a fact about the writer rather than an assumption. Across the
        # corpus 3289 sides are stated and 1751 left out, and **not one
        # stated side is zero** -- so an omission cannot be a stated zero
        # that went missing, and it cannot be the 0.04in default either,
        # since the tables that mean the default write all four out.
        stated = omitted = zeros = 0
        for source in sorted(SAMPLES.rglob("*.pub")):
            for cell in cell_records(source):
                for side in pubfile._CELL_INSETS:
                    if side not in cell:
                        omitted += 1
                    else:
                        stated += 1
                        zeros += cell[side] == 0
        # A canary over whatever samples this machine has, not a claim that
        # any particular one is here: the files that carry tables are the
        # newsletters, which are not in the repository. What the rule rests
        # on is checked everywhere by the synthetic tests below.
        if not (stated and omitted):
            self.skipTest("no sample present carries a table cell")
        self.assertEqual(zeros, 0)

    def test_a_cell_leaves_sides_out_sparsely_rather_than_truncating(self):
        # The stated counts fall away towards the last side -- 1056, 1037,
        # 1001, 195 -- which reads like trailing fields being truncated.
        # It is not that: cells state the top inset while leaving out the
        # left one, so each side is written on its own and a side that is
        # absent says nothing about the sides after it.
        cells = [
            cell
            for source in sorted(SAMPLES.rglob("*.pub"))
            for cell in cell_records(source)
        ]
        if not cells:
            self.skipTest("no sample present carries a table cell")
        self.assertTrue([c for c in cells if 0x0B in c and 0x0A not in c])


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

    def test_the_fields_run_left_right_top_bottom(self):
        # Measured off Publisher's own PDF rather than inferred from the
        # order Escher uses for a text frame: 0x0B is the right inset, so
        # it lands third in a left/top/right/bottom tuple, not second.
        table = self.one_table((0, 0, {0x0A: 12700, 0x0B: 25400, 0x0C: 38100, 0x0D: 50800}))
        self.assertEqual(table.insets[(0, 0)], (1.0, 3.0, 2.0, 4.0))

    def test_a_side_left_out_is_zero_not_a_default(self):
        # Zero rather than Publisher's own 0.04in: the writer emits no
        # explicit zero anywhere in the corpus (0 of 3397 inset fields),
        # writes all four out when they *are* the default, and in
        # Publisher's PDF a cell stating no inset at all sets its text
        # 0.02pt from the column edge where the default would be 2.88pt in.
        table = self.one_table((0, 0, {0x0A: 9525, 0x0B: 9525, 0x0C: 9525}))
        self.assertEqual([round(v, 4) for v in table.insets[(0, 0)]], [0.75, 0.75, 0.75, 0.0])

    def test_a_side_left_out_of_the_middle_is_zero_too(self):
        # The reading only holds because omission is per-side rather than a
        # truncation of the trailing fields: a cell states the right inset
        # and leaves the left one out. The corpus says Publisher writes
        # cells this way; this says the reader believes it, on any machine.
        table = self.one_table((0, 0, {0x0B: 44450}))
        self.assertEqual([round(v, 4) for v in table.insets[(0, 0)]], [0.0, 0.0, 3.5, 0.0])

    def test_a_cell_stating_no_side_at_all_is_four_zeros(self):
        table = self.one_table((0, 0, {}))
        self.assertEqual(table.insets[(0, 0)], (0.0, 0.0, 0.0, 0.0))

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


def cell_rule_shape(table_seq, segment, weight=0.5, color=0x0000FF) -> bytes:
    """One Escher shape of the kind Publisher writes for a ruled cell edge.

    A rule is stated as a segment on the grid's lattice -- the lines
    between cells, numbered from zero -- rather than as a side of a cell,
    which is why one shape can rule several cells at once.
    """
    start_row, start_column, end_row, end_column = segment
    pairs = [(0x2001, 1 if start_row == end_row else 2)]
    for key, value in (
        (0x2004, start_row), (0x2005, start_column),
        (0x2006, end_row), (0x2007, end_column),
    ):
        # Publisher leaves a zero out rather than writing it.
        if value:
            pairs.append((key, value))
    pairs.append((0x6802, table_seq))
    return _escher_container(0xF004, [
        _escher_properties([(0x0181, color), (0x01CB, round(weight * 12700))]),
        _escher_values(0xF010, sorted(pairs)),
    ])


def cell_shade_shape(table_seq, cell, color=0x0000FF) -> bytes:
    """One Escher shape of the kind Publisher writes for a shaded cell.

    A shade names a single cell rather than a run of lattice, so it
    carries no orientation and no line width -- which is what separates
    it from a rule in the same stream.
    """
    row, column = cell
    pairs = [(key, value) for key, value in ((0x2002, row), (0x2003, column)) if value]
    pairs.append((0x6802, table_seq))
    return _escher_container(0xF004, [
        _escher_properties([(0x0181, color)]),
        _escher_values(0xF010, sorted(pairs)),
    ])


class TableRuleReadingTest(unittest.TestCase):
    """The cell rules Publisher keeps in EscherStm rather than in the cell.

    A cell's own record states padding and alignment and no line anywhere
    in the corpus. The lines are shapes in the drawing stream, one per
    ruled edge, tied back to their table by its own chunk seqnum.
    """

    def read(self, *shapes, widths=(72.0, 36.0), heights=(18.0, 18.0), palette=()):
        contents, refs = b"\x00" * 8, []
        for seq, (kind, payload) in enumerate((
            (0x10, table_chunk(widths, heights, cells_seqnum=1)),
            (0x63, cells_chunk((0, 0, {0x0A: 12700}))),
        )):
            refs.append((seq, kind, len(contents)))
            contents += payload
        tables = pubfile._read_tables(
            contents, refs, escher=b"".join(shapes), palette=list(palette)
        )
        return tables[pubfile.table_signature(list(widths), list(heights))]

    def test_a_horizontal_segment_rules_the_top_of_the_cell_below_it(self):
        table = self.read(cell_rule_shape(0, (1, 0, 1, 1)))
        self.assertIn((1, 0, "top"), table.rules)

    def test_a_vertical_segment_rules_the_left_of_the_cell_beside_it(self):
        table = self.read(cell_rule_shape(0, (0, 1, 1, 1)))
        self.assertIn((0, 1, "left"), table.rules)

    def test_an_interior_line_is_given_to_both_cells_that_share_it(self):
        # One line on the page, but two cells state it. A cell whose
        # record was read has its unruled sides written off, so leaving
        # the cell above to say nothing would put a zero against the rule.
        table = self.read(cell_rule_shape(0, (1, 0, 1, 1)))
        self.assertIn((0, 0, "bottom"), table.rules)

    def test_an_interior_line_down_the_grid_is_shared_the_same_way(self):
        table = self.read(cell_rule_shape(0, (0, 1, 1, 1)))
        self.assertIn((0, 0, "right"), table.rules)

    def test_the_line_along_the_foot_of_the_table_rules_the_last_row(self):
        # There is no row below it to carry the rule as a top edge, and a
        # cell that is not in the table must not be keyed at all.
        table = self.read(cell_rule_shape(0, (2, 0, 2, 1)))
        self.assertIn((1, 0, "bottom"), table.rules)
        self.assertNotIn((2, 0, "top"), table.rules)

    def test_the_line_down_the_side_of_the_table_rules_the_last_column(self):
        table = self.read(cell_rule_shape(0, (0, 2, 1, 2)))
        self.assertIn((0, 1, "right"), table.rules)
        self.assertNotIn((0, 2, "left"), table.rules)

    def test_the_colour_is_the_fill_because_the_shape_is_a_filled_bar(self):
        # 0x00BBGGRR, as everywhere else in this stream: 0x0000FF is red,
        # not blue. The shape states no line colour at all -- 545 of the
        # corpus's 604 do not -- because Publisher draws the rule as a
        # thin filled rectangle rather than as a stroke.
        table = self.read(cell_rule_shape(0, (1, 0, 1, 1), color=0x0000FF))
        self.assertEqual(table.rules[(1, 0, "top")].color, (255, 0, 0))

    def test_a_colour_named_from_the_palette_is_resolved(self):
        # Two thirds of the corpus's rules name a palette entry rather
        # than stating a colour, which is the form ColorReference reads.
        table = self.read(
            cell_rule_shape(0, (1, 0, 1, 1), color=0x08000001),
            palette=[(0, 0, 0), (17, 34, 51)],
        )
        self.assertEqual(table.rules[(1, 0, "top")].color, (17, 34, 51))

    def test_the_weight_arrives_in_points(self):
        table = self.read(cell_rule_shape(0, (1, 0, 1, 1), weight=0.5))
        self.assertAlmostEqual(table.rules[(1, 0, "top")].weight, 0.5)

    def test_a_shaded_cell_is_read_by_the_cell_it_names(self):
        table = self.read(cell_shade_shape(0, (1, 1), color=0x00FFFF))
        self.assertEqual(table.shades[(1, 1)], (255, 255, 0))

    def test_a_shade_of_the_first_cell_states_neither_row_nor_column(self):
        # Publisher leaves a zero out, so the top-left cell's shade names
        # no coordinates at all and is not thereby a shade of nothing.
        table = self.read(cell_shade_shape(0, (0, 0)))
        self.assertIn((0, 0), table.shades)


STYLED_TABLE = SAMPLES / "experiments" / "table-styled.pub"


@unittest.skipUnless(STYLED_TABLE.exists(), "the styled table sample is absent")
class StyledTableTest(unittest.TestCase):
    """The one sample whose ruling is known from outside the file.

    It was drawn in Publisher to order: R1C1 and R1C2 shaded, every side
    of R2C1 ruled and the top of R2C2, row 3 untouched. So this is the
    only place the lattice reading can be checked against a human's
    intention rather than against itself.
    """

    def setUp(self):
        structure = pubfile.read_structure(STYLED_TABLE)
        self.table = next(
            table for table in structure.tables.values() if table is not None
        )

    def test_every_side_of_the_bordered_cell_is_ruled(self):
        self.assertEqual(
            sorted(
                side for (row, column, side) in self.table.rules
                if (row, column) == (1, 0)
            ),
            ["bottom", "left", "right", "top"],
        )

    def test_the_cell_bordered_on_one_side_is_ruled_there_and_not_below(self):
        sides = {
            side for (row, column, side) in self.table.rules
            if (row, column) == (1, 1)
        }
        # Its left is R2C1's right: one line, and both cells state it.
        self.assertEqual(sides, {"top", "left"})

    def test_the_untouched_row_states_only_the_line_it_shares(self):
        # Row 3 was left alone, so the only rule it can carry is the one
        # along its top, which is also the foot of the bordered cell.
        self.assertEqual(
            {
                (column, side) for (row, column, side) in self.table.rules
                if row == 2
            },
            {(0, "top")},
        )

    def test_the_rules_carry_the_weight_and_colour_that_were_drawn(self):
        rule = self.table.rules[(1, 0, "top")]
        self.assertAlmostEqual(rule.weight, 3.0)
        self.assertEqual(rule.color, (255, 0, 0))

    def test_the_two_shaded_cells_are_the_colours_they_were_given(self):
        # Solid red and solid yellow across the top row, and the third
        # cell of that row left unshaded.
        self.assertEqual(
            self.table.shades, {(0, 0): (255, 0, 0), (0, 1): (255, 255, 0)}
        )


def shape_chunk(story_id=None, chain_index=None) -> bytes:
    """A SHAPE chunk saying which story it holds and where in it."""
    blocks = []
    if story_id is not None:
        blocks.append(_u32(0x27, story_id))
    if chain_index is not None:
        blocks.append(_u32(0x28, chain_index))
    return _chunk(blocks)


class StoryChainReadingTest(unittest.TestCase):
    """Which text frames Publisher linked, and in what order.

    libmspub hands the whole story to every frame of a chain and says
    nothing about the link, so the order used to be the order the frames
    turned up in. The file states it outright: a shape names the story it
    holds and its own place in it.
    """

    def read(self, *shapes):
        contents, refs = b"\x00" * 8, []
        for seq, payload in enumerate(shapes):
            refs.append((seq, 0x01, len(contents)))
            contents += payload
        return pubfile._read_story_chains(contents, refs)

    def test_shapes_sharing_a_story_are_a_chain_in_the_stated_order(self):
        chains = self.read(
            shape_chunk(77, 2), shape_chunk(77, 0), shape_chunk(77, 1),
        )
        self.assertEqual(chains, [[1, 2, 0]])

    def test_the_first_link_states_no_index(self):
        # The same writing rule the cell insets follow: a field is written
        # when it has something to say, so the head of a chain leaves the
        # index out rather than writing a zero.
        chains = self.read(shape_chunk(77, 1), shape_chunk(77))
        self.assertEqual(chains, [[1, 0]])

    def test_a_story_held_by_one_shape_is_not_a_chain(self):
        # This is what keeps a repeated label out: a page-number footer is
        # one master shape replayed onto every page, not a chain of frames.
        self.assertEqual(self.read(shape_chunk(2), shape_chunk(3)), [])

    def test_a_shape_naming_no_story_is_passed_over(self):
        self.assertEqual(self.read(shape_chunk(), shape_chunk()), [])

    def test_two_shapes_claiming_one_place_are_not_a_chain(self):
        # Nothing in the corpus does this, and a chain whose order is
        # contradicted is not an order.
        self.assertEqual(self.read(shape_chunk(77, 1), shape_chunk(77, 1)), [])

    def test_a_truncated_shape_chunk_does_not_raise(self):
        payload = shape_chunk(77, 1)
        for cut in range(1, len(payload)):
            with self.subTest(cut=cut):
                self.read(payload[:cut], shape_chunk(77))


@needs_newsletter
class RealStoryChainTest(unittest.TestCase):
    def test_the_chains_of_a_real_file_are_read_in_order(self):
        # Three chains, and the file states an order for each. The last is
        # the pair the old text-and-capacity guess could not see: 1,905
        # characters in a frame roomy enough for about 2,325, so nothing
        # oversets and nothing gave the link away.
        structure = pubfile.read_structure(NEWSLETTER)
        self.assertEqual(
            structure.story_chains,
            [
                [316, 317, 311, 489],
                [337, 338, 387, 402, 392, 354, 553, 555],
                [527, 490],
            ],
        )


@needs_samples
class TrackedStoryChainTest(unittest.TestCase):
    """The half of the chain reading a fresh clone can still check.

    Kept apart from the newsletter test above so it survives the machines
    that do not have the newsletters -- which is every machine but one.
    """

    def test_a_file_that_links_nothing_states_no_chain(self):
        structure = pubfile.read_structure(SAMPLES / "MISSAL MARIANA E PEDRO.pub")
        self.assertEqual(structure.story_chains, [])


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

    def structure_with(
        self, insets, widths=(72.0, 36.0), heights=(18.0,), rules=None, shades=None
    ):
        signature = pubfile.table_signature(list(widths), list(heights))
        return pubfile.FileStructure(
            tables={
                signature: pubfile.TableStructure(
                    insets=insets, rules=rules or {}, shades=shades or {}
                )
            }
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

    def test_a_matched_cell_is_marked_unruled(self):
        # Its record was read, and no record in the corpus states a rule:
        # so this is a cell Publisher recorded no lines for, which is not
        # the same as a cell nobody looked at.
        document, table = self.document_with_table()
        convert._apply_cell_insets(
            document, self.structure_with({(0, 0): (1.0, 2.0, 3.0, 4.0)})
        )
        self.assertTrue(table.cells[0].unruled)

    def test_a_ruled_edge_reaches_the_cell_it_rules(self):
        document, table = self.document_with_table()
        rule = pubfile.CellRule(weight=0.5, color=(17, 34, 51))
        carried = model.CellRule(weight=0.5, color=(17, 34, 51))
        convert._apply_cell_insets(
            document,
            self.structure_with(
                {(0, 0): (1.0, 1.0, 1.0, 1.0)}, rules={(0, 0, "top"): rule}
            ),
        )
        self.assertEqual(table.cells[0].rules, {"top": carried})

    def test_a_spanning_cell_takes_its_right_edge_from_the_far_column(self):
        # A rule is stated per grid position, and a merged cell covers
        # several. Its right-hand side is the right of the last column it
        # spans, not the right of the one it starts in -- which is a line
        # running through the middle of it.
        document, table = self.document_with_table()
        table.cells = [model.TableCell(row=0, column=0, column_span=2)]
        rule = pubfile.CellRule(weight=0.5)
        convert._apply_cell_insets(
            document,
            self.structure_with(
                {(0, 0): (1.0, 1.0, 1.0, 1.0)}, rules={(0, 1, "right"): rule}
            ),
        )
        self.assertEqual(set(table.cells[0].rules), {"right"})

    def test_a_line_through_the_middle_of_a_spanning_cell_is_not_drawn(self):
        # IDML has one stroke per side of a cell, so half of one cannot
        # be ruled. Claiming the whole side would draw a line Publisher
        # did not, which is worse than the line it cannot draw.
        document, table = self.document_with_table()
        table.cells = [model.TableCell(row=0, column=0, column_span=2)]
        convert._apply_cell_insets(
            document,
            self.structure_with(
                {(0, 0): (1.0, 1.0, 1.0, 1.0)},
                rules={(0, 1, "left"): pubfile.CellRule(weight=0.5)},
            ),
        )
        self.assertEqual(table.cells[0].rules, {})

    def test_a_shaded_cell_reaches_the_cell_it_fills(self):
        document, table = self.document_with_table()
        convert._apply_cell_insets(
            document,
            self.structure_with(
                {(0, 0): (1.0, 1.0, 1.0, 1.0)}, shades={(0, 0): (17, 34, 51)}
            ),
        )
        self.assertEqual(table.cells[0].shade, (17, 34, 51))

    def test_a_cell_the_file_does_not_mention_is_not_marked(self):
        document, table = self.document_with_table()
        convert._apply_cell_insets(
            document, self.structure_with({(0, 0): (1.0, 1.0, 1.0, 1.0)})
        )
        self.assertFalse(table.cells[1].unruled)

    def test_no_structure_at_all_marks_nothing(self):
        document, table = self.document_with_table()
        convert._apply_cell_insets(document, None)
        self.assertFalse(table.cells[0].unruled)

    def test_the_tables_it_silenced_are_named_in_the_report(self):
        document, _table = self.document_with_table()
        convert._apply_cell_insets(
            document, self.structure_with({(0, 0): (1.0, 2.0, 3.0, 4.0)})
        )
        self.assertTrue(
            any("cell rule" in warning for warning in document.warnings),
            document.warnings,
        )

    def test_a_table_whose_lines_were_read_is_not_reported_as_silenced(self):
        # The warning is about lines that were lost, and this table's
        # were not: they came out of the drawing stream onto its cells.
        document, _table = self.document_with_table()
        convert._apply_cell_insets(
            document,
            self.structure_with(
                {(0, 0): (1.0, 2.0, 3.0, 4.0)},
                rules={(0, 0, "top"): pubfile.CellRule(weight=0.5)},
            ),
        )
        self.assertEqual(document.warnings, [])

    def test_a_ruled_table_with_no_text_in_it_is_kept(self):
        # The grid a blank ruled table draws is the whole of what it
        # contributes, and until the drawing stream was read there was
        # nothing to see it by.
        document, table = self.document_with_table()
        convert._apply_cell_insets(
            document,
            self.structure_with(
                {(0, 0): (1.0, 1.0, 1.0, 1.0)},
                rules={(0, 0, "top"): pubfile.CellRule(weight=0.5)},
            ),
        )
        convert._drop_blank_tables(document)
        self.assertEqual(document.pages[0].items, [table])

    def test_a_table_with_text_in_it_is_kept(self):
        document, table = self.document_with_table()
        table.cells[0].story.paragraphs.append(
            model.Paragraph(spans=[model.Span(text="x")])
        )
        convert._drop_blank_tables(document)
        self.assertEqual(document.pages[0].items, [table])

    def test_a_table_with_a_fill_of_its_own_is_kept(self):
        document, table = self.document_with_table()
        table.style.fill = (255, 0, 0)
        convert._drop_blank_tables(document)
        self.assertEqual(document.pages[0].items, [table])

    def test_a_table_that_draws_nothing_and_says_nothing_is_dropped(self):
        document, _table = self.document_with_table()
        convert._apply_cell_insets(
            document, self.structure_with({(0, 0): (1.0, 1.0, 1.0, 1.0)})
        )
        convert._drop_blank_tables(document)
        self.assertEqual(document.pages[0].items, [])

    def test_dropping_a_table_is_said_rather_than_done_in_silence(self):
        document, _table = self.document_with_table()
        convert._drop_blank_tables(document)
        self.assertEqual(len(document.warnings), 1)
        self.assertIn("1 empty table(s) dropped", document.warnings[0])

    def test_a_table_that_is_kept_says_nothing(self):
        document, _table = self.document_with_table()
        convert._apply_cell_insets(
            document,
            self.structure_with(
                {(0, 0): (1.0, 1.0, 1.0, 1.0)},
                rules={(0, 0, "top"): pubfile.CellRule(weight=0.5)},
            ),
        )
        document.warnings.clear()
        convert._drop_blank_tables(document)
        self.assertEqual(document.warnings, [])

    def test_a_shaded_table_is_not_reported_as_silenced_either(self):
        document, _table = self.document_with_table()
        convert._apply_cell_insets(
            document,
            self.structure_with(
                {(0, 0): (1.0, 2.0, 3.0, 4.0)}, shades={(0, 0): (255, 0, 0)}
            ),
        )
        self.assertEqual(document.warnings, [])


def quill_stream(*chunks: tuple) -> bytes:
    """A Quill stream holding the given (name, payload) chunks.

    The reference list sits at 0x18 and every reference names an offset
    into the stream, so the payloads are laid out after the list.
    """
    body = b""
    references = b""
    start = 0x18 + 8 + 24 * len(chunks)
    for name, payload in chunks:
        offset = start + len(body)
        references += (
            struct.pack("<H", 0x18) + name.encode("ascii")
            + struct.pack("<H", 0) + b"\x01\x00\x00\x00" + b"    "
            + struct.pack("<II", offset, len(payload))
        )
        body += payload
    head = b"\x00" * 0x18 + struct.pack("<HHI", 0, len(chunks), 0xFFFFFFFF)
    return head + references + body


def tab_block(*stops) -> bytes:
    """A paragraph style's tab stop record. Each stop is (EMU, code)."""
    entries = []
    for index, (position, code) in enumerate(stops):
        fields = [_u32(0x00, position & 0xFFFFFFFF)]
        if code is not None:
            fields.append(_block(0x01, 0x10, struct.pack("<H", code)))
        entries.append(_container(index, 0x88, fields))
    return _container(0x32, 0x82, [
        _block(0x27, 0x1A, struct.pack("<H", len(stops))),
        _container(0x28, 0x8A, entries),
    ])


def paragraph_chunk(text_at: int, paragraphs) -> bytes:
    """An FDPP chunk: the offset each paragraph ends at, then its style.

    `paragraphs` is a sequence of (character count, style body). Offsets
    are measured from the start of the stream, which is why the caller
    passes where the TEXT chunk begins.
    """
    styles, at = b"", 0
    positions, ends = [], []
    end = text_at
    for length, body in paragraphs:
        end += length * 2
        ends.append(end)
    header = 8 + 6 * len(paragraphs)
    for _length, body in paragraphs:
        positions.append(header + at)
        styles += struct.pack("<I", len(body) + 4) + body
        at += len(body) + 4
    return (
        struct.pack("<H", len(paragraphs)) + b"\x00" * 6
        + b"".join(struct.pack("<I", e) for e in ends)
        + b"".join(struct.pack("<H", p) for p in positions)
        + styles
    )


class TabStopReadingTest(unittest.TestCase):
    """The stops libmspub parses into a member it then never reads."""

    def read(self, text: str, paragraphs, chunk_name: str = "FDPP"):
        encoded = text.encode("utf-16-le")
        # The text chunk must be laid out before its offsets can be stated,
        # so build once to learn where it lands and then again for real.
        probe = quill_stream(("TEXT", encoded), (chunk_name, b""))
        text_at = struct.unpack_from("<I", probe, 0x18 + 8 + 16)[0]
        stream = quill_stream(
            ("TEXT", encoded), (chunk_name, paragraph_chunk(text_at, paragraphs))
        )
        return pubfile._paragraph_stops(stream)

    def test_a_stop_arrives_in_points_against_its_paragraph(self):
        found = self.read("a\tb\r", [(4, tab_block((114300, None)))])
        self.assertEqual(found, [("a\tb\r", ((9.0, "left"),))])

    def test_the_alignment_byte_says_centre_or_right(self):
        found = self.read(
            "a\tb\r", [(4, tab_block((2096901, 0xFF02), (4181102, 0xCC01)))]
        )
        self.assertEqual(
            [alignment for _position, alignment in found[0][1]], ["center", "right"]
        )

    def test_a_stop_left_of_the_text_edge_is_read_as_negative(self):
        found = self.read("a\tb\r", [(4, tab_block((0xFFF59304, None)))])
        self.assertAlmostEqual(found[0][1][0][0], -53.8, places=1)

    def test_a_paragraph_with_no_tab_is_not_reported(self):
        # A stop only decides where a tab lands; there is nothing to carry.
        self.assertEqual(self.read("ab\r", [(3, tab_block((114300, None)))]), [])

    def test_a_tabbed_paragraph_stating_no_stops_is_still_reported(self):
        # Two paragraphs of one text, one with stops and one without, is an
        # ambiguity — which needs the second to be visible to the caller.
        found = self.read("a\tb\r", [(4, b"")])
        self.assertEqual(found, [("a\tb\r", ())])

    def test_paragraphs_are_cut_where_the_offsets_say(self):
        found = self.read(
            "a\tb\rc\td\r",
            [(4, tab_block((114300, None))), (4, tab_block((228600, None)))],
        )
        self.assertEqual([text for text, _stops in found], ["a\tb\r", "c\td\r"])
        self.assertEqual(found[1][1], ((18.0, "left"),))

    def test_a_stream_with_no_text_chunk_reads_nothing(self):
        self.assertEqual(pubfile._paragraph_stops(quill_stream(("FDPP", b""))), [])

    def test_a_truncated_chunk_does_not_raise(self):
        stream = quill_stream(("TEXT", "a\tb\r".encode("utf-16-le")), ("FDPP", b"\x09"))
        self.assertEqual(pubfile._paragraph_stops(stream), [])


class TabStopApplicationTest(unittest.TestCase):
    """Tying a stop to the paragraph libmspub reported, by its text."""

    def document(self, *texts: str) -> model.Document:
        frame = model.TextFrame(x=0.0, y=0.0, width=200.0, height=100.0)
        for text in texts:
            paragraph = model.Paragraph()
            paragraph.spans.append(model.Span(text=text))
            frame.story.paragraphs.append(paragraph)
        return model.Document(pages=[page_with(frame)])

    def paragraphs(self, document):
        return document.pages[0].items[0].story.paragraphs

    def structure(self, *stops) -> pubfile.FileStructure:
        return pubfile.FileStructure(paragraph_stops=list(stops))

    def test_a_paragraph_matched_by_its_text_gets_its_stops(self):
        document = self.document("van:\tLisa")
        convert._apply_tab_stops(
            document, self.structure(("van:\tLisa\r", ((27.4, "left"),)))
        )
        self.assertEqual(
            self.paragraphs(document)[0].tab_stops, [model.TabStop(27.4, "left")]
        )

    def test_the_paragraph_mark_and_control_characters_do_not_block_a_match(self):
        document = self.document("a\tb")
        convert._apply_tab_stops(document, self.structure(("a\tb\r\x00", ((9.0, "left"),))))
        self.assertEqual(self.paragraphs(document)[0].tab_stops, [model.TabStop(9.0)])

    def test_one_text_stated_two_ways_is_an_ambiguity_and_neither_applies(self):
        document = self.document("a\tb")
        convert._apply_tab_stops(
            document,
            self.structure(("a\tb\r", ((9.0, "left"),)), ("a\tb\r", ((18.0, "left"),))),
        )
        self.assertEqual(self.paragraphs(document)[0].tab_stops, [])

    def test_one_text_stated_twice_alike_is_not_an_ambiguity(self):
        document = self.document("a\tb")
        convert._apply_tab_stops(
            document,
            self.structure(("a\tb\r", ((9.0, "left"),)), ("a\tb\r", ((9.0, "left"),))),
        )
        self.assertEqual(self.paragraphs(document)[0].tab_stops, [model.TabStop(9.0)])

    def test_a_text_seen_both_with_stops_and_without_is_an_ambiguity(self):
        document = self.document("a\tb")
        convert._apply_tab_stops(
            document, self.structure(("a\tb\r", ((9.0, "left"),)), ("a\tb\r", ()))
        )
        self.assertEqual(self.paragraphs(document)[0].tab_stops, [])

    def test_a_paragraph_the_file_says_nothing_about_is_left_alone(self):
        document = self.document("a\tb")
        convert._apply_tab_stops(document, self.structure(("c\td\r", ((9.0, "left"),))))
        self.assertEqual(self.paragraphs(document)[0].tab_stops, [])

    def test_tabs_with_no_stop_in_a_file_stating_no_interval_are_reported(self):
        # Nothing can be written: the file states no interval and half an
        # inch is the reader's default rather than Publisher's, so these
        # tabs keep the reader's grid and the report says so.
        document = self.document("a\tb", "c\td")
        convert._apply_tab_stops(document, self.structure(("a\tb\r", ()), ("c\td\r", ())))
        self.assertEqual([p.tab_stops for p in self.paragraphs(document)], [[], []])
        self.assertEqual(len(document.warnings), 1)
        self.assertIn("2 tabbed paragraph(s)", document.warnings[0])

    def test_a_document_whose_tabs_are_all_placed_is_not_warned_about(self):
        document = self.document("a\tb")
        convert._apply_tab_stops(document, self.structure(("a\tb\r", ((9.0, "left"),))))
        self.assertEqual(document.warnings, [])

    def test_no_structure_at_all_changes_nothing(self):
        document = self.document("a\tb")
        convert._apply_tab_stops(document, None)
        self.assertEqual(self.paragraphs(document)[0].tab_stops, [])
        self.assertEqual(document.warnings, [])

    def test_a_paragraph_inside_a_table_cell_is_reached(self):
        table = model.Table(x=0.0, y=0.0, width=100.0, height=20.0)
        cell = model.TableCell(row=0, column=0)
        paragraph = model.Paragraph()
        paragraph.spans.append(model.Span(text="a\tb"))
        cell.story.paragraphs.append(paragraph)
        table.cells = [cell]
        document = model.Document(pages=[page_with(table)])
        convert._apply_tab_stops(document, self.structure(("a\tb\r", ((9.0, "left"),))))
        self.assertEqual(paragraph.tab_stops, [model.TabStop(9.0)])


def section_chunk(interval_emu=None, block_type=0x22) -> bytes:
    """A `SGP ` chunk stating the document's default tab interval, or none."""
    if interval_emu is None:
        return struct.pack("<I", 4)
    block = struct.pack("<BB", 0x00, block_type) + struct.pack("<I", interval_emu)
    return struct.pack("<I", 4 + len(block)) + block


class DefaultTabStopTest(unittest.TestCase):
    """Reading the document-wide interval out of the Quill stream."""

    def read(self, *chunks):
        return pubfile._default_tab_stop(quill_stream(*chunks))

    def test_an_interval_is_read_as_points(self):
        # 359410 EMU, which is what three of the corpus files state.
        self.assertAlmostEqual(self.read(("SGP ", section_chunk(359410))), 28.3)

    def test_a_chunk_stating_no_block_reads_nothing(self):
        self.assertIsNone(self.read(("SGP ", section_chunk())))

    def test_a_stream_with_no_section_chunk_reads_nothing(self):
        self.assertIsNone(self.read(("TEXT", b"")))

    def test_a_block_of_another_type_is_not_the_interval(self):
        self.assertIsNone(self.read(("SGP ", section_chunk(359410, block_type=0x20))))

    def test_a_reading_outside_publishers_own_range_is_refused(self):
        # Publisher allows 1 to 1584 points; anything else says the block
        # is not what it looks like.
        self.assertIsNone(self.read(("SGP ", section_chunk(1))))
        self.assertIsNone(self.read(("SGP ", section_chunk(1585 * 12700))))

    def test_a_truncated_chunk_does_not_raise(self):
        self.assertIsNone(self.read(("SGP ", b"\x40\x00\x00\x00\x00")))


class DefaultTabGridTest(unittest.TestCase):
    """Writing the document's default grid out as an explicit ruler."""

    def document(self, *, width=200.0, page_width=612.0, indent=0.0):
        frame = model.TextFrame(x=0.0, y=0.0, width=width, height=100.0)
        paragraph = model.Paragraph(margin_left=indent)
        paragraph.spans.append(model.Span(text="a\tb"))
        frame.story.paragraphs.append(paragraph)
        page = model.Page(width=page_width, height=792.0)
        page.items.append(frame)
        return model.Document(pages=[page]), paragraph

    def apply(self, interval, *, stops=(), **kwargs):
        document, paragraph = self.document(**kwargs)
        convert._apply_tab_stops(
            document,
            pubfile.FileStructure(
                paragraph_stops=list(stops), default_tab_stop=interval
            ),
        )
        return document, paragraph

    def test_a_stated_interval_becomes_a_ruler_of_left_stops(self):
        _document, paragraph = self.apply(50.0)
        self.assertEqual(
            paragraph.tab_stops,
            [model.TabStop(50.0), model.TabStop(100.0),
             model.TabStop(150.0), model.TabStop(200.0)],
        )

    def test_the_ruler_covers_the_frame_rather_than_fitting_inside_it(self):
        # 200pt of width holds two 80pt stops and 40pt of remainder. The
        # third stop is past the edge, where Publisher's endless grid has
        # one too; without it a tab in that last 40pt falls back on the
        # reader's own grid, which is the error being fixed.
        _document, paragraph = self.apply(80.0, width=200.0)
        self.assertEqual(
            [stop.position for stop in paragraph.tab_stops], [80.0, 160.0, 240.0]
        )

    def test_a_frame_narrower_than_one_stop_still_gets_the_first(self):
        # Both the frame and the page are narrower than the interval, so
        # there is no multiple inside either. Publisher would send the tab
        # to the next one regardless, off the frame and onto the next line.
        _document, paragraph = self.apply(50.0, width=20.0, page_width=30.0)
        self.assertEqual([stop.position for stop in paragraph.tab_stops], [50.0])

    def test_an_indent_neither_moves_nor_shortens_the_ruler(self):
        # A stop is measured from the frame's text edge, so every paragraph
        # in a frame is on one grid; an indent only makes the stops behind
        # it unreachable.
        _document, paragraph = self.apply(50.0, width=200.0, indent=60.0)
        self.assertEqual(
            [stop.position for stop in paragraph.tab_stops],
            [50.0, 100.0, 150.0, 200.0],
        )

    def test_the_readers_own_grid_needs_no_ruler_and_no_warning(self):
        # A document stating half an inch is already where InDesign puts
        # it, so there is nothing to write and nothing to check.
        document, paragraph = self.apply(pubfile.READER_DEFAULT_TAB_STOP)
        self.assertEqual(paragraph.tab_stops, [])
        self.assertEqual(document.warnings, [])

    def test_a_file_stating_no_interval_gets_no_ruler_but_is_reported(self):
        # Half an inch is the reader's default, not Publisher's: all
        # thirteen files written from scratch on the metric install state
        # an interval of their own, so a file stating none leaves its
        # grid unknown.
        document, paragraph = self.apply(None)
        self.assertEqual(paragraph.tab_stops, [])
        self.assertEqual(len(document.warnings), 1)
        self.assertIn("1 tabbed paragraph(s)", document.warnings[0])
        self.assertIn("36pt", document.warnings[0])

    def test_a_stop_the_file_states_wins_over_the_grid(self):
        _document, paragraph = self.apply(50.0, stops=[("a\tb\r", ((17.0, "right"),))])
        self.assertEqual(paragraph.tab_stops, [model.TabStop(17.0, "right")])

    def test_a_frame_too_narrow_to_believe_falls_back_to_the_page(self):
        # One corpus frame reports 5.5pt of width while holding 34
        # paragraphs of text; its tabs are better served by a long ruler
        # than by none.
        _document, paragraph = self.apply(50.0, width=5.5, page_width=300.0)
        self.assertEqual(
            [stop.position for stop in paragraph.tab_stops],
            [50.0, 100.0, 150.0, 200.0, 250.0, 300.0],
        )

    def test_a_ruler_is_capped_so_a_tiny_interval_cannot_flood_the_file(self):
        _document, paragraph = self.apply(1.0, width=100000.0)
        self.assertEqual(len(paragraph.tab_stops), convert._MAX_RULER_STOPS)

    def test_the_grid_is_named_in_a_warning_because_it_is_not_confirmed(self):
        document, _paragraph = self.apply(8.0787)
        self.assertEqual(len(document.warnings), 1)
        self.assertIn("8.08pt", document.warnings[0])
        self.assertIn("1 paragraph(s)", document.warnings[0])

    def test_a_table_cell_is_ruled_across_the_columns_it_spans(self):
        table = model.Table(x=0.0, y=0.0, width=300.0, height=20.0)
        table.column_widths = [100.0, 100.0, 100.0]
        cell = model.TableCell(row=0, column=0, column_span=2)
        paragraph = model.Paragraph()
        paragraph.spans.append(model.Span(text="a\tb"))
        cell.story.paragraphs.append(paragraph)
        table.cells = [cell]
        document = model.Document(pages=[page_with(table)])
        convert._apply_tab_stops(
            document, pubfile.FileStructure(default_tab_stop=50.0)
        )
        self.assertEqual(
            [stop.position for stop in paragraph.tab_stops],
            [50.0, 100.0, 150.0, 200.0],
        )


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


def wordart_bools(**flags) -> int:
    """WordArt's packed booleans: the low half values, the high half which
    of them the file states at all. Bit n is property 0xFF - n."""
    bits = {
        "strikethrough": 0x00, "small_caps": 0x01, "shadow": 0x02,
        "underline": 0x03, "italic": 0x04, "bold": 0x05,
        "stretch": 0x0A,
    }
    value = stated = 0
    for name, on in flags.items():
        stated |= 1 << bits[name]
        if on:
            value |= 1 << bits[name]
    return stated << 16 | value


def wordart_shape(
    text="Kerkdiensten", font="Monotype Corsiva", size=20.0, rotation=None,
    box=(-100, -50, 100, -20), anchor=True, spacing=None, bools=None,
    shape_type=None, shape_seq=None, wrap_distance=None,
) -> bytes:
    """One Escher shape container carrying WordArt, as Publisher writes it."""
    entries = []
    if text is not None:
        entries.append((0x00C0, text.encode("utf-16-le") + b"\x00\x00"))
    if font is not None:
        entries.append((0x00C5, font.encode("utf-16-le") + b"\x00\x00"))
    if size is not None:
        entries.append((0x00C3, int(size * 65536)))
    if spacing is not None:
        entries.append((0x00C4, int(spacing * 65536)))
    if bools is not None:
        entries.append((0x00FF, bools))
    if rotation is not None:
        entries.append((0x0004, int(rotation * 65536)))
    if wrap_distance is not None:
        # dxWrapDistLeft: how far the text kept clear of this shape's left
        # edge, in EMU. Publisher writes one only where the wrap is on.
        entries.append((0x0384, int(wrap_distance * 12700)))
    children = [_escher_properties(entries)]
    if shape_type is not None:
        children.insert(0, _escher(0xF00A, b"", instance=shape_type))
    if anchor:
        children.append(_escher_values(0xF010, [
            (0x2001, box[0] * 12700), (0x2002, box[1] * 12700),
            (0x2003, box[2] * 12700), (0x2004, box[3] * 12700),
        ]))
    if shape_seq is not None:
        # Client data, which is where the shape's own seqnum lives -- the
        # number its page chunk lists it by, and the only thing tying an
        # Escher shape to the page it is on.
        children.append(_escher_values(0xF011, [(0x6801, shape_seq)]))
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

    def test_a_shape_stating_no_size_says_so_rather_than_inventing_one(self):
        # The parser used to work a size back from the band here, out of
        # two averaged constants. Sizing needs the font the words are set
        # in, which is `convert`'s to read: `WordArtSizingTest` in
        # test_convert.py now holds the assertions that were here.
        art = self.one(text="D", size=None, box=(0, 0, 45, 40))
        self.assertTrue(art.fitted)
        self.assertIsNone(art.size)

    def test_the_band_is_reported_whatever_shape_it_is(self):
        # The band is what `convert` fits the headline into, so both of its
        # dimensions have to survive the parse even when no size does.
        art = self.one(text="Kerkdiensten", size=None, box=(0, 0, 120, 40))
        self.assertIsNone(art.size)
        self.assertAlmostEqual(art.width, 120.0)
        self.assertAlmostEqual(art.height, 40.0)

    def test_a_stated_size_is_still_the_file_s_own(self):
        # Only the invented number changes here. What the file states is
        # left alone.
        art = self.one(size=20.0, box=(0, 0, 200, 40))
        self.assertFalse(art.fitted)
        self.assertAlmostEqual(art.size, 20.0)

    def test_a_stated_wrap_distance_means_the_copy_flows_around_it(self):
        # The room a wrap leaves is the only thing in the file that says
        # there is one: a shape the text runs under has no distance to
        # keep, so it states none.
        self.assertTrue(self.one(wrap_distance=2.88).wraps_text)

    def test_a_shape_stating_no_distance_is_one_the_text_runs_under(self):
        self.assertFalse(self.one().wraps_text)

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

    def test_a_line_break_is_carried_however_it_is_written(self):
        # How many lines a headline is set on decides its size, so the
        # breaks have to survive the parse verbatim -- the splitting itself
        # belongs to `convert`, which is where the sizing moved.
        for break_ in ("\r\n", "\r", "\n"):
            with self.subTest(break_=break_):
                art = self.one(text=f"o{break_}t", size=None)
                self.assertEqual(art.text, f"o{break_}t")


class WordArtBooleanTest(unittest.TestCase):
    """The sixteen booleans WordArt packs into one property.

    The low half holds the values and the high half says which of them the
    file states at all, so a bit left out of the high half is unstated
    rather than false. MS-ODRAW numbers a boolean set from the highest
    property id down, which is why bit n is property 0xFF - n.
    """

    def one(self, **kwargs):
        found = pubfile._wordart_shapes(wordart_shape(**kwargs))
        self.assertEqual(len(found), 1)
        return found[0]

    def test_a_shape_stating_nothing_is_not_styled(self):
        art = self.one()
        self.assertEqual(
            (art.bold, art.italic, art.underline, art.strikethrough),
            (False, False, False, False),
        )

    def test_each_boolean_is_read_from_its_own_bit(self):
        for name in ("bold", "italic", "underline", "strikethrough"):
            with self.subTest(name=name):
                art = self.one(bools=wordart_bools(**{name: True}))
                self.assertTrue(getattr(art, name))
                others = {"bold", "italic", "underline", "strikethrough"} - {name}
                for other in others:
                    self.assertFalse(getattr(art, other))

    def test_a_value_bit_without_its_stated_bit_is_not_believed(self):
        # The low half alone is a value the file never claimed to state.
        art = self.one(bools=1 << 0x05)
        self.assertFalse(art.bold)

    def test_a_stated_bit_set_to_false_stays_false(self):
        art = self.one(bools=wordart_bools(bold=False, italic=True))
        self.assertFalse(art.bold)
        self.assertTrue(art.italic)

    def test_the_corpus_pairing_of_italic_and_bold_reads_apart(self):
        # 40 of the corpus's 48 shapes are italic and 1 is bold; reading
        # one as the other would restyle every headline in the file.
        art = self.one(bools=wordart_bools(italic=True, bold=False))
        self.assertTrue(art.italic)
        self.assertFalse(art.bold)


class WordArtShapeTypeTest(unittest.TestCase):
    """Whether the words are bent, and into what."""

    def one(self, **kwargs):
        found = pubfile._wordart_shapes(wordart_shape(**kwargs))
        self.assertEqual(len(found), 1)
        return found[0]

    def test_a_shape_with_no_shape_record_is_not_bent(self):
        self.assertIsNone(self.one().warp)

    def test_plain_wordart_is_not_bent(self):
        self.assertIsNone(self.one(shape_type=136).warp)

    def test_a_preset_is_named_the_way_publishers_gallery_names_it(self):
        self.assertEqual(self.one(shape_type=147).warp, "button curve")
        self.assertEqual(self.one(shape_type=156).warp, "wave 1")
        self.assertEqual(self.one(shape_type=175).warp, "can down")

    def test_a_shape_type_that_is_not_wordart_at_all_names_no_warp(self):
        self.assertIsNone(self.one(shape_type=1).warp)


class WordArtSpacingTest(unittest.TestCase):
    """Character spacing, stated as a multiple of normal."""

    def one(self, **kwargs):
        found = pubfile._wordart_shapes(wordart_shape(**kwargs))
        self.assertEqual(len(found), 1)
        return found[0]

    def test_a_shape_stating_no_spacing_states_none(self):
        self.assertIsNone(self.one().spacing)

    def test_normal_spacing_is_not_carried_as_a_difference(self):
        self.assertIsNone(self.one(spacing=1.0).spacing)

    def test_loose_spacing_is_read_as_its_multiple(self):
        # 1.2 is what Publisher's gallery calls Loose, and what 36 of the
        # corpus's 48 shapes state.
        self.assertAlmostEqual(self.one(spacing=1.2).spacing, 1.2, places=4)

    def test_tight_spacing_is_read_as_its_multiple(self):
        self.assertAlmostEqual(self.one(spacing=0.8).spacing, 0.8, places=4)


class SentenceTest(unittest.TestCase):
    """Clauses joined so a report reads as prose rather than a list."""

    def test_one_clause_stands_alone(self):
        self.assertEqual(convert._sentence(["only this"]), "only this")

    def test_two_clauses_are_joined_with_and(self):
        self.assertEqual(convert._sentence(["one", "two"]), "one, and two")

    def test_more_clauses_keep_the_and_for_the_last(self):
        self.assertEqual(
            convert._sentence(["one", "two", "three"]), "one, two, and three"
        )


class ShapeSeqnumReadingTest(unittest.TestCase):
    """A shape's own seqnum, which is what says which page it is on."""

    def test_client_data_carries_the_shapes_seqnum(self):
        art = pubfile._wordart_shapes(wordart_shape(shape_seq=311))[0]
        self.assertEqual(art.shape_seq, 311)

    def test_a_shape_stating_no_client_data_has_no_seqnum(self):
        self.assertIsNone(pubfile._wordart_shapes(wordart_shape())[0].shape_seq)

    def test_every_shape_with_a_seqnum_and_a_box_becomes_an_anchor(self):
        # Not just WordArt: any shape libmspub *did* report is what ties a
        # page chunk to the page libmspub emitted for it.
        stream = (
            wordart_shape(text=None, shape_seq=297, box=(-100, -50, 100, -20))
            + wordart_shape(text="Headline", shape_seq=311, box=(0, 0, 40, 60))
        )
        anchors = pubfile._shape_anchors(stream)
        self.assertEqual([a.shape_seq for a in anchors], [297, 311])
        self.assertAlmostEqual(anchors[0].centre_x, 0.0)
        self.assertAlmostEqual(anchors[0].centre_y, -35.0)
        self.assertAlmostEqual(anchors[1].centre_x, 20.0)
        self.assertAlmostEqual(anchors[1].centre_y, 30.0)
        # The box's size, not only its middle: it is the file's own second
        # opinion on the size libmspub reports.
        self.assertAlmostEqual(anchors[0].width, 200.0)
        self.assertAlmostEqual(anchors[0].height, 30.0)
        self.assertAlmostEqual(anchors[1].width, 40.0)
        self.assertAlmostEqual(anchors[1].height, 60.0)

    def test_a_shape_missing_either_half_is_not_an_anchor(self):
        self.assertEqual(pubfile._shape_anchors(wordart_shape(shape_seq=None)), [])
        self.assertEqual(
            pubfile._shape_anchors(wordart_shape(shape_seq=311, anchor=False)), []
        )

    def test_a_truncated_stream_does_not_raise(self):
        payload = wordart_shape(shape_seq=311)
        for cut in range(1, len(payload)):
            with self.subTest(cut=cut):
                pubfile._shape_anchors(payload[:cut])

    def test_the_page_a_seqnum_belongs_to_is_looked_up(self):
        structure = pubfile.FileStructure(shape_pages={311: 266, 574: 308})
        self.assertEqual(structure.page_seq_of(311), 266)
        self.assertEqual(structure.page_seq_of(574), 308)
        self.assertIsNone(structure.page_seq_of(999))
        self.assertIsNone(structure.page_seq_of(None))

    def test_a_real_file_states_the_page_of_the_shape_it_never_reported(self):
        # The whole of backlog §10 rests on this: 'I venerdì 2006 di Avvento'
        # has no shape in the event stream, and the file still says which
        # page it is on -- the same page as the sibling that *was* reported.
        structure = pubfile.read_structure(SAMPLES / "Cantico_dei_Cantici.pub")
        orphan = next(a for a in structure.wordart if "Avvento" in a.text)
        sibling = next(
            a for a in structure.wordart if a.text == "Il Cantico dei Cantici"
        )
        self.assertEqual(orphan.shape_seq, 311)
        self.assertEqual(structure.page_seq_of(orphan.shape_seq), 266)
        self.assertEqual(
            structure.page_seq_of(sibling.shape_seq),
            structure.page_seq_of(orphan.shape_seq),
        )

    def test_no_page_lists_a_shape_twice(self):
        # A shape belonging to one page is what makes the lists usable as
        # "which page is this on"; two pages claiming one shape would not be.
        for name in ("Cantico_dei_Cantici.pub", "MISSAL MARIANA E PEDRO.pub"):
            with self.subTest(source=name):
                structure = pubfile.read_structure(SAMPLES / name)
                listed = [
                    seqnum
                    for page in list(structure.pages) + list(structure.masters.values())
                    for seqnum in page.shape_seqnums
                ]
                self.assertEqual(len(listed), len(set(listed)))


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

    def test_the_frame_is_centred_on_the_band(self):
        # The band is where the words go, and the frame is centred on it
        # rather than equal to it -- see the wrap allowance below. Centred
        # and unfilled, the extra height shows as nothing while they fit.
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(self.art()))
        frame = document.pages[0].items[0]
        self.assertAlmostEqual(frame.x, 100.0)
        self.assertAlmostEqual(frame.width, 200.0)
        self.assertAlmostEqual(frame.x + frame.width / 2.0, 200.0)
        self.assertAlmostEqual(frame.y + frame.height / 2.0, 215.0)

    def test_the_frame_is_the_band_and_no_larger(self):
        # Giving it room for a wrapped headline was tried and taken back
        # out: the extra height only holds the words in place if the
        # reader centres them vertically, and if it does not the headline
        # hangs half a band high. Growing it is not the answer to a band
        # too short for the type either -- the frame is what the copy
        # flows around, and the first baseline below is what places the
        # line inside it.
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(self.art()))
        frame = document.pages[0].items[0]
        self.assertAlmostEqual(frame.width, 200.0)
        self.assertAlmostEqual(frame.height, 30.0)

    def test_a_headline_the_copy_flows_around_pushes_it_aside(self):
        # A dropped initial is a shape floating over the column it begins,
        # and its paragraph is not indented to make room: without a wrap
        # the letter is drawn straight through its own first lines.
        document = self.document_with(self.guides())
        convert._recover_wordart(
            document, self.structure_with(self.art(wraps_text=True))
        )
        self.assertTrue(document.pages[0].items[0].wrap_text)

    def test_a_headline_that_states_no_wrap_leaves_the_text_where_it_is(self):
        # Asking for a wrap on every headline is worse than asking for
        # none: a band that merely clips the corner of a date box would
        # push the date out of it.
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(self.art()))
        self.assertFalse(document.pages[0].items[0].wrap_text)

    def test_the_first_line_states_the_baseline_it_sits_on(self):
        # A reader left to itself hangs the first baseline a whole font
        # ascent below the frame's top, and a band is shorter than that --
        # so the baseline lands past the bottom of its frame and the
        # headline is not drawn at all. What is stated instead is where
        # in the band the baseline goes: a headline is sized so its ink
        # fills the band, so that is the share of the ink above the
        # baseline. Ink of 0.75 em fills a 30pt band at 40pt, and 0.60 em
        # of that 40 is 24pt down.
        document = self.document_with(self.guides())
        with self.measured(ink=0.75, above=0.60):
            convert._recover_wordart(
                document, self.structure_with(self.art(size=None, stretch=True))
            )
        frame = document.pages[0].items[0]
        self.assertTrue(frame.first_baseline_from_leading)
        self.assertAlmostEqual(frame.story.paragraphs[0].line_spacing_pt, 24.0)

    def test_the_stated_baseline_never_reaches_past_the_band(self):
        # It cannot, and this is why: the size is what makes the ink fill
        # the band, so the part of the ink above the baseline is a share of
        # a band's worth. A baseline past the frame's bottom is the whole
        # failure being fixed here.
        document = self.document_with(self.guides())
        with self.measured(ink=0.75, above=0.75):
            convert._recover_wordart(
                document, self.structure_with(self.art(size=None, stretch=True))
            )
        frame = document.pages[0].items[0]
        self.assertLessEqual(frame.story.paragraphs[0].line_spacing_pt, 30.0)

    def test_each_later_line_of_a_stacked_headline_takes_its_share(self):
        # The first line states the baseline; every line after it steps
        # down by the band's own share, which puts each one's ink in its
        # own slice of the band.
        document = self.document_with(self.guides())
        with self.measured(ink=0.75, above=0.60):
            convert._recover_wordart(
                document,
                self.structure_with(self.art(
                    text="one\r\ntwo\r\nthree", size=None, stretch=True
                )),
            )
        spacing = [
            paragraph.line_spacing_pt
            for paragraph in document.pages[0].items[0].story.paragraphs
        ]
        # 30pt of band over three lines is 10pt each, and the first states
        # its baseline instead: 0.60 of the 13.33pt those 10pt buy.
        self.assertAlmostEqual(spacing[0], 10.0 / 0.75 * 0.60)
        self.assertAlmostEqual(spacing[1], 10.0)
        self.assertAlmostEqual(spacing[2], 10.0)

    def test_the_words_are_centred_across_the_band_and_placed_down_it(self):
        # Across, because WordArt fits its glyphs to the shape and the band
        # is the words rather than a box they sit in one corner of. Down
        # it, because a reader centres nothing it has decided will not fit,
        # and the stated baseline is what makes it fit.
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(self.art()))
        frame = document.pages[0].items[0]
        self.assertEqual(frame.vertical_align, "top")
        self.assertEqual(frame.story.paragraphs[0].align, "center")

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

    def span(self, document):
        return document.pages[0].items[0].story.paragraphs[0].spans[0]

    def test_the_styling_the_shape_carries_ends_up_on_the_words(self):
        # WordArt states these on the shape rather than on the text, so
        # they are lost with the shape unless they are put back here.
        document = self.document_with(self.guides())
        convert._recover_wordart(
            document,
            self.structure_with(
                self.art(bold=True, italic=True, underline=True, strikethrough=True)
            ),
        )
        span = self.span(document)
        self.assertEqual(
            (span.bold, span.italic, span.underline, span.strikethrough),
            (True, True, True, True),
        )

    def test_an_unstyled_headline_stays_unstyled(self):
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(self.art()))
        span = self.span(document)
        self.assertEqual(
            (span.bold, span.italic, span.underline, span.strikethrough),
            (False, False, False, False),
        )

    # These tests are about what the *report* says, so what the headline
    # measures has to be stated rather than read off whichever fonts this
    # machine has. Monotype Corsiva is installed on the machine this was
    # written on and not on the build runner, which is exactly the way a
    # test like this fails somewhere else.
    def measured(self, source="font", ink=0.8, above=0.6, advance=0.5):
        """Measure what the test states, not what this machine has.

        The size a headline is set at and the baseline it sits on are both
        worked out from the font, and the two faces 46 of the corpus's
        headlines use ship with Office rather than with an operating
        system -- so a test asserting either has to state the metrics
        behind it or it passes here and fails on a build runner.
        """
        def measure(family, bold, italic, text):
            return fontmetrics.Metrics(
                ink_per_em=ink,
                ink_above_baseline_per_em=above,
                width_per_em=advance * max(len(text), 1),
                mean_advance_per_em=advance,
                source=source,
            )
        return mock.patch.object(convert.fontmetrics, "measure", measure)

    # -- what the report says -------------------------------------------
    #
    # Only what a person has to act on. A headline that was measured, set
    # straight because the file never bent it, and had its duplicate paint
    # dropped is a converted headline, not a warning -- fifteen of those
    # used to print a paragraph explaining, four different ways, that
    # nothing had gone wrong. The count reaches the detail line instead.

    def test_a_clean_recovery_warns_about_nothing(self):
        with self.measured():
            document = self.document_with(self.guides())
            convert._recover_wordart(document, self.structure_with(self.art()))
            self.assertEqual(document.warnings, [])

    def test_a_clean_recovery_is_still_counted(self):
        with self.measured():
            document = self.document_with(self.guides())
            convert._recover_wordart(document, self.structure_with(self.art()))
            self.assertEqual(document.wordart, 1)

    def test_a_dropped_repeat_is_not_a_warning(self):
        # Dropping the second paint of the same guides is what stops the
        # outline drawing rules across the words. It is the fix, not a loss.
        with self.measured():
            document = self.document_with(self.guides(), self.outlined())
            convert._recover_wordart(document, self.structure_with(self.art()))
            self.assertEqual(document.warnings, [])

    def test_a_headline_at_normal_spacing_is_not_reported(self):
        with self.measured():
            document = self.document_with(self.guides())
            convert._recover_wordart(document, self.structure_with(self.art(spacing=1.2)))
            self.assertEqual(document.warnings, [])

    def test_a_bent_headline_is_reported_and_named(self):
        # The one real loss, and it is rare: 47 of the corpus's 48 shapes
        # are not bent at all.
        with self.measured():
            document = self.document_with(self.guides())
            convert._recover_wordart(
                document, self.structure_with(self.art(warp="button curve"))
            )
            self.assertEqual(len(document.warnings), 1)
            warning = document.warnings[0]
            self.assertIn("bent", warning)
            self.assertIn("button curve", warning)
            self.assertIn("Kerkdiensten", warning)

    def test_an_unmeasured_font_is_reported_by_name(self):
        # Naming the font is the actionable part: installing it is the fix.
        document = self.document_with(self.guides())
        convert._recover_wordart(
            document, self.structure_with(self.art(font=self.UNMEASURABLE))
        )
        self.assertEqual(len(document.warnings), 1)
        self.assertIn(self.UNMEASURABLE, document.warnings[0])
        self.assertIn("sized from averages", document.warnings[0])

    # A family no machine has, so these measure on the global averages
    # rather than on whichever headline faces happen to be installed here.
    # What tracking becomes for a font that *can* be read is
    # `WordArtTrackingTest` in test_convert.py.
    UNMEASURABLE = "No Such Face"

    def test_loose_spacing_becomes_tracking(self):
        # 1.2 is Publisher's Loose. IDML states the space added, in
        # thousandths of an em, against a multiple of the glyph advance.
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(
            self.art(spacing=1.2, font=self.UNMEASURABLE)
        ))
        self.assertAlmostEqual(self.span(document).tracking, 100.0)

    def test_tight_spacing_becomes_negative_tracking(self):
        document = self.document_with(self.guides())
        convert._recover_wordart(document, self.structure_with(
            self.art(spacing=0.8, font=self.UNMEASURABLE)
        ))
        self.assertAlmostEqual(self.span(document).tracking, -100.0)

    def test_a_headline_at_normal_spacing_states_no_tracking(self):
        document = self.document_with(self.guides())
        for spacing in (None, 1.0):
            with self.subTest(spacing=spacing):
                document = self.document_with(self.guides())
                convert._recover_wordart(
                    document, self.structure_with(self.art(spacing=spacing))
                )
                self.assertIsNone(self.span(document).tracking)

    def test_a_shape_with_nowhere_to_go_is_named_rather_than_dropped(self):
        document = self.document_with()
        convert._recover_wordart(
            document, self.structure_with(self.art(text="I venerdì di Avvento"))
        )
        warning = " ".join(document.warnings)
        self.assertIn("not placed", warning)
        self.assertIn("I venerdì di Avvento", warning)

    def test_an_unplaced_shape_is_named_with_the_shape_it_was_bent_into(self):
        # The one warped headline in the corpus is also the one libmspub
        # reports no shape for, so this is the branch that has to say it:
        # retyping a bent headline is a different job from retyping a
        # straight one.
        document = self.document_with()
        convert._recover_wordart(
            document,
            self.structure_with(
                self.art(text="I venerdì di Avvento", warp="button curve")
            ),
        )
        self.assertIn("(button curve)", " ".join(document.warnings))

    def test_an_unplaced_straight_headline_is_named_without_a_shape(self):
        document = self.document_with()
        convert._recover_wordart(
            document, self.structure_with(self.art(text="Kerkdiensten"))
        )
        warning = next(w for w in document.warnings if "not placed" in w)
        self.assertIn("'Kerkdiensten'", warning)
        self.assertNotIn("(", warning.split("so ", 1)[1])

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

    def outlined(self, **style) -> model.Path:
        """The same guides, painted as an outline rather than a fill."""
        path = self.guides(**style)
        path.style.fill = None
        path.style.stroke = (54, 27, 0)
        path.style.stroke_width = 0.75
        return path

    def test_guides_that_are_stroked_rather_than_filled_still_match(self):
        # Cantico's headline arrives this way: its glyphs are filled with a
        # texture, so the only colour on the shape is the outline. Reading
        # the fill alone left the words behind and drew the guides instead.
        document = self.document_with(self.outlined())
        convert._recover_wordart(document, self.structure_with(self.art()))
        frame = document.pages[0].items[0]
        self.assertIsInstance(frame, model.TextFrame)
        self.assertEqual(frame.story.paragraphs[0].spans[0].text, "Kerkdiensten")

    def test_with_no_fill_the_outline_colours_the_words(self):
        document = self.document_with(self.outlined())
        convert._recover_wordart(document, self.structure_with(self.art()))
        span = document.pages[0].items[0].story.paragraphs[0].spans[0]
        self.assertEqual(span.color, (54, 27, 0))
        # Spent on the words, so not also drawn around them.
        self.assertIsNone(span.stroke)

    def test_repeated_paints_of_one_shape_make_one_frame(self):
        # libmspub draws once per paint, so a headline that is filled and
        # outlined reports the same guides twice. Taking each on its own
        # wrote the words twice over, or left rules across them.
        document = self.document_with(self.guides(), self.outlined())
        convert._recover_wordart(document, self.structure_with(self.art()))
        items = document.pages[0].items
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], model.TextFrame)

    def test_the_paints_are_merged_rather_than_one_winning(self):
        document = self.document_with(self.guides(), self.outlined())
        convert._recover_wordart(document, self.structure_with(self.art()))
        span = document.pages[0].items[0].story.paragraphs[0].spans[0]
        self.assertEqual(span.color, (0, 51, 128))       # from the fill pass
        self.assertEqual(span.stroke, (54, 27, 0))       # from the outline pass
        self.assertAlmostEqual(span.stroke_width, 0.75)

    def test_a_gradient_on_the_shape_stays_a_gradient_on_the_words(self):
        ramp = model.Gradient(
            stops=(
                model.GradientStop(location=0.0, color=(145, 56, 1)),
                model.GradientStop(location=100.0, color=(255, 209, 125)),
            ),
        )
        document = self.document_with(self.guides(gradient=ramp))
        convert._recover_wordart(document, self.structure_with(self.art()))
        span = document.pages[0].items[0].story.paragraphs[0].spans[0]
        self.assertEqual(span.gradient, ramp)
        # The flat first stop stays too, for anything not gradient-aware.
        self.assertEqual(span.color, (0, 51, 128))

    def test_a_stroked_guide_pair_is_not_reported_as_unrenderable(self):
        # It draws its edges, so "encloses no area and draws nothing" would
        # be the wrong complaint about it.
        document = self.document_with(self.outlined())
        convert._check_unrenderable_paths(document)
        self.assertFalse(any("enclose no area" in w for w in document.warnings))


class PageByChunkTest(unittest.TestCase):
    """Which page libmspub emitted for each of the file's page chunks.

    Not the chunk order: in every newsletter in the corpus the two are a
    permutation of one another, so the mapping is measured from the shapes
    both sides describe.
    """

    def document(self, *pages) -> model.Document:
        return model.Document(pages=list(pages))

    def page_holding(self, *centres) -> model.Page:
        """A page of 612 x 792 with an item at each page-relative centre."""
        page = model.Page(width=612.0, height=792.0)
        for x, y in centres:
            page.items.append(
                model.Rectangle(x=306.0 + x - 5.0, y=396.0 + y - 5.0,
                                width=10.0, height=10.0)
            )
        return page

    def structure(self, anchors, shape_pages) -> pubfile.FileStructure:
        return pubfile.FileStructure(
            anchors=[pubfile.ShapeAnchor(*a) for a in anchors],
            shape_pages=shape_pages,
        )

    def test_a_chunk_is_the_page_its_shapes_were_drawn_on(self):
        document = self.document(
            self.page_holding((-100.0, -200.0)),
            self.page_holding((50.0, 75.0)),
        )
        found = convert._page_by_chunk(document, self.structure(
            [(311, -100.0, -200.0), (574, 50.0, 75.0)], {311: 266, 574: 308}
        ))
        self.assertEqual(found, {266: 0, 308: 1})

    def test_chunk_order_is_not_taken_for_page_order(self):
        # The kerkbode case: chunk 266 is libmspub's page 2 and chunk 335
        # its page 0. Anything indexing the chunk list would have these
        # exactly the wrong way round.
        document = self.document(
            self.page_holding((10.0, 10.0)),
            self.page_holding((20.0, 20.0)),
            self.page_holding((30.0, 30.0)),
        )
        found = convert._page_by_chunk(document, self.structure(
            [(323, 30.0, 30.0), (472, 10.0, 10.0)], {323: 266, 472: 335}
        ))
        self.assertEqual(found, {266: 2, 335: 0})

    def test_several_shapes_of_one_chunk_agreeing_is_still_one_page(self):
        document = self.document(
            self.page_holding((10.0, 10.0), (40.0, 40.0)),
            self.page_holding((20.0, 20.0)),
        )
        found = convert._page_by_chunk(document, self.structure(
            [(1, 10.0, 10.0), (2, 40.0, 40.0)], {1: 308, 2: 308}
        ))
        self.assertEqual(found, {308: 0})

    def test_a_chunk_whose_shapes_disagree_settles_nothing(self):
        document = self.document(
            self.page_holding((10.0, 10.0)),
            self.page_holding((20.0, 20.0)),
        )
        found = convert._page_by_chunk(document, self.structure(
            [(1, 10.0, 10.0), (2, 20.0, 20.0)], {1: 308, 2: 308}
        ))
        self.assertEqual(found, {})

    def test_a_shape_drawn_on_every_page_settles_nothing(self):
        # Which is how a master keeps out of this: libmspub replays its
        # shapes onto every page, so the anchor matches all of them.
        document = self.document(
            self.page_holding((10.0, 10.0)),
            self.page_holding((10.0, 10.0)),
        )
        found = convert._page_by_chunk(document, self.structure(
            [(1, 10.0, 10.0)], {1: 263}
        ))
        self.assertEqual(found, {})

    def test_a_shape_no_page_lists_settles_nothing(self):
        document = self.document(self.page_holding((10.0, 10.0)))
        found = convert._page_by_chunk(document, self.structure(
            [(1, 10.0, 10.0)], {}
        ))
        self.assertEqual(found, {})

    def test_the_two_sides_need_only_agree_to_a_fraction_of_a_point(self):
        # Both measure the same EMU by different routes, so requiring an
        # exact match would turn rounding into a cliff.
        document = self.document(self.page_holding((10.0, 10.0)))
        found = convert._page_by_chunk(document, self.structure(
            [(1, 10.3, 9.7)], {1: 266}
        ))
        self.assertEqual(found, {266: 0})


class MasterAttributionTest(unittest.TestCase):
    """Which master each page applies, taken from that page's own chunk.

    The file's chunk list is not in libmspub's page order (§14), so the
    master a page applies has to be reached through the mapping the shapes
    measure rather than by indexing the list. Where the mapping settles
    nothing, what the remaining chunks agree on is still a fact; where they
    disagree, the page is left alone.
    """

    def page_at(self, x: float) -> model.Page:
        """A page with two leading items and one of its own, at `x`.

        The leading pair is what a master's shapes look like once replayed:
        identical on every page, and so no help in telling pages apart. The
        third item is the page's own, and the only thing an anchor can match
        to one page rather than all of them.
        """
        page = model.Page(width=612.0, height=792.0)
        for y in (10.0, 80.0):
            page.items.append(model.Rectangle(x=10.0, y=y, width=50.0, height=50.0))
        page.items.append(model.Rectangle(x=x, y=300.0, width=20.0, height=20.0))
        return page

    def anchor_for(self, shape_seq: int, page: model.Page) -> pubfile.ShapeAnchor:
        """An anchor sitting exactly where that page's own item was drawn."""
        item = page.items[-1]
        return pubfile.ShapeAnchor(
            shape_seq=shape_seq,
            centre_x=item.x + item.width / 2.0 - page.width / 2.0,
            centre_y=item.y + item.height / 2.0 - page.height / 2.0,
        )

    def structure(self, document, chunks, masters) -> pubfile.FileStructure:
        """A file stating these page chunks, in this order.

        `chunks` is (chunk seq, the master it applies, the index of the page
        whose own shape it lists) -- and None for that last one where the
        chunk lists nothing libmspub drew, which is a chunk the mapping
        cannot settle. `masters` is master seq -> how many shapes it holds.
        """
        s = pubfile.FileStructure(
            masters={
                seq: pubfile.PageStructure(seq=seq, is_master=True, shape_count=count)
                for seq, count in masters.items()
            }
        )
        for offset, (seq, applied, page_index) in enumerate(chunks):
            chunk = pubfile.PageStructure(seq=seq, applied_master=applied)
            if page_index is not None:
                shape_seq = 500 + offset
                chunk.shape_seqnums.append(shape_seq)
                s.shape_pages[shape_seq] = seq
                s.anchors.append(
                    self.anchor_for(shape_seq, document.pages[page_index])
                )
            s.pages.append(chunk)
        return s

    def document(self, *pages) -> model.Document:
        return model.Document(pages=list(pages))

    def test_a_page_takes_the_master_its_own_chunk_applies(self):
        # The kerkbode case in miniature: chunk 300 is libmspub's page 1 and
        # chunk 301 its page 0, so indexing the chunk list hands each page
        # the other one's master.
        document = self.document(self.page_at(100.0), self.page_at(400.0))
        structure = self.structure(
            document, [(300, 263, 1), (301, 294, 0)], {263: 1, 294: 2}
        )
        attributed = convert._attribute_masters(document, structure)
        self.assertEqual([key[0] for _items, key in attributed], [294, 263])
        self.assertEqual([len(items) for items, _key in attributed], [2, 1])

    def test_the_one_chunk_left_over_settles_the_one_page_left_over(self):
        # Chunk 300's shape puts it on page 1, which leaves page 0 and chunk
        # 301 as the only pair either could be -- so page 0 applies 294 even
        # though nothing of 301 was ever drawn. `1336 kerkbode.pub` has
        # exactly one such page.
        document = self.document(self.page_at(100.0), self.page_at(400.0))
        structure = self.structure(
            document, [(300, 263, 1), (301, 294, None)], {263: 1, 294: 2}
        )
        attributed = convert._attribute_masters(document, structure)
        self.assertEqual([key[0] for _items, key in attributed], [294, 263])

    def test_a_page_no_chunk_identifies_gets_no_master(self):
        # Two chunks left over and two pages to put them on, and the masters
        # they apply do not even agree on how many shapes they hold -- so
        # there is no telling how much of either page came from a master.
        document = self.document(
            self.page_at(100.0), self.page_at(400.0), self.page_at(500.0)
        )
        structure = self.structure(
            document,
            [(300, 263, 0), (301, 294, None), (302, 265, None)],
            {263: 1, 294: 2, 265: 1},
        )
        attributed = convert._attribute_masters(document, structure)
        self.assertEqual(attributed[0][1][0], 263)
        self.assertEqual(attributed[1], ([], None))
        self.assertEqual(attributed[2], ([], None))

    def test_a_master_every_remaining_chunk_applies_needs_no_mapping(self):
        # A document with one master cannot be got wrong by mis-ordering:
        # whichever chunk a page turns out to be, the master is the same.
        document = self.document(self.page_at(100.0), self.page_at(400.0))
        structure = self.structure(
            document, [(300, 263, None), (301, 263, None)], {263: 1}
        )
        attributed = convert._attribute_masters(document, structure)
        self.assertEqual([key[0] for _items, key in attributed], [263, 263])
        self.assertEqual([len(items) for items, _key in attributed], [1, 1])

    def test_an_unsettled_page_keeps_its_items_when_only_the_master_is_in_doubt(self):
        # MISSAL's shape: two masters holding one shape each, and pages whose
        # every shape sits where every other page's does. How much of the
        # page came from a master is settled -- both say one item -- but
        # which master it was is not, so the items stay where they are.
        document = self.document(self.page_at(100.0), self.page_at(400.0))
        structure = self.structure(
            document, [(300, 263, None), (301, 294, None)], {263: 1, 294: 1}
        )
        attributed = convert._attribute_masters(document, structure)
        self.assertEqual([len(items) for items, _key in attributed], [1, 1])
        self.assertEqual([key for _items, key in attributed], [None, None])

    def test_items_whose_master_is_in_doubt_are_numbered_but_not_lifted(self):
        # A page number can still be resolved without knowing the master --
        # it comes from the page's own index. Lifting cannot: two shapes
        # this alike may still belong to two different masters.
        document = model.Document()
        for x in (100.0, 400.0):
            page = model.Page(width=612.0, height=792.0)
            page.items.append(frame_saying(" #"))
            page.items.append(model.Rectangle(x=10.0, y=730.0, width=400.0, height=2.0))
            page.items.append(model.Rectangle(x=x, y=300.0, width=20.0, height=20.0))
            document.pages.append(page)
        structure = self.structure(
            document, [(300, 263, None), (301, 294, None)], {263: 2, 294: 2}
        )
        structure.has_fields = True
        convert._apply_master_pages(document, structure)
        numbered = [
            p.items[0].story.paragraphs[0].spans[0].text for p in document.pages
        ]
        self.assertEqual(numbered, [" 1", " 2"])
        self.assertEqual(document.masters, [])
        for page in document.pages:
            self.assertEqual(len(page.items), 3)
            self.assertIsNone(page.master)


class UnreportedWordArtTest(unittest.TestCase):
    """Placing a headline libmspub reported no shape for at all.

    The file has the words, the band, the rotation and the page. What the
    event stream has to supply is two confirmations: another shape of the
    same page chunk, which says which page this is, and nothing standing in
    the band, which says the headline is not already there.
    """

    BAND = dict(centre_x=-100.0, centre_y=-200.0, width=64.0, height=63.0)

    def art(self, **kwargs):
        defaults = dict(
            text="I venerdì\r\n2006\r\ndi Avvento", font="Comic Sans MS",
            size=15.9, shape_seq=311, warp="button curve", **self.BAND,
        )
        defaults.update(kwargs)
        return pubfile.WordArt(**defaults)

    def sibling(self) -> model.Rectangle:
        """A shape libmspub did report, elsewhere on the same page."""
        return model.Rectangle(x=100.0, y=100.0, width=50.0, height=20.0)

    def structure(self, art, **kwargs) -> pubfile.FileStructure:
        # The sibling sits at (125, 110) on a 612 x 792 page, which is
        # (-181, -286) from its centre -- where the file's anchor puts it.
        defaults = dict(
            wordart=[art],
            anchors=[pubfile.ShapeAnchor(shape_seq=313, centre_x=-181.0,
                                         centre_y=-286.0)],
            shape_pages={311: 266, 313: 266},
        )
        defaults.update(kwargs)
        return pubfile.FileStructure(**defaults)

    def document(self, *items) -> model.Document:
        return model.Document(pages=[page_with(self.sibling(), *items)])

    def band_of(self, art, page) -> tuple:
        return (
            page.width / 2.0 + art.centre_x - art.width / 2.0,
            page.height / 2.0 + art.centre_y - art.height / 2.0,
        )

    def test_the_headline_is_placed_on_the_page_the_file_puts_it_on(self):
        art = self.art()
        document = self.document()
        convert._recover_wordart(document, self.structure(art))
        frames = [
            i for i in document.pages[0].items if isinstance(i, model.TextFrame)
        ]
        self.assertEqual(len(frames), 1)
        x, y = self.band_of(art, document.pages[0])
        self.assertAlmostEqual(frames[0].x, x)
        self.assertAlmostEqual(frames[0].y, y)
        self.assertAlmostEqual(frames[0].width, 64.0)
        self.assertAlmostEqual(frames[0].height, 63.0)

    def test_the_words_the_font_and_the_size_come_with_it(self):
        document = self.document()
        convert._recover_wordart(document, self.structure(self.art()))
        frame = next(
            i for i in document.pages[0].items if isinstance(i, model.TextFrame)
        )
        self.assertEqual(
            ["".join(s.text for s in p.spans) for p in frame.story.paragraphs],
            ["I venerdì", "2006", "di Avvento"],
        )
        span = frame.story.paragraphs[0].spans[0]
        self.assertEqual(span.font, "Comic Sans MS")
        self.assertAlmostEqual(span.size_pt, 15.9)

    def test_with_no_shape_reported_there_is_no_paint_to_take(self):
        # libmspub reported nothing, so there is no fill, ramp or shadow to
        # read off it, and the words are left to the reader's own black
        # rather than given a colour the file never stated here.
        document = self.document()
        convert._recover_wordart(document, self.structure(self.art()))
        frame = next(
            i for i in document.pages[0].items if isinstance(i, model.TextFrame)
        )
        span = frame.story.paragraphs[0].spans[0]
        self.assertIsNone(span.color)
        self.assertIsNone(span.gradient)
        self.assertIsNone(span.stroke)
        self.assertIsNone(frame.style.shadow)

    def test_placing_it_is_reported_as_resting_on_the_file(self):
        document = self.document()
        convert._recover_wordart(document, self.structure(self.art()))
        warning = next(w for w in document.warnings if "placed from the file" in w)
        self.assertIn("I venerdì 2006 di Avvento", warning)
        self.assertIn("(button curve)", warning)

    def test_a_shape_whose_page_cannot_be_settled_is_not_placed(self):
        # Nothing else of its page chunk reached the event stream, so there
        # is no telling which page libmspub turned that chunk into.
        document = self.document()
        convert._recover_wordart(
            document, self.structure(self.art(), anchors=[], shape_pages={311: 266})
        )
        self.assertFalse(
            any(isinstance(i, model.TextFrame) for i in document.pages[0].items)
        )
        warning = next(w for w in document.warnings if "not placed" in w)
        self.assertIn("no telling which page", warning)

    def test_a_band_with_something_already_drawn_in_it_is_left_alone(self):
        # The case this guard exists for: a WordArt whose words also came
        # through as an ordinary frame would otherwise be written twice.
        art = self.art()
        document = self.document()
        x, y = self.band_of(art, document.pages[0])
        document.pages[0].items.append(
            model.TextFrame(x=x + 10.0, y=y + 10.0, width=40.0, height=20.0)
        )
        convert._recover_wordart(document, self.structure(art))
        placed = [
            i for i in document.pages[0].items if isinstance(i, model.TextFrame)
        ]
        self.assertEqual(len(placed), 1)      # the one that was already there
        warning = next(w for w in document.warnings if "not placed" in w)
        self.assertIn("already drawn across the band", warning)

    def test_a_page_background_is_not_something_in_the_way(self):
        # It covers every band on the page, so counting it as an
        # obstruction would turn this off for any page that has one.
        art = self.art()
        document = self.document(
            model.Rectangle(x=0.0, y=0.0, width=612.0, height=792.0)
        )
        convert._recover_wordart(document, self.structure(art))
        self.assertTrue(
            any(isinstance(i, model.TextFrame) for i in document.pages[0].items)
        )


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

    def test_the_headline_libmspub_reports_nothing_for_reaches_its_page(self):
        # backlog §10, end to end: 'I venerdì 2006 di Avvento' has no shape
        # in the event stream at all. Its page comes from the file, checked
        # against the sibling shapes libmspub *does* report on that page --
        # page chunk 266, which for this file is page 0.
        source = SAMPLES / "Cantico_dei_Cantici.pub"
        document = convert.parse_document(source)
        structure = pubfile.read_structure(source)
        convert._recover_wordart(document, structure)
        words = {
            " ".join(
                " ".join(s.text for p in item.story.paragraphs for s in p.spans).split()
            ): index
            for index, page in enumerate(document.pages)
            for item in model._walk(page.items)
            if isinstance(item, model.TextFrame)
        }
        placed = {text: index for text, index in words.items() if "Avvento" in text}
        self.assertEqual(placed, {"I venerdì 2006 di Avvento": 0})
        self.assertTrue(
            any("placed from the file alone" in w for w in document.warnings)
        )
        self.assertFalse(any("not placed" in w for w in document.warnings))

    def test_a_newsletter_page_takes_its_own_master_not_its_neighbours(self):
        # backlog §14: this file's chunk list is a permutation of libmspub's
        # page order, so indexing it hands 16 of the 28 pages the other
        # master of the pair. Every chunk here settles, so the measured
        # answer is complete and simply disagrees with the indexed one.
        source = SAMPLES / "cgk" / "1338 kerkbode.pub"
        if not source.exists():
            self.skipTest("newsletter sample absent")
        document = convert.parse_document(source)
        structure = pubfile.read_structure(source)
        attributed = convert._attribute_masters(document, structure)
        taken = [None if key is None else key[0] for _items, key in attributed]
        indexed = [page.applied_master for page in structure.pages]
        self.assertEqual(set(taken), {263, 294})
        self.assertEqual(sum(1 for a, b in zip(taken, indexed) if a != b), 16)

    def test_the_missal_masters_survive_pages_the_mapping_cannot_settle(self):
        # Ten of its fifteen pages hold one full-page frame apiece, all at
        # the same place, so no shape tells those pages apart. Both masters
        # hold one shape, which is enough to say how much of each page came
        # from a master even where it cannot say which master it was.
        source = SAMPLES / "MISSAL MARIANA E PEDRO.pub"
        document = convert.parse_document(source)
        structure = pubfile.read_structure(source)
        attributed = convert._attribute_masters(document, structure)
        self.assertEqual([len(items) for items, _key in attributed], [1] * 15)

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


class GradientReadingTest(unittest.TestCase):
    """The ramp libmspub drops the ends of."""

    def palette(self):
        return [(0, 0, 0), (255, 0, 0), (255, 204, 0)]

    def test_a_reference_reads_as_bgr_unless_it_is_flagged(self):
        self.assertEqual(pubfile._resolve_color(0x00563412, 0, []), (0x12, 0x34, 0x56))

    def test_a_flagged_reference_indexes_the_palette(self):
        self.assertEqual(
            pubfile._resolve_color(0x08000002, 0, self.palette()), (255, 204, 0)
        )

    def test_an_index_past_the_palette_is_not_a_colour(self):
        self.assertIsNone(pubfile._resolve_color(0x08000009, 0, self.palette()))

    def test_an_intensity_change_against_itself_terminates(self):
        # The base of an intensity change is read directly rather than
        # resolved in its turn; resolving it recursed until the stack ran
        # out, which cost the file its masters and its WordArt with it.
        self.assertIsNotNone(pubfile._resolve_color(0x10800100, 0x10800100, []))

    def test_an_intensity_change_darkens_the_colour_underneath(self):
        # Half intensity against a black base halves every channel.
        found = pubfile._resolve_color(0x10800100, 0x00FFFFFF, [])
        self.assertEqual(found, (128, 128, 128))

    def test_the_shade_list_reads_as_position_and_colour(self):
        blob = (
            struct.pack("<HI", 1, 0)  # count, then four bytes of header
            + struct.pack("<II", 0x00563412, 32768)  # colour, 0.5 in 16.16
        )
        self.assertEqual(
            pubfile._shade_stops(blob, [], 0), [(0.5, (0x12, 0x34, 0x56))]
        )

    def test_a_shade_list_shorter_than_it_claims_stops_at_the_data(self):
        blob = struct.pack("<HI", 9, 0) + struct.pack("<II", 0x00112233, 0)
        self.assertEqual(len(pubfile._shade_stops(blob, [], 0)), 1)

    def test_nothing_to_read_is_no_stops(self):
        self.assertEqual(pubfile._shade_stops(b"", [], 0), [])
        self.assertEqual(pubfile._shade_stops(None, [], 0), [])


@needs_samples
class RealGradientTest(unittest.TestCase):
    def test_the_whole_ramp_is_read_out_of_a_real_file(self):
        # libmspub reports this one as a single grey stop. The file states
        # brown, white and a grey waypoint, at a focus of 100 -- so the
        # ramp runs white to brown, through the grey.
        structure = pubfile.read_structure(SAMPLES / "cgk" / "1336 kerkbode.pub")
        if structure is None:
            self.skipTest("newsletter sample absent")
        ramps = {
            tuple(colour for _position, colour in found.stops)
            for found in structure.gradients
        }
        self.assertIn(((255, 255, 255), (0xE1, 0xE1, 0xE1), (0x66, 0x33, 0x00)), ramps)

    def test_a_reconstructed_ramp_holds_the_waypoints_libmspub_reports(self):
        """The reversal rule, checked against libmspub's own reading.

        Where libmspub reports a ramp in full it applies the same focus
        rule this reconstruction does, so on those shapes the two must
        agree waypoint for waypoint -- and where they do, the rule is
        right on the shapes it reports only one stop of as well.
        """
        if not convert.PUBDUMP.exists():
            self.skipTest("pubdump not built")
        source = SAMPLES / "cgk" / "1336 kerkbode.pub"
        if not source.exists():
            self.skipTest("newsletter sample absent")
        structure = pubfile.read_structure(source)
        document = convert.parse_document(source)
        checked = 0
        for page in document.pages:
            for item in model._walk(page.items):
                ramp = item.style.gradient
                if ramp is None or len(ramp.stops) < 2:
                    continue
                found = structure.gradient_for(
                    item.x + item.width / 2 - page.width / 2,
                    item.y + item.height / 2 - page.height / 2,
                    item.width,
                    item.height,
                )
                if found is None:
                    continue
                theirs = [stop.color for stop in ramp.stops]
                ours = [colour for _position, colour in found.stops]
                # Ours adds the ends libmspub drops -- and only where the
                # waypoints do not already reach them -- so what it reports
                # has to sit inside ours, in order, unbroken.
                inside = any(
                    ours[at:at + len(theirs)] == theirs
                    for at in range(len(ours) - len(theirs) + 1)
                )
                self.assertTrue(inside, f"{ours} does not contain {theirs}")
                checked += 1
        self.assertGreater(checked, 10, "no ramps were compared")


class GradientMatchingTest(unittest.TestCase):
    """Telling one shape's ramp from another's where they overlap."""

    def structure(self, *shapes) -> pubfile.FileStructure:
        return pubfile.FileStructure(gradients=[
            pubfile.ShapeGradient(
                stops=[(0.0, (r, 0, 0)), (1.0, (255, 255, 255))],
                centre_x=cx, centre_y=cy, width=w, height=h,
            )
            for r, cx, cy, w, h in shapes
        ])

    def test_a_shape_on_its_own_is_matched_by_where_it_sits(self):
        found = self.structure((1, 10.0, 20.0, 100.0, 50.0)).gradient_for(
            10.0, 20.0, 100.0, 50.0
        )
        self.assertEqual(found.stops[0][1], (1, 0, 0))

    def test_a_size_the_outline_widened_still_matches(self):
        # The anchor box measures the outline; libmspub reports the path.
        found = self.structure((1, 10.0, 20.0, 199.0, 83.0)).gradient_for(
            10.0, 20.0, 182.5, 66.4
        )
        self.assertIsNotNone(found)

    def test_two_ramps_at_one_centre_are_told_apart_by_size(self):
        structure = self.structure(
            (1, 10.0, 20.0, 100.0, 50.0), (2, 10.2, 20.1, 340.0, 35.0)
        )
        self.assertEqual(
            structure.gradient_for(10.0, 20.0, 340.0, 35.0).stops[0][1], (2, 0, 0)
        )

    def test_two_ramps_of_one_size_at_one_centre_are_an_ambiguity(self):
        structure = self.structure(
            (1, 10.0, 20.0, 100.0, 50.0), (2, 10.2, 20.1, 100.0, 50.0)
        )
        self.assertIsNone(structure.gradient_for(10.0, 20.0, 100.0, 50.0))

    def test_a_shape_nowhere_near_is_not_matched(self):
        structure = self.structure((1, 10.0, 20.0, 100.0, 50.0))
        self.assertIsNone(structure.gradient_for(300.0, 20.0, 100.0, 50.0))


class GradientRestorationTest(unittest.TestCase):
    """Only a ramp that arrived flattened is replaced."""

    def document(self, style: model.GraphicStyle) -> model.Document:
        shape = model.Rectangle(x=50.0, y=100.0, width=100.0, height=50.0, style=style)
        return model.Document(pages=[page_with(shape)]), shape

    def structure(self):
        # page_with makes a 612x792 page, so this centre is the shape's.
        return pubfile.FileStructure(gradients=[
            pubfile.ShapeGradient(
                stops=[(0.0, (102, 51, 0)), (0.48, (225, 225, 225)), (1.0, (255, 255, 255))],
                centre_x=100.0 - 306.0, centre_y=125.0 - 396.0,
                width=100.0, height=50.0,
            )
        ])

    def test_a_flattened_ramp_is_replaced_with_the_whole_one(self):
        document, shape = self.document(
            model.GraphicStyle(fill=(225, 225, 225), approximated_fill=True)
        )
        convert._restore_gradient_ramps(document, self.structure())
        self.assertEqual(
            [stop.color for stop in shape.style.gradient.stops],
            [(102, 51, 0), (225, 225, 225), (255, 255, 255)],
        )
        self.assertFalse(shape.style.approximated_fill)

    def test_the_flat_fill_becomes_the_ramps_first_colour(self):
        document, shape = self.document(
            model.GraphicStyle(fill=(225, 225, 225), approximated_fill=True)
        )
        convert._restore_gradient_ramps(document, self.structure())
        self.assertEqual(shape.style.fill, (102, 51, 0))

    def test_a_turned_shape_turns_its_ramp_with_it(self):
        # Publisher turns a shape and its shade together, and libmspub
        # reports the two apart: the rotation goes on the shape, and for a
        # polygon it goes into the order of the points, where a ramp cannot
        # see it. Every section-heading band in the corpus is stated this
        # way -- the panel's ramp, upside down -- and left flat side up it
        # runs brown at the top into white at the foot instead of the other
        # way about.
        document, shape = self.document(
            model.GraphicStyle(fill=(225, 225, 225), approximated_fill=True)
        )
        structure = self.structure()
        structure.gradients[0].rotation = 180.0
        convert._restore_gradient_ramps(document, structure)
        self.assertAlmostEqual(shape.style.gradient.angle, 180.0)

    def test_a_turn_the_item_already_carries_is_not_counted_twice(self):
        # Where libmspub *does* report the rotation, the reader turns the
        # object and its ramp with it, so adding the file's rotation on top
        # would turn the ramp twice.
        document, shape = self.document(
            model.GraphicStyle(fill=(225, 225, 225), approximated_fill=True)
        )
        shape.rotation = 180.0
        structure = self.structure()
        structure.gradients[0].rotation = 180.0
        convert._restore_gradient_ramps(document, structure)
        self.assertAlmostEqual(shape.style.gradient.angle, 0.0)

    def test_a_ramp_that_arrived_whole_still_gains_its_ends(self):
        # libmspub drops the two end colours from every ramp with a
        # waypoint list, not only from the ones that collapse to a stop,
        # so a ramp that survived is missing them just the same.
        whole = model.Gradient(stops=(
            model.GradientStop(location=32.0, color=(1, 2, 3)),
            model.GradientStop(location=49.0, color=(4, 5, 6)),
        ))
        document, shape = self.document(model.GraphicStyle(gradient=whole))
        convert._restore_gradient_ramps(document, self.structure())
        self.assertEqual(
            [stop.color for stop in shape.style.gradient.stops],
            [(102, 51, 0), (225, 225, 225), (255, 255, 255)],
        )

    def test_a_ramp_the_file_states_no_waypoints_for_is_left_alone(self):
        # With no waypoint list libmspub builds the ramp from the two end
        # colours itself, and gets it right; `pubfile` reports nothing for
        # those, so there is nothing here to replace them with.
        whole = model.Gradient(stops=(
            model.GradientStop(location=0.0, color=(1, 2, 3)),
            model.GradientStop(location=100.0, color=(4, 5, 6)),
        ))
        document, shape = self.document(model.GraphicStyle(gradient=whole))
        convert._restore_gradient_ramps(document, pubfile.FileStructure())
        self.assertIs(shape.style.gradient, whole)

    def test_a_shape_the_file_says_nothing_about_keeps_its_flat_fill(self):
        document, shape = self.document(
            model.GraphicStyle(fill=(1, 1, 1), approximated_fill=True)
        )
        convert._restore_gradient_ramps(document, pubfile.FileStructure())
        self.assertIsNone(shape.style.gradient)
        self.assertTrue(shape.style.approximated_fill)

    def test_a_shape_inside_a_group_is_reached(self):
        shape = model.Rectangle(
            x=50.0, y=100.0, width=100.0, height=50.0,
            style=model.GraphicStyle(fill=(225, 225, 225), approximated_fill=True),
        )
        group = model.Group(children=[shape])
        document = model.Document(pages=[page_with(group)])
        convert._restore_gradient_ramps(document, self.structure())
        self.assertIsNotNone(shape.style.gradient)

    def test_no_structure_at_all_changes_nothing(self):
        document, shape = self.document(
            model.GraphicStyle(fill=(1, 1, 1), approximated_fill=True)
        )
        convert._restore_gradient_ramps(document, None)
        self.assertIsNone(shape.style.gradient)


def turned_band(turn: float, width: float = 100.0, height: float = 20.0,
                centre=(300.0, 400.0)) -> list:
    """A rectangle's four corners, turned about its own centre.

    Clockwise, because the page measures y downwards -- the frame both
    libmspub's points and Publisher's stated turn are in.
    """
    radians = math.radians(turn)
    cos, sin = math.cos(radians), math.sin(radians)
    return [
        (centre[0] + x * cos - y * sin, centre[1] + x * sin + y * cos)
        for x, y in (
            (-width / 2, -height / 2), (width / 2, -height / 2),
            (width / 2, height / 2), (-width / 2, height / 2),
        )
    ]


def bearing(points) -> float:
    """The turn a band is drawn at, read off its first edge."""
    (x0, y0), (x1, y1) = points[0], points[1]
    return math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360.0


class FlooredTurnTest(unittest.TestCase):
    """A turn stated in fractions of a degree, drawn a whole degree out.

    libmspub keeps only the whole degrees of the turn a shape states, and
    it keeps them by flooring: a band stated at -540.042145 is drawn as
    though it were stated at -541, which is 1.042 degrees away and shows
    as 6.5pt of drop across an A5 page. Publisher's own PDF export of
    `1336 kerkbode.pub` draws that band at the fraction the file states,
    so the file is right and the shape has to be laid back on it.
    """

    #: The turn every section band in the kerkbode corpus states, and the
    #: 181 degrees libmspub draws it at.
    STATED = -35392202 / 65536.0
    DRAWN = 181.0

    def document(self, turn: float = DRAWN):
        shape = model.Polygon(points=turned_band(turn), closed=True)
        shape.x = min(p[0] for p in shape.points)
        shape.y = min(p[1] for p in shape.points)
        shape.width = max(p[0] for p in shape.points) - shape.x
        shape.height = max(p[1] for p in shape.points) - shape.y
        return model.Document(pages=[page_with(shape)]), shape

    def structure(self, rotation: float = STATED):
        # page_with makes a 612x792 page, so this centre is the band's.
        return pubfile.FileStructure(gradients=[
            pubfile.ShapeGradient(
                stops=[(0.0, (102, 51, 0)), (1.0, (255, 255, 255))],
                rotation=rotation,
                centre_x=300.0 - 306.0, centre_y=400.0 - 396.0,
                width=100.0, height=20.0,
            )
        ])

    def test_the_fraction_libmspub_floors_away_is_put_back(self):
        document, shape = self.document()
        convert._restore_floored_turns(document, self.structure())
        self.assertAlmostEqual(bearing(shape.points), 180.042145, places=4)

    def test_the_band_keeps_the_centre_and_the_size_it_was_drawn_at(self):
        document, shape = self.document()
        convert._restore_floored_turns(document, self.structure())
        centre_x = sum(p[0] for p in shape.points) / 4
        centre_y = sum(p[1] for p in shape.points) / 4
        self.assertAlmostEqual(centre_x, 300.0)
        self.assertAlmostEqual(centre_y, 400.0)
        edge = math.dist(shape.points[0], shape.points[1])
        self.assertAlmostEqual(edge, 100.0)

    def test_the_box_is_measured_again_around_the_points_it_moved(self):
        # The IDML writer places a polygon's points about the centre of
        # its box, so a box left describing the old corners moves the
        # whole shape off the page position libmspub gave it.
        document, shape = self.document()
        convert._restore_floored_turns(document, self.structure())
        # A 100 x 20 band laid almost flat barely leans out of its box;
        # the 21.7pt-tall box it arrived in was a degree of lean.
        self.assertAlmostEqual(shape.width, 100.0147, places=3)
        self.assertAlmostEqual(shape.height, 20.0736, places=3)
        self.assertAlmostEqual(shape.x, min(p[0] for p in shape.points))
        self.assertAlmostEqual(shape.y, min(p[1] for p in shape.points))

    def test_a_turn_stated_in_whole_degrees_is_left_alone(self):
        # Nothing was floored away, so there is nothing to put back --
        # and the corpus states far more of these than fractional ones.
        document, shape = self.document(turn=180.0)
        before = list(shape.points)
        convert._restore_floored_turns(document, self.structure(rotation=180.0))
        self.assertEqual(shape.points, before)

    def test_a_shape_the_file_says_nothing_about_is_left_alone(self):
        document, shape = self.document()
        before = list(shape.points)
        convert._restore_floored_turns(document, pubfile.FileStructure())
        self.assertEqual(shape.points, before)

    def test_no_structure_at_all_changes_nothing(self):
        document, shape = self.document()
        before = list(shape.points)
        convert._restore_floored_turns(document, None)
        self.assertEqual(shape.points, before)


class RampDirectionTest(unittest.TestCase):
    """Which end a ramp starts from, and which way its waypoints run.

    Publisher's focus says where the ramp begins. At 100 it begins at the
    fill-back colour, and libmspub reverses the waypoints with it --
    `addColorReverse`, at the distance from the other end. Every shape in
    the corpus that states a waypoint list states a focus of 100, and on
    the 32 ramps libmspub reports in full the two readings agree stop for
    stop, which is what makes the rule safe to apply to the ones it
    reports a single stop of.
    """

    BROWN, GREY, WHITE = (102, 51, 0), (225, 225, 225), (255, 255, 255)

    def test_a_forward_ramp_runs_from_the_fill_colour(self):
        stops = pubfile._ramp_stops(0, [(0.48, self.GREY)], self.BROWN, self.WHITE)
        self.assertEqual(
            stops, [(0.0, self.BROWN), (0.48, self.GREY), (1.0, self.WHITE)]
        )

    def test_a_focus_of_a_hundred_runs_the_other_way(self):
        stops = pubfile._ramp_stops(100, [(0.48, self.GREY)], self.BROWN, self.WHITE)
        self.assertEqual(
            stops, [(0.0, self.WHITE), (0.52, self.GREY), (1.0, self.BROWN)]
        )

    def test_reversing_keeps_the_waypoints_in_their_new_order(self):
        stops = pubfile._ramp_stops(
            100, [(0.52, (1, 1, 1)), (0.69, (2, 2, 2))], self.BROWN, self.WHITE
        )
        self.assertEqual(
            [colour for _position, colour in stops],
            [self.WHITE, (2, 2, 2), (1, 1, 1), self.BROWN],
        )
        self.assertAlmostEqual(stops[1][0], 0.31)
        self.assertAlmostEqual(stops[2][0], 0.48)

    def test_a_list_that_already_reaches_an_end_states_that_end_itself(self):
        stops = pubfile._ramp_stops(
            0, [(0.0, (1, 1, 1)), (1.0, (2, 2, 2))], self.BROWN, self.WHITE
        )
        self.assertEqual(stops, [(0.0, (1, 1, 1)), (1.0, (2, 2, 2))])


class GradientAngleTest(unittest.TestCase):
    """Three transformations sit between the file and `draw:angle`."""

    def angle(self, raw):
        return pubfile._gradient_angle({pubfile._PROP_FILL_ANGLE: raw})

    def test_the_angle_is_the_high_half_of_a_fixed_point_value(self):
        self.assertEqual(self.angle(0x00B40000), -180.0)  # 180 in the file

    def test_it_is_negated_because_odf_measures_clockwise(self):
        self.assertEqual(self.angle(0x00870000), -135.0)  # 135 in the file

    def test_the_two_angles_the_format_states_askew_are_corrected(self):
        # -45 in the file is 225 to libmspub, which reports it as -225 --
        # the value the corpus carries and the model already folds.
        self.assertEqual(self.angle(0xFFD30000), -225.0)

    def test_a_shape_stating_no_angle_has_none(self):
        self.assertEqual(pubfile._gradient_angle({}), 0.0)


@needs_samples
class RealGradientRotationTest(unittest.TestCase):
    """The heading bands are the panel's ramp, turned upside down."""

    def test_the_bands_of_a_real_file_state_their_half_turn(self):
        structure = pubfile.read_structure(SAMPLES / "cgk" / "1336 kerkbode.pub")
        if structure is None:
            self.skipTest("newsletter sample absent")
        # The band behind the *Meditatie* headline on page 3: white at the
        # top of it down to brown at the foot, which is the ramp below
        # turned over. Publisher states the turn on the shape, and multiple
        # turns are stated as they were made -- one band states -540.
        band = next(
            found for found in structure.gradients
            if abs(found.width - 346.3) < 1 and abs(found.height - 32.0) < 1
        )
        self.assertAlmostEqual(band.rotation % 360.0, 180.0, places=3)
        # The panel underneath is the same ramp the other way up, and
        # states no turn at all.
        panel = next(
            found for found in structure.gradients
            if abs(found.width - 346.3) < 1 and abs(found.height - 382.4) < 1
        )
        self.assertEqual(panel.rotation, 0.0)


if __name__ == "__main__":
    unittest.main()


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


def guide(position_emu: int, *, margin: bool = True) -> bytes:
    """One guide entry: its position, and whether it is a margin guide."""
    fields = [_u32(0x01, position_emu)]
    if margin:
        # A zero-length block: presence is the whole of what it says.
        fields.append(_block(0x04, 0x08))
    return _container(0x00, 0x88, fields)


def guides_chunk(*guides: bytes) -> bytes:
    return _chunk([_container(0x02, 0xA0, list(guides))])


CM = 360000  # EMU per centimetre, which is how the samples were set


class GuideReadingTest(unittest.TestCase):
    """The layout guides, which libmspub has no concept of at all."""

    def read(self, *chunks):
        contents, refs = b"\x00" * 8, []
        for seq, (kind, payload) in enumerate(chunks):
            refs.append((seq, kind, len(contents)))
            contents += payload
        return pubfile._read_guides(contents, refs)

    def margins_a(self, *extra: bytes):
        """The controlled sample: 1, 1.5, 2 and 2.5cm on A4."""
        return self.read((0x4C, guides_chunk(
            guide(1 * CM), *extra, guide(19 * CM),
            guide(int(1.5 * CM)), guide(int(27.2 * CM)),
        )))

    def test_the_four_margin_guides_are_read_in_order(self):
        found = self.margins_a()
        self.assertIsNotNone(found)
        # Left and right first, then top and bottom: the file writes every
        # vertical guide before any horizontal one.
        self.assertAlmostEqual(found.left / 28.3465, 1.0, places=3)
        self.assertAlmostEqual(found.right / 28.3465, 19.0, places=3)
        self.assertAlmostEqual(found.top / 28.3465, 1.5, places=3)
        self.assertAlmostEqual(found.bottom / 28.3465, 27.2, places=3)

    def test_an_unflagged_guide_between_the_first_pair_is_a_column(self):
        found = self.margins_a(guide(10 * CM, margin=False))
        self.assertIsNotNone(found)
        self.assertEqual(len(found.columns), 1)
        self.assertAlmostEqual(found.columns[0] / 28.3465, 10.0, places=3)
        self.assertEqual(found.rows, ())

    def test_a_file_with_no_guide_chunk_reads_nothing(self):
        self.assertIsNone(self.read((0x44, _chunk([_u32(0x01, 1)]))))

    def test_fewer_than_four_margin_guides_is_not_this_shape(self):
        self.assertIsNone(self.read((0x4C, guides_chunk(
            guide(1 * CM), guide(19 * CM), guide(int(1.5 * CM)),
        ))))

    def test_a_guide_outside_the_margins_is_not_this_shape(self):
        # The margins bound the list at both ends. A file that puts a
        # ruler guide before the left margin is one this reading does not
        # describe, and reading it anyway would misplace all four.
        self.assertIsNone(self.read((0x4C, guides_chunk(
            guide(int(0.5 * CM), margin=False),
            guide(1 * CM), guide(19 * CM),
            guide(int(1.5 * CM)), guide(int(27.2 * CM)),
        ))))

    def test_margins_that_cross_are_refused(self):
        self.assertIsNone(self.read((0x4C, guides_chunk(
            guide(19 * CM), guide(1 * CM),
            guide(int(1.5 * CM)), guide(int(27.2 * CM)),
        ))))

    def test_a_truncated_chunk_does_not_raise(self):
        self.assertIsNone(self.read((0x4C, b"\x40\x00\x00\x00\x02\xa0")))


class GuideMarginTest(unittest.TestCase):
    """Turning guide positions into the insets a reader wants."""

    def guides(self, **kwargs):
        base = dict(left=36.0, right=576.0, top=72.0, bottom=720.0)
        base.update(kwargs)
        return pubfile.PageGuides(**base)

    def test_the_right_and_bottom_are_measured_back_from_the_page(self):
        self.assertEqual(self.guides().margins(612.0, 792.0), (36.0, 72.0, 36.0, 72.0))

    def test_a_guide_off_the_page_states_no_margin_at_all(self):
        # Page and guides from different documents. A margin that cannot
        # be true is worse than none, because the reader would draw it.
        self.assertIsNone(self.guides().margins(300.0, 792.0))

    def test_a_guide_on_the_page_edge_survives_the_rounding(self):
        # Page size arrives through libmspub's four-decimal inches while a
        # guide is read in EMU, so the two disagree by a fraction of a point.
        found = self.guides(right=612.02).margins(612.0, 792.0)
        self.assertIsNotNone(found)
        self.assertEqual(found[2], 0.0)


@needs_samples
class RealGuideTest(unittest.TestCase):
    """The five tracked samples, whose guides were never read before."""

    def test_every_sample_states_symmetric_margins(self):
        expected = {
            "Blank Note Card (100_1502 Snail) (2 up).pub": 18.0,
            "Bus Meeting Zones & Luggage JLW.pub": 36.0,
            "Cantico_dei_Cantici.pub": 29.48,
            "MISSAL MARIANA E PEDRO.pub": 36.0,
            "rotated_text.pub": 70.87,
        }
        for name, left in expected.items():
            with self.subTest(name=name):
                structure = pubfile.read_structure(SAMPLES / name)
                self.assertIsNotNone(structure)
                self.assertIsNotNone(structure.guides)
                self.assertAlmostEqual(structure.guides.left, left, places=1)
                # No sample states a column guide; the newsletters do.
                self.assertEqual(structure.guides.columns, ())


@needs_newsletter
class NewsletterGuideTest(unittest.TestCase):
    """The two-column A5 newsletter, the only corpus file with a column."""

    def test_the_column_guide_is_read_beside_the_margins(self):
        structure = pubfile.read_structure(NEWSLETTER)
        self.assertIsNotNone(structure)
        guides = structure.guides
        self.assertIsNotNone(guides)
        left, top, right, bottom = guides.margins(421.02, 594.9576)
        for measured, centimetres in (
            (left, 1.4), (top, 1.5), (right, 1.6), (bottom, 1.7)
        ):
            self.assertAlmostEqual(measured / 28.3465, centimetres, places=1)
        self.assertEqual(len(guides.columns), 1)


class CellAlignmentReadingTest(unittest.TestCase):
    """Field 0x07 of a cell record, read back in Publisher as alignment."""

    def one_cell(self, fields):
        contents, refs = b"\x00" * 8, []
        for seq, (kind, payload) in enumerate((
            (0x10, table_chunk((72.0,), (18.0,), cells_seqnum=1)),
            (0x63, cells_chunk((0, 0, fields))),
        )):
            refs.append((seq, kind, len(contents)))
            contents += payload
        tables = pubfile._read_tables(contents, refs)
        return tables[pubfile.table_signature([72.0], [18.0])]

    def test_one_means_centre_and_two_means_bottom(self):
        self.assertEqual(self.one_cell({0x07: 1}).alignments[(0, 0)], "center")
        self.assertEqual(self.one_cell({0x07: 2}).alignments[(0, 0)], "bottom")

    def test_a_cell_leaving_the_field_out_is_top_aligned(self):
        # Publisher writes nothing for top, the way it writes nothing for
        # an inset of zero -- so absent is a statement, not a gap.
        self.assertEqual(self.one_cell({0x0A: 12700}).alignments[(0, 0)], "top")

    def test_a_value_we_do_not_recognise_is_left_unstated(self):
        self.assertEqual(self.one_cell({0x07: 9}).alignments, {})


@needs_newsletter
class RealCellAlignmentTest(unittest.TestCase):
    def test_every_cell_of_a_real_table_states_an_alignment(self):
        structure = pubfile.read_structure(NEWSLETTER)
        self.assertIsNotNone(structure)
        read = [table for table in structure.tables.values() if table is not None]
        self.assertTrue(read)
        for table in read:
            self.assertEqual(set(table.alignments), set(table.insets))
            self.assertLessEqual(
                set(table.alignments.values()), {"top", "center", "bottom"}
            )


class CellAlignmentApplicationTest(unittest.TestCase):
    """Getting the alignment onto the cells libmspub reported."""

    def apply(self, alignments, insets=None):
        table = model.Table(
            x=0.0, y=0.0, width=72.0, height=18.0,
            column_widths=[72.0], row_heights=[18.0],
        )
        table.cells = [model.TableCell(row=0, column=0)]
        document = model.Document(pages=[page_with(table)])
        signature = pubfile.table_signature([72.0], [18.0])
        structure = pubfile.FileStructure(
            tables={signature: pubfile.TableStructure(
                insets=insets if insets is not None else {(0, 0): (1.0, 1.0, 1.0, 1.0)},
                alignments=alignments,
            )}
        )
        convert._apply_cell_insets(document, structure)
        return table.cells[0]

    def test_a_matched_cell_gets_its_alignment(self):
        self.assertEqual(self.apply({(0, 0): "bottom"}).vertical_align, "bottom")

    def test_a_cell_the_file_states_no_alignment_for_states_none(self):
        self.assertIsNone(self.apply({}).vertical_align)

    def test_alignment_does_not_cost_the_insets(self):
        # The two are read from one record, and a file that only ever
        # stated padding must still be applied as it always was.
        cell = self.apply({})
        self.assertEqual(cell.insets, model.CellInsets(1.0, 1.0, 1.0, 1.0))
        self.assertTrue(cell.unruled)


class PageMarginApplicationTest(unittest.TestCase):
    """Resolving one set of document-wide guides against every page."""

    def document(self, *sizes):
        pages = [model.Page(width=w, height=h) for w, h in sizes]
        return model.Document(pages=pages)

    def structure(self, **kwargs):
        base = dict(left=36.0, right=576.0, top=72.0, bottom=720.0)
        base.update(kwargs)
        return pubfile.FileStructure(guides=pubfile.PageGuides(**base))

    def test_every_page_gets_the_documents_guides(self):
        document = self.document((612.0, 792.0), (612.0, 792.0))
        convert._apply_page_margins(document, self.structure())
        for page in document.pages:
            self.assertEqual(
                page.margins, model.PageMargins(36.0, 72.0, 36.0, 72.0)
            )

    def test_a_master_is_given_them_as_well(self):
        document = self.document((612.0, 792.0))
        document.masters.append(model.Master(width=612.0, height=792.0))
        convert._apply_page_margins(document, self.structure())
        self.assertIsNotNone(document.masters[0].margins)

    def test_columns_arrive_as_insets_from_the_left_edge(self):
        document = self.document((612.0, 792.0))
        convert._apply_page_margins(document, self.structure(columns=(306.0,)))
        self.assertEqual(document.pages[0].margins.columns, (270.0,))

    def test_a_page_the_guides_do_not_fit_keeps_the_readers_default(self):
        document = self.document((300.0, 792.0))
        convert._apply_page_margins(document, self.structure())
        self.assertIsNone(document.pages[0].margins)
        self.assertTrue(document.warnings)

    def test_a_file_stating_no_guides_changes_nothing(self):
        document = self.document((612.0, 792.0))
        convert._apply_page_margins(document, pubfile.FileStructure())
        self.assertIsNone(document.pages[0].margins)
        self.assertEqual(document.warnings, [])

    def test_no_structure_at_all_changes_nothing(self):
        document = self.document((612.0, 792.0))
        convert._apply_page_margins(document, None)
        self.assertIsNone(document.pages[0].margins)


def page_list_chunk(*seqnums: int) -> bytes:
    """A PAGE LIST chunk: every page chunk in the order Publisher pages it."""
    return _chunk([
        _u32(0x01, len(seqnums)),
        _container(0x02, 0xA0, [
            _block(0x00, 0x70, struct.pack("<I", seq)) for seq in seqnums
        ]),
    ])


def page_chunk(*, master: bool = False, applied: int = 263, shapes=()) -> bytes:
    """A PAGE chunk stating what the file says is on it."""
    children = [_block(0x0E, 0xC0, struct.pack("<I", 8) + b"M\x00")] if master else [
        _u32(0x0D, applied)
    ]
    children.append(
        _container(0x02, 0x90, [_block(0x70, 0x70, struct.pack("<I", s)) for s in shapes])
    )
    return _chunk(children)


class PageOrderReadingTest(unittest.TestCase):
    """The list that says which page of the document each chunk is.

    libmspub calls `startPage` only for a page carrying shapes of its own,
    so a page whose content comes only from its master never reaches the
    event stream. The loss is silent and usually not at the end, which
    shifts every page after it one place early -- fatal for a booklet,
    where it flips recto and verso for the rest of the document.
    """

    def contents(self, *chunks):
        buffer, refs = b"\x00" * 8, []
        for seq, (kind, payload) in enumerate(chunks):
            refs.append((seq, kind, len(buffer)))
            buffer += payload
        return buffer, refs

    def test_the_page_order_list_is_read_in_the_order_it_states(self):
        buffer, refs = self.contents(
            (pubfile._PAGE_LIST_CHUNK, page_list_chunk(500, 400, 300)),
        )
        self.assertEqual(pubfile._read_page_order(buffer, refs), [500, 400, 300])

    def test_a_file_stating_no_page_order_reads_as_none_stated(self):
        buffer, refs = self.contents((pubfile._PAGE_CHUNK, page_chunk(shapes=(1,))))
        self.assertEqual(pubfile._read_page_order(buffer, refs), [])

    def test_a_shapeless_page_between_two_others_is_found_at_its_position(self):
        # The chunks are deliberately out of page order: the list is the
        # only statement of which page is which, and indexing would put the
        # blank in the wrong place.
        chunks = {
            263: pubfile.PageStructure(seq=263, is_master=True, shape_count=1),
            310: pubfile.PageStructure(seq=310, applied_master=263, shape_count=4),
            320: pubfile.PageStructure(seq=320, applied_master=263, shape_count=0),
            330: pubfile.PageStructure(seq=330, applied_master=263, shape_count=7),
        }
        blanks = pubfile._blank_pages([263, 330, 320, 310], chunks)
        self.assertEqual([position for position, _chunk in blanks], [2])
        self.assertEqual([chunk.seq for _position, chunk in blanks], [320])

    def test_a_trailing_shapeless_run_is_publishers_scratch_band_not_a_page(self):
        # Publisher keeps unused page chunks at the tail of the list. A
        # blank page at the very end of the document cannot be told from
        # them, and inserting one wrongly would renumber the whole document.
        chunks = {
            310: pubfile.PageStructure(seq=310, applied_master=263, shape_count=4),
            320: pubfile.PageStructure(seq=320, applied_master=263, shape_count=0),
            330: pubfile.PageStructure(seq=330, applied_master=263, shape_count=0),
        }
        self.assertEqual(pubfile._blank_pages([310, 320, 330], chunks), [])

    def test_the_known_dummy_chunks_are_never_pages(self):
        dummy = sorted(pubfile._DUMMY_PAGE_SEQNUMS)[0]
        chunks = {
            310: pubfile.PageStructure(seq=310, applied_master=263, shape_count=4),
            dummy: pubfile.PageStructure(seq=dummy, applied_master=263, shape_count=0),
            330: pubfile.PageStructure(seq=330, applied_master=263, shape_count=7),
        }
        self.assertEqual(pubfile._blank_pages([310, dummy, 330], chunks), [])


class BlankPageRestorationTest(unittest.TestCase):
    """Putting back the pages libmspub never reported."""

    def document(self, pages: int) -> model.Document:
        document = model.Document()
        for index in range(pages):
            document.pages.append(
                model.Page(width=421.0, height=595.0, items=[frame_saying(f"p{index}")])
            )
        return document

    def structure(self, shaped: int, *blanks: int):
        s = structure_for(shaped, fields=False)
        s.blank_pages = [
            (position, pubfile.PageStructure(seq=900 + position, applied_master=263))
            for position in blanks
        ]
        return s

    def test_a_blank_page_is_put_back_where_the_file_places_it(self):
        document, structure = self.document(3), self.structure(3, 2)
        self.assertEqual(convert._restore_blank_pages(document, structure), 1)
        self.assertEqual(len(document.pages), 4)
        self.assertEqual([bool(p.items) for p in document.pages],
                         [True, False, True, True])
        # The page keeps the sheet its neighbours are on, or it would print
        # at the reader's default size in the middle of the document.
        self.assertEqual(document.pages[1].width, 421.0)
        self.assertEqual(document.pages[1].height, 595.0)

    def test_several_blanks_all_land_on_their_own_positions(self):
        document, structure = self.document(4), self.structure(4, 2, 5)
        self.assertEqual(convert._restore_blank_pages(document, structure), 2)
        self.assertEqual([bool(p.items) for p in document.pages],
                         [True, False, True, True, False, True])

    def test_the_two_halves_stay_the_same_length(self):
        # `_attribute_masters` reads a page's master off the chunks left
        # over only when both halves hold the same number of pages, so a
        # restored page has to reach the structure as well as the document.
        document, structure = self.document(3), self.structure(3, 2)
        convert._restore_blank_pages(document, structure)
        self.assertEqual(len(structure.pages), len(document.pages))

    def test_a_document_the_structure_does_not_describe_is_left_alone(self):
        # The positions are offsets into the whole document. If the pages
        # that did arrive are not the ones the file says arrived, every one
        # of them is a guess -- so nothing moves.
        document, structure = self.document(5), self.structure(3, 2)
        self.assertEqual(convert._restore_blank_pages(document, structure), 0)
        self.assertEqual(len(document.pages), 5)

    def test_a_position_off_the_end_of_the_document_moves_nothing(self):
        document, structure = self.document(3), self.structure(3, 9)
        self.assertEqual(convert._restore_blank_pages(document, structure), 0)
        self.assertEqual(len(document.pages), 3)

    def test_no_structure_and_no_blanks_are_both_no_ops(self):
        document = self.document(3)
        self.assertEqual(convert._restore_blank_pages(document, None), 0)
        self.assertEqual(convert._restore_blank_pages(document, self.structure(3)), 0)
        self.assertEqual(len(document.pages), 3)

    def test_the_restored_page_takes_its_number_from_its_place(self):
        # The point of putting it back: a '#' on page 3 of a document that
        # lost page 2 read '2' before, and every page after it was wrong.
        document, structure = self.document(3), self.structure(3, 2)
        structure.has_fields = True
        for page in document.pages:
            page.items[0].story.paragraphs[0].spans[0].text = "#"
        convert._restore_blank_pages(document, structure)
        convert._apply_master_pages(document, structure)
        numbers = [
            page.items[0].story.paragraphs[0].spans[0].text
            for page in document.pages if page.items
        ]
        self.assertEqual(numbers, ["1", "3", "4"])


class RealBlankPageTest(unittest.TestCase):
    """The reading, against the files it was taken from.

    Publisher's own exported PDFs of `1336` and `1338` are the ground
    truth: both impose 14 sheets two pages up, so both documents are 28
    pages, where libmspub reports 27 and 28. The one it is short is the
    blank leaf this finds.
    """

    @needs_samples
    def test_the_tracked_samples_state_no_blank_pages(self):
        # None of them is short a page, so the reading must not invent one.
        for name in (
            "MISSAL MARIANA E PEDRO.pub",
            "Cantico_dei_Cantici.pub",
            "Bus Meeting Zones & Luggage JLW.pub",
            "Blank Note Card (100_1502 Snail) (2 up).pub",
            "rotated_text.pub",
        ):
            with self.subTest(name=name):
                structure = pubfile.read_structure(SAMPLES / name)
                self.assertEqual(structure.blank_pages, [])

    @needs_newsletter
    def test_the_newsletters_page_count_matches_publishers_own_pdf(self):
        expected = {
            # name: (pages libmspub reports, blank page positions)
            "1336 kerkbode.pub": (27, [23]),
            "1337 kerkbode.pub": (31, [17]),
            "1338 kerkbode.pub": (28, []),
        }
        for name, (reported, blanks) in expected.items():
            source = SAMPLES / "cgk" / name
            if not source.exists():
                continue
            with self.subTest(name=name):
                structure = pubfile.read_structure(source)
                self.assertEqual(len(structure.pages), reported)
                self.assertEqual(
                    [position for position, _chunk in structure.blank_pages], blanks
                )
                # 28 and 32 -- a saddle-stitched booklet's page count is a
                # multiple of four, and reported alone neither of them is.
                self.assertEqual((reported + len(blanks)) % 4, 0)

    @needs_newsletter
    def test_the_page_order_list_accounts_for_libmspubs_own_order(self):
        # The whole placement rests on this: the entries of the list that
        # carry shapes are libmspub's pages, in libmspub's order. Measured
        # by the shapes both halves state the position of.
        source = NEWSLETTER
        document = convert.parse_document(source)
        structure = pubfile.read_structure(source)
        shaped = {page.seq for page in structure.pages}
        contents = pubfile._read_stream(
            source.read_bytes(), *pubfile._CONTENTS_STREAM
        )
        order = pubfile._read_page_order(
            contents, pubfile._chunk_references(contents)
        )
        placed = {seq: index for index, seq in
                  enumerate(seq for seq in order if seq in shaped)}
        measured = convert._page_by_chunk(document, structure)
        self.assertGreater(len(measured), 20)
        for chunk, index in measured.items():
            self.assertEqual(placed[chunk], index, f"chunk {chunk}")

    @needs_newsletter
    def test_the_restored_page_lands_between_the_pages_it_separates(self):
        source = NEWSLETTER
        document = convert.parse_document(source)
        structure = pubfile.read_structure(source)
        before = [len(page.items) for page in document.pages]
        self.assertEqual(convert._restore_blank_pages(document, structure), 1)
        self.assertEqual(len(document.pages), 28)
        self.assertEqual(document.pages[22].items, [])
        # Nothing else moved: the pages either side are the ones that were
        # either side of the gap.
        self.assertEqual(
            [len(page.items) for page in document.pages],
            before[:22] + [0] + before[22:],
        )


def print_setup_chunk(sheet_width: float, sheet_height: float) -> bytes:
    """A PRINT SETUP chunk, stating the sheet in EMU as the file does."""
    return _chunk([
        _u32(0x06, 600), _u32(0x07, 600),          # printer resolutions
        _u32(0x0B, round(sheet_width * EMU_PER_POINT)),
        _u32(0x0C, round(sheet_height * EMU_PER_POINT)),
    ])


class PrintSheetReadingTest(unittest.TestCase):
    """The sheet the publication is imposed onto, where the file states one.

    Not an enum and not a guess: two stated lengths. In the newsletters they
    read 914.0 x 681.4pt against a 421.0 x 595.0pt page, which is Publisher's
    own exported PDF to a fifth of a point.
    """

    def read(self, *chunks):
        buffer, refs = b"\x00" * 8, []
        for seq, (kind, payload) in enumerate(chunks):
            refs.append((seq, kind, len(buffer)))
            buffer += payload
        return pubfile._read_print_sheet(buffer, refs)

    def test_the_sheet_arrives_in_points(self):
        sheet = self.read((pubfile._PRINT_SETUP_CHUNK, print_setup_chunk(914.04, 681.36)))
        self.assertEqual([round(v, 2) for v in sheet], [914.04, 681.36])

    def test_a_file_stating_no_print_setup_reads_as_none(self):
        # Every file in the corpus that was never printed is one of these,
        # which is the whole limit of the reading.
        self.assertIsNone(self.read((pubfile._PAGE_CHUNK, page_chunk(shapes=(1,)))))

    def test_a_chunk_missing_either_dimension_reads_as_none(self):
        half = _chunk([_u32(0x0B, 11608200)])
        self.assertIsNone(self.read((pubfile._PRINT_SETUP_CHUNK, half)))

    @needs_newsletter
    def test_the_newsletters_state_the_sheet_publisher_exported(self):
        for name in ("1336 kerkbode.pub", "1337 kerkbode.pub", "1338 kerkbode.pub"):
            source = SAMPLES / "cgk" / name
            if not source.exists():
                continue
            with self.subTest(name=name):
                sheet = pubfile.read_structure(source).print_sheet
                self.assertIsNotNone(sheet)
                self.assertAlmostEqual(sheet[0], 914.04, delta=0.5)
                self.assertAlmostEqual(sheet[1], 681.36, delta=0.5)

    @needs_samples
    def test_a_file_never_printed_states_no_sheet(self):
        structure = pubfile.read_structure(SAMPLES / "rotated_text.pub")
        self.assertIsNone(structure.print_sheet)


class FacingDetectionTest(unittest.TestCase):
    """Deciding from the file whether the document is a booklet.

    The evidence is arithmetic on two stated lengths plus a page count: a
    sheet that fits two of these pages side by side, and a count that a
    saddle stitch could fold. Publisher's own layout type is not readable
    (actions.md 12), so this is the shape of a booklet rather than the
    file's word for one, and the caller can always override it.
    """

    def document(self, pages: int, width=421.02, height=594.9576):
        document = model.Document()
        for _ in range(pages):
            document.pages.append(model.Page(width=width, height=height))
        return document

    def structure(self, sheet):
        s = pubfile.FileStructure()
        s.print_sheet = sheet
        return s

    def test_a_two_up_sheet_and_a_foldable_count_reads_as_facing(self):
        self.assertTrue(convert._detect_facing_pages(
            self.document(28), self.structure((914.04, 681.36))))

    def test_a_count_a_saddle_stitch_cannot_fold_does_not(self):
        # 27 pages cannot be folded into sheets of four, so whatever the
        # sheet says, this is not a booklet the converter can lay out.
        for pages in (25, 26, 27, 29):
            with self.subTest(pages=pages):
                self.assertFalse(convert._detect_facing_pages(
                    self.document(pages), self.structure((914.04, 681.36))))

    def test_a_sheet_holding_one_page_does_not(self):
        # Lisa Hoogendijk: a 280 x 350mm page centred on A3, one page, and
        # Publisher's own PDF of it is a single page.
        self.assertFalse(convert._detect_facing_pages(
            self.document(4, width=793.7, height=992.1),
            self.structure((841.89, 1190.55))))

    def test_a_sheet_holding_three_or_more_pages_does_not(self):
        # Three up is an imposition this cannot describe as reader's
        # spreads, so it is left alone rather than guessed at.
        self.assertFalse(convert._detect_facing_pages(
            self.document(28), self.structure((1400.0, 681.36))))

    def test_no_sheet_and_no_structure_read_as_not_facing(self):
        self.assertFalse(convert._detect_facing_pages(self.document(28), None))
        self.assertFalse(convert._detect_facing_pages(
            self.document(28), self.structure(None)))

    def test_pages_of_differing_size_are_never_guessed_at(self):
        document = self.document(28)
        document.pages[5].width = 300.0
        self.assertFalse(convert._detect_facing_pages(
            document, self.structure((914.04, 681.36))))

    def test_an_empty_document_reads_as_not_facing(self):
        self.assertFalse(convert._detect_facing_pages(
            self.document(0), self.structure((914.04, 681.36))))


class RealFacingDetectionTest(unittest.TestCase):
    """Detection against the corpus, both ways round.

    The three newsletters are booklets -- Publisher's own exported PDFs of
    `1336` and `1338` impose fourteen two-up sheets each -- and nothing
    else in the corpus is known to be one. A detector that fired on a
    flyer would be worse than the flag it replaces, so the negatives
    matter more than the positives here.
    """

    def decide(self, source: Path) -> bool:
        document = convert.parse_document(source)
        structure = pubfile.read_structure(source)
        convert._restore_blank_pages(document, structure)
        return convert._detect_facing_pages(document, structure)

    @needs_newsletter
    def test_every_newsletter_is_read_as_a_booklet(self):
        for name in ("1336 kerkbode.pub", "1337 kerkbode.pub", "1338 kerkbode.pub"):
            source = SAMPLES / "cgk" / name
            if not source.exists():
                continue
            with self.subTest(name=name):
                self.assertTrue(self.decide(source))

    @needs_samples
    def test_nothing_else_in_the_corpus_is(self):
        for name in (
            "MISSAL MARIANA E PEDRO.pub",
            "Cantico_dei_Cantici.pub",
            "Bus Meeting Zones & Luggage JLW.pub",
            "Blank Note Card (100_1502 Snail) (2 up).pub",
            "rotated_text.pub",
        ):
            with self.subTest(name=name):
                self.assertFalse(self.decide(SAMPLES / name))

    @needs_newsletter
    def test_a_single_page_on_an_oversized_sheet_is_not_a_booklet(self):
        # Lisa Hoogendijk states a print sheet -- A3 -- and Publisher's own
        # PDF of it is one 280 x 350mm page. The negative that has the
        # chunk, which is the one that matters.
        source = SAMPLES / "cgk" / "Lisa Hoogendijk.pub"
        if not source.exists():
            self.skipTest(f"{source.name} absent")
        self.assertIsNotNone(pubfile.read_structure(source).print_sheet)
        self.assertFalse(self.decide(source))


def strings_chunk(*lengths: int) -> bytes:
    """A STRS chunk: how many stories, two words nothing reads, a length each.

    The lengths are in characters and cover the TEXT chunk exactly, which
    is what `_story_texts` checks before it trusts them.
    """
    return struct.pack("<3I", len(lengths), 0, 0) + struct.pack(
        "<%dI" % len(lengths), *lengths
    )


class StoryTextReadingTest(unittest.TestCase):
    """The TEXT chunk cut into stories by the lengths STRS states."""

    def test_cuts_the_text_at_the_stated_lengths(self):
        text = "Ziekenzorg\rAfgelopen 11 mei\rSecond story\r"
        stream = quill_stream(
            ("TEXT", text.encode("utf-16-le")),
            ("STRS", strings_chunk(28, 13)),
        )
        self.assertEqual(
            pubfile._story_texts(stream),
            ["Ziekenzorg\rAfgelopen 11 mei\r", "Second story\r"],
        )

    def test_refuses_lengths_that_do_not_cover_the_text(self):
        stream = quill_stream(
            ("TEXT", "Ziekenzorg\r".encode("utf-16-le")),
            ("STRS", strings_chunk(4, 4)),
        )
        self.assertEqual(pubfile._story_texts(stream), [])


class RealStoryTextTest(unittest.TestCase):
    """The story libmspub cuts off, read whole from the file."""

    @needs_truncated
    def test_reads_the_article_libmspub_truncates(self):
        structure = pubfile.read_structure(TRUNCATED)
        article = next(
            (t for t in structure.story_texts if t.startswith("Ziekenzorg\r")), None
        )
        self.assertIsNotNone(article, "the article is not among the stories read")
        # libmspub stops at 'voor over', mid-word, 81 characters in.
        self.assertIn("voor overleg", article)
        self.assertIn("Commissie dames ziekenzorg", article)
        self.assertEqual(len(article), 4562)


class TruncatedStoryTest(unittest.TestCase):
    """Text the event stream cut short, completed from the file.

    libmspub builds its spans from the character-run tables and, where it
    misreads them for a story, stops partway through -- so the frame holds
    a prefix of what the file says is there. The file's own text is the
    only thing that says how much is missing.
    """

    def restore(self, frame, *stories) -> model.Document:
        document = model.Document()
        document.pages.append(page_with(frame))
        convert._restore_truncated_stories(
            document, pubfile.FileStructure(story_texts=list(stories))
        )
        return document

    def test_completes_a_story_cut_off_mid_paragraph(self):
        frame = frame_saying("Ziekenzorg")
        self.restore(frame, "Ziekenzorg\rAfgelopen 11 mei\r")
        self.assertEqual(
            [p.text() for p in frame.story.paragraphs],
            ["Ziekenzorg", "Afgelopen 11 mei"],
        )

    def test_leaves_a_frame_holding_its_whole_story_alone(self):
        frame = frame_saying("Datum")
        self.restore(frame, "Datum\r", "Datum\r1e dienst:\r")
        self.assertEqual([p.text() for p in frame.story.paragraphs], ["Datum"])

    def test_refuses_a_prefix_two_stories_share(self):
        frame = frame_saying("Beste gemeenteleden,")
        self.restore(
            frame,
            "Beste gemeenteleden,\rHet duurt nog een poosje\r",
            "Beste gemeenteleden,\rU kunt uw voorkeur\r",
        )
        self.assertEqual(
            [p.text() for p in frame.story.paragraphs], ["Beste gemeenteleden,"]
        )

    def test_the_tail_continues_the_last_run_that_arrived(self):
        frame = frame_saying("Ziekenzorg")
        frame.story.paragraphs[0].spans[:] = [
            model.Span(text="Zieken", font="Calibri", size_pt=10.0, bold=True),
            model.Span(text="zorg", font="Calibri", size_pt=10.0),
        ]
        self.restore(frame, "Ziekenzorg\rAfgelopen 11 mei\r")
        tail = frame.story.paragraphs[1].spans[0]
        self.assertEqual(tail.text, "Afgelopen 11 mei")
        self.assertEqual(tail.font, "Calibri")
        self.assertEqual(tail.size_pt, 10.0)
        self.assertFalse(tail.bold)

    def test_reports_a_chain_s_story_once_though_both_frames_are_filled(self):
        first, second = frame_saying("Ziekenzorg"), frame_saying("Ziekenzorg")
        document = model.Document()
        document.pages.append(page_with(first, second))
        added = convert._restore_truncated_stories(
            document,
            pubfile.FileStructure(story_texts=["Ziekenzorg\rAfgelopen 11 mei\r"]),
        )
        self.assertEqual(added, [16])
        for frame in (first, second):
            self.assertEqual(frame.story.paragraphs[-1].text(), "Afgelopen 11 mei")


class RealTruncatedStoryTest(unittest.TestCase):
    """The article libmspub halves, against the file it came from.

    `1337 kerkbode.pub` is the only file in the corpus this fires on. Its
    'Ziekenzorg' story fills two facing pages and reaches the event stream
    as a single line, so it is both the reason this pass exists and the
    only real evidence that it works.
    """

    @needs_truncated
    def test_the_article_reaches_the_page_whole(self):
        work = Path(tempfile.mkdtemp())
        destination = work / "1337.idml"
        result = convert.convert(TRUNCATED, destination)
        self.assertTrue(result.ok, result.error)

        with zipfile.ZipFile(destination) as archive:
            stories = [
                "".join(
                    element.text or ""
                    for element in ET.fromstring(archive.read(name)).iter("Content")
                )
                for name in archive.namelist() if name.startswith("Stories/")
            ]
        article = next((s for s in stories if s.startswith("Ziekenzorg")), None)
        self.assertIsNotNone(article, "the article is not in the package")
        self.assertIn("voor overleg", article)
        self.assertIn("Commissie dames ziekenzorg", article)
        self.assertGreater(len(article), 4000)


class RestoredTextWarningTest(unittest.TestCase):
    """Text put back is text the operator has not proofread."""

    def note(self, restored):
        document = model.Document()
        convert._note_restored_text(document, restored)
        return document.warnings

    def test_says_how_much_went_back_and_into_how_many_stories(self):
        warnings = self.note([4323, 199])
        self.assertEqual(len(warnings), 1)
        self.assertIn("4522", warnings[0])
        self.assertIn("2 story/stories", warnings[0])

    def test_says_nothing_when_nothing_was_short(self):
        self.assertEqual(self.note([]), [])


def story_ids_chunk(*ids: int) -> bytes:
    """A SYID chunk: a word nothing reads, the count, then an id each."""
    return struct.pack("<2I", 0, len(ids)) + struct.pack("<%dI" % len(ids), *ids)


class StoryIdReadingTest(unittest.TestCase):
    """The name the file gives each story, in the order STRS cuts them.

    A shape says which story it holds by id, not by position, so without
    this table the id says nothing. With it, a frame can be tied to its
    words without reading either -- which is the whole point, because the
    frames this exists for arrived with no words in them.
    """

    def test_reads_one_id_per_story(self):
        stream = quill_stream(("SYID", story_ids_chunk(2, 3, 13, 40992)))
        self.assertEqual(pubfile._story_ids(stream), [2, 3, 13, 40992])

    def test_refuses_a_count_the_chunk_has_no_room_for(self):
        # Truncation must not be read as a shorter table: the positions are
        # what carry the meaning, so a table missing its tail is not a
        # table missing nothing.
        stream = quill_stream(("SYID", struct.pack("<2I", 0, 40) + b"\x02\x00\x00\x00"))
        self.assertEqual(pubfile._story_ids(stream), [])

    def test_a_stream_without_the_chunk_states_no_ids(self):
        stream = quill_stream(("TEXT", "Ziekenzorg\r".encode("utf-16-le")))
        self.assertEqual(pubfile._story_ids(stream), [])


class StoryOfShapeTest(unittest.TestCase):
    """A shape's story id taken through SYID to a position in STRS."""

    def structure(self, **kwargs) -> pubfile.FileStructure:
        defaults = dict(
            story_texts=["Datum\r", "Beste gemeenteleden,\r", "Lieve gemeente,\r"],
            story_ids=[4, 62, 70],
            shape_stories={295: 4, 313: 62, 347: 70},
        )
        defaults.update(kwargs)
        return pubfile.FileStructure(**defaults)

    def test_composes_the_id_into_a_position(self):
        structure = self.structure()
        self.assertEqual(structure.story_of_shape(313), 1)
        self.assertEqual(structure.story_of_shape(347), 2)

    def test_two_tables_describing_different_documents_answer_nothing(self):
        # Neither table can be trusted about the other unless they agree on
        # how many stories there are to name.
        structure = self.structure(story_ids=[4, 62])
        self.assertIsNone(structure.story_of_shape(313))

    def test_a_shape_naming_no_story_has_none(self):
        self.assertIsNone(self.structure().story_of_shape(999))

    def test_an_id_the_table_does_not_list_has_none(self):
        structure = self.structure(shape_stories={295: 12345})
        self.assertIsNone(structure.story_of_shape(295))

    def test_a_file_stating_no_ids_answers_nothing(self):
        self.assertIsNone(self.structure(story_ids=[]).story_of_shape(313))


class RealStoryIdTest(unittest.TestCase):
    """SYID and STRS, read off the file that needs them."""

    @needs_truncated
    def test_every_story_the_ruler_cuts_is_named_once(self):
        structure = pubfile.read_structure(TRUNCATED)
        self.assertEqual(len(structure.story_ids), len(structure.story_texts))
        self.assertEqual(len(set(structure.story_ids)), len(structure.story_ids))

    @needs_truncated
    def test_the_letter_libmspub_delivers_empty_is_found_by_id(self):
        # Two frames, pages 24 and 25, one story: libmspub opens both and
        # closes them again without a paragraph, so the id is the only way
        # back to the words.
        structure = pubfile.read_structure(TRUNCATED)
        shapes = [
            seq for seq in structure.shape_stories
            if (index := structure.story_of_shape(seq)) is not None
            and structure.story_texts[index].startswith("Beste gemeenteleden,\rHet duurt")
        ]
        self.assertEqual(len(shapes), 2, "the letter is held by a pair of frames")


class UndeliveredStoryTest(unittest.TestCase):
    """Frames libmspub opened and left completely empty.

    Not the same failure as text cut short, and not reachable the same way.
    A frame holding a prefix can be matched against the file's stories by
    that prefix; a frame holding nothing cannot, because every story starts
    with nothing. The file names the story each shape holds, and that name
    still answers when the text does not.
    """

    def frame_at(self, x, y, w=100.0, h=50.0) -> model.TextFrame:
        return model.TextFrame(x=x, y=y, width=w, height=h)

    def anchor(self, shape_seq, frame, page) -> pubfile.ShapeAnchor:
        """Where the file puts a shape, given where libmspub drew it."""
        return pubfile.ShapeAnchor(
            shape_seq=shape_seq,
            centre_x=frame.x + frame.width / 2.0 - page.width / 2.0,
            centre_y=frame.y + frame.height / 2.0 - page.height / 2.0,
        )

    def fill(self, frames, shape_stories, story_texts, extra_anchors=()):
        page = model.Page(width=612.0, height=792.0)
        for frame in frames.values():
            page.items.append(frame)
        document = model.Document(pages=[page])
        anchors = [
            self.anchor(seq, frame, page) for seq, frame in frames.items()
        ]
        anchors += [self.anchor(seq, frames[on], page) for seq, on in extra_anchors]
        structure = pubfile.FileStructure(
            story_texts=list(story_texts),
            story_ids=[10 * (i + 1) for i in range(len(story_texts))],
            shape_stories=dict(shape_stories),
            anchors=anchors,
            shape_pages={a.shape_seq: 266 for a in anchors},
        )
        filled = convert._fill_undelivered_stories(document, structure)
        return document, filled

    def test_an_empty_frame_gets_the_story_the_file_names(self):
        frame = self.frame_at(100.0, 100.0)
        _document, filled = self.fill(
            {313: frame}, {313: 20},
            ["Datum\r", "Beste gemeenteleden,\rHet duurt nog een poosje\r"],
        )
        self.assertEqual(
            [p.text() for p in frame.story.paragraphs],
            ["Beste gemeenteleden,", "Het duurt nog een poosje"],
        )
        self.assertEqual(filled, [len("Beste gemeenteleden,Het duurt nog een poosje")])

    def test_a_frame_holding_text_is_left_alone(self):
        # Cut-short text is `_restore_truncated_stories`' business, and it
        # keeps libmspub's reading of the runs that did arrive.
        frame = frame_saying("Datum")
        frame.x, frame.y, frame.width, frame.height = 100.0, 100.0, 100.0, 50.0
        self.fill({313: frame}, {313: 20}, ["Datum\r", "Beste gemeenteleden,\r"])
        self.assertEqual([p.text() for p in frame.story.paragraphs], ["Datum"])

    def test_a_frame_two_shapes_claim_is_left_alone(self):
        # `Lisa Hoogendijk.pub` sits a decorative shape 0.2pt from a text
        # frame, inside the half-point the match allows, so both resolve to
        # it -- and the decoration's story is not the one that belongs
        # there. Where two shapes claim one frame, neither speaks for it.
        frame = self.frame_at(100.0, 100.0)
        self.fill(
            {313: frame}, {313: 20, 347: 10},
            ["Datum\r", "Beste gemeenteleden,\r"],
            extra_anchors=[(347, 313)],
        )
        self.assertTrue(frame.story.is_empty())

    def test_a_story_that_is_itself_empty_leaves_the_frame_empty(self):
        # Publisher's own blank frames are blank, and the file agreeing
        # that there is nothing there is the answer, not a failure to look.
        frame = self.frame_at(100.0, 100.0)
        _document, filled = self.fill({313: frame}, {313: 10}, [" \r", "Datum\r"])
        self.assertTrue(frame.story.is_empty())
        self.assertEqual(filled, [])

    def test_a_shape_the_file_does_not_name_a_story_for_is_passed_over(self):
        frame = self.frame_at(100.0, 100.0)
        self.fill({313: frame}, {}, ["Datum\r"])
        self.assertTrue(frame.story.is_empty())

    def test_a_file_that_states_no_stories_changes_nothing(self):
        frame = self.frame_at(100.0, 100.0)
        document = model.Document(pages=[page_with(frame)])
        self.assertEqual(
            convert._fill_undelivered_stories(document, pubfile.FileStructure()), []
        )
        self.assertTrue(frame.story.is_empty())


class BlankFrameDropTest(unittest.TestCase):
    """An empty frame draws nothing -- asked once the file has had its say.

    libmspub's own rule, moved to where the answer is knowable. `model`
    cannot tell a frame Publisher left blank from one libmspub failed to
    fill, so it places both and this decides between them afterwards.
    """

    def drop(self, *items) -> List[model.Item]:
        document = model.Document(pages=[page_with(*items)])
        convert._drop_blank_frames(document)
        return document.pages[0].items

    def test_an_empty_frame_with_no_paint_goes(self):
        self.assertEqual(self.drop(model.TextFrame()), [])

    def test_a_frame_holding_words_stays(self):
        frame = frame_saying("Beste gemeenteleden,")
        self.assertEqual(self.drop(frame), [frame])

    def test_an_empty_frame_that_is_painted_stays(self):
        # It draws its fill even with nothing in it, which is the whole of
        # why the rule asks about paint at all.
        frame = model.TextFrame()
        frame.style.fill = (255, 0, 0)
        self.assertEqual(self.drop(frame), [frame])

    def test_an_empty_frame_inside_a_group_goes(self):
        group = model.Group(children=[model.TextFrame(), frame_saying("Datum")])
        self.drop(group)
        self.assertEqual(len(group.children), 1)

    def test_nothing_else_is_touched(self):
        rectangle = model.Rectangle(x=1.0, y=2.0, width=3.0, height=4.0)
        self.assertEqual(self.drop(rectangle), [rectangle])


class FilledTextWarningTest(unittest.TestCase):
    """Words with no formatting behind them have to be flagged as such."""

    def note(self, filled):
        document = model.Document()
        convert._note_filled_text(document, filled)
        return document.warnings

    def test_says_how_much_went_in_and_into_how_many_places(self):
        warnings = self.note([2187, 2106])
        self.assertEqual(len(warnings), 1)
        self.assertIn("4293", warnings[0])
        self.assertIn("2 frame(s) and table(s)", warnings[0])

    def test_says_the_type_is_not_publishers(self):
        self.assertIn("default face", self.note([2187])[0])

    def test_says_nothing_when_every_frame_arrived_filled(self):
        self.assertEqual(self.note([]), [])


def divisions_chunk(*bounds: int) -> bytes:
    """A TCD chunk: the boundary count, two words nothing reads, then them."""
    return struct.pack("<3I", len(bounds), 0, 0xFF00) + struct.pack(
        "<%dI" % len(bounds), *bounds
    )


class CellDivisionReadingTest(unittest.TestCase):
    """Where each of a table's cells stops in the story it is cut from."""

    def test_reads_the_boundaries(self):
        stream = quill_stream(("TCD ", divisions_chunk(21, 51, 65)))
        self.assertEqual(pubfile._cell_divisions(stream), [[21, 51, 65]])

    def test_refuses_boundaries_that_do_not_ascend(self):
        # A cell cannot end before the cell in front of it, so this is not
        # the layout being read and cutting on it would scramble the table.
        stream = quill_stream(("TCD ", divisions_chunk(21, 12, 65)))
        self.assertEqual(pubfile._cell_divisions(stream), [])

    def test_refuses_a_count_the_chunk_has_no_room_for(self):
        stream = quill_stream(
            ("TCD ", struct.pack("<3I", 40, 0, 0xFF00) + b"\x15\x00\x00\x00")
        )
        self.assertEqual(pubfile._cell_divisions(stream), [])

    def test_reads_one_per_table(self):
        stream = quill_stream(
            ("TCD ", divisions_chunk(21, 51)), ("TCD ", divisions_chunk(9)),
        )
        self.assertEqual(pubfile._cell_divisions(stream), [[21, 51], [9]])


class DivisionPairingTest(unittest.TestCase):
    """Which division cuts which story, settled by order and checked by count.

    Nothing in a TCD chunk names its table, so the pairing rests on the
    stream order matching the STRS order of the stories being cut. That is
    only usable because it can be checked: every division has to state the
    cell count of the table it lands on.
    """

    def tables(self, *specs) -> dict:
        return {
            ("grid", index): pubfile.TableStructure(story_id=story, cell_count=cells)
            for index, (story, cells) in enumerate(specs)
        }

    def test_pairs_them_in_the_order_the_stories_come(self):
        paired = pubfile._pair_divisions(
            self.tables((70, 3), (62, 2)), [[5], [4, 9]], [62, 70],
        )
        # Story 62 sits first in SYID, so the first division is its own.
        self.assertEqual(paired, {0: [5], 1: [4, 9]})

    def test_a_count_that_does_not_fit_drops_the_whole_pairing(self):
        # One division landing on the wrong table would cut somebody else's
        # words into these cells, and nothing on the page would look wrong.
        self.assertEqual(
            pubfile._pair_divisions(
                self.tables((70, 3), (62, 9)), [[5], [4, 9]], [62, 70],
            ),
            {},
        )

    def test_a_single_celled_table_needs_no_division(self):
        paired = pubfile._pair_divisions(
            self.tables((62, 1), (70, 3)), [[4, 9]], [62, 70],
        )
        self.assertEqual(paired, {1: [4, 9]})


class TableCellTextTest(unittest.TestCase):
    """A table's story cut into its cells."""

    def structure(self, **kwargs) -> pubfile.FileStructure:
        signature = pubfile.table_signature([50.0], [10.0, 10.0, 10.0])
        defaults = dict(
            tables={signature: pubfile.TableStructure(story_id=70, cell_count=3)},
            story_texts=["Maandag\rKrooswijkhof\rJos Mol\r"],
            story_ids=[70],
            table_divisions={0: [7, 20]},
        )
        defaults.update(kwargs)
        return pubfile.FileStructure(**defaults)

    def cut(self, structure):
        return structure.table_cell_texts([50.0], [10.0, 10.0, 10.0])

    def test_cuts_the_story_one_piece_per_cell(self):
        self.assertEqual(
            self.cut(self.structure()), ["Maandag", "Krooswijkhof", "Jos Mol\r"]
        )

    def test_a_single_celled_table_takes_the_whole_story(self):
        signature = pubfile.table_signature([50.0], [10.0])
        structure = pubfile.FileStructure(
            tables={signature: pubfile.TableStructure(story_id=70, cell_count=1)},
            story_texts=["Beste gemeenteleden,\rU kunt uw voorkeur\r"],
            story_ids=[70],
        )
        self.assertEqual(
            structure.table_cell_texts([50.0], [10.0]),
            ["Beste gemeenteleden,\rU kunt uw voorkeur\r"],
        )

    def test_a_table_with_no_division_is_not_cut(self):
        self.assertIsNone(self.cut(self.structure(table_divisions={})))

    def test_a_boundary_past_the_end_of_the_story_is_not_cut(self):
        self.assertIsNone(self.cut(self.structure(table_divisions={0: [7, 900]})))

    def test_a_story_id_the_file_does_not_name_is_not_cut(self):
        self.assertIsNone(self.cut(self.structure(story_ids=[4])))


class UndeliveredTableTest(unittest.TestCase):
    """Tables libmspub opened and left with every cell empty."""

    def table(self, cells=3) -> model.Table:
        table = model.Table(column_widths=[50.0], row_heights=[10.0] * cells)
        table.cells = [model.TableCell(row=i, column=0) for i in range(cells)]
        return table

    def fill(self, table, **kwargs):
        signature = pubfile.table_signature(table.column_widths, table.row_heights)
        defaults = dict(
            tables={
                signature: pubfile.TableStructure(story_id=70, cell_count=len(table.cells))
            },
            story_texts=["Maandag\rKrooswijkhof\rJos Mol\r"],
            story_ids=[70],
            table_divisions={0: [7, 20]},
        )
        defaults.update(kwargs)
        document = model.Document(pages=[page_with(table)])
        return convert._fill_undelivered_tables(document, pubfile.FileStructure(**defaults))

    def texts(self, table) -> List[str]:
        return ["".join(p.text() for p in c.story.paragraphs) for c in table.cells]

    def test_an_empty_table_gets_the_story_cut_into_its_cells(self):
        table = self.table()
        filled = self.fill(table)
        self.assertEqual(self.texts(table), ["Maandag", "Krooswijkhof", "Jos Mol"])
        self.assertEqual(filled, [len("MaandagKrooswijkhofJos Mol")])

    def test_a_table_holding_text_is_left_alone(self):
        # Publisher's row order and the story's typing order can differ, so
        # a table that arrived with text keeps libmspub's placement of it.
        table = self.table()
        table.cells[0].story.paragraphs.append(
            model.Paragraph(spans=[model.Span(text="8-7-2026")])
        )
        self.fill(table)
        self.assertEqual(self.texts(table), ["8-7-2026", "", ""])

    def test_a_table_the_file_states_no_division_for_is_left_alone(self):
        table = self.table()
        self.fill(table, table_divisions={})
        self.assertEqual(self.texts(table), ["", "", ""])

    def test_a_story_with_no_words_leaves_the_table_empty(self):
        table = self.table()
        filled = self.fill(table, story_texts=["\r\r\r"], table_divisions={0: [0, 1]})
        self.assertEqual(self.texts(table), ["", "", ""])
        self.assertEqual(filled, [])

    def test_a_document_with_no_structure_read_changes_nothing(self):
        table = self.table()
        document = model.Document(pages=[page_with(table)])
        self.assertEqual(convert._fill_undelivered_tables(document, None), [])


class RealUndeliveredTableTest(unittest.TestCase):
    """Page 27 of 1337: a cleaning rota whose 90 cells all arrive empty."""

    @needs_truncated
    def test_the_rota_columns_are_cut_in_the_order_they_are_printed(self):
        # Checked against the Publisher PDF when this was written: each
        # column's forty lines match the printed page exactly, in order.
        structure = pubfile.read_structure(TRUNCATED)
        document = convert.parse_document(TRUNCATED)
        convert._fill_undelivered_tables(document, structure)
        rotas = [
            item for item in document.all_items()
            if isinstance(item, model.Table) and len(item.cells) in (44, 45)
        ]
        self.assertEqual(len(rotas), 2, "the rota is two single-column tables")
        left = next(t for t in rotas if len(t.cells) == 45)
        texts = ["".join(p.text() for p in c.story.paragraphs) for c in left.cells]
        self.assertEqual(texts[0].strip(), "Maandag 29 juni 2026")
        self.assertEqual(texts[1].strip(), "Krooswijkhof |09:00-10.45 uur")
        self.assertEqual(texts[2].strip(), "Corné de Jong")
        self.assertEqual(texts[7].strip(), "Jos Mol")
        self.assertEqual(texts[8].strip(), "", "a blank row separates the groups")
        self.assertEqual(texts[9].strip(), "Donderdag 02 juli 2026")

    @needs_truncated
    def test_the_intro_box_takes_the_whole_story(self):
        structure = pubfile.read_structure(TRUNCATED)
        document = convert.parse_document(TRUNCATED)
        convert._fill_undelivered_tables(document, structure)
        box = next(
            item for item in document.all_items()
            if isinstance(item, model.Table) and len(item.cells) == 1
        )
        text = "".join(p.text() for p in box.cells[0].story.paragraphs)
        self.assertIn("Beste gemeenteleden", text)
        self.assertIn("schoonmaak.cgkdordrecht-c.nl", text)
