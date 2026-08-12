"""Structure read from the .pub itself, and what is done with it.

This is the one place the converter looks at the binary directly rather
than going through libmspub, so the tests care about two things: that it
reads the right thing, and that it never costs a conversion when it
cannot.
"""

from __future__ import annotations

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
