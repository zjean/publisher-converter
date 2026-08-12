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
    def _document(self, pages: int, text: str = " #") -> model.Document:
        document = model.Document()
        for _ in range(pages):
            document.pages.append(page_with(frame_saying(text),
                                            model.Rectangle(width=10.0, height=10.0)))
        return document

    def footers(self, document):
        return [p.items[0].story.paragraphs[0].spans[0].text for p in document.pages]

    def test_the_placeholder_becomes_the_real_page_number(self):
        document = self._document(3)
        convert._resolve_page_numbers(document, structure_for(3, fields=True))
        self.assertEqual(self.footers(document), [" 1", " 2", " 3"])
        self.assertTrue(any("page-number field" in w for w in document.warnings))

    def test_a_document_with_no_field_table_is_left_alone(self):
        # No TOKN chunk means every '#' in the document was typed.
        document = self._document(3)
        convert._resolve_page_numbers(document, structure_for(3, fields=False))
        self.assertEqual(self.footers(document), [" #", " #", " #"])
        self.assertEqual(document.warnings, [])

    def test_no_structure_at_all_is_left_alone(self):
        document = self._document(2)
        convert._resolve_page_numbers(document, None)
        self.assertEqual(self.footers(document), [" #", " #"])

    def test_a_page_count_mismatch_is_left_alone(self):
        # Nothing can be attributed if the two halves cannot be lined up.
        document = self._document(3)
        convert._resolve_page_numbers(document, structure_for(5, fields=True))
        self.assertEqual(self.footers(document), [" #", " #", " #"])
        self.assertEqual(document.warnings, [])

    def test_inconsistent_master_content_is_left_alone(self):
        # If pages sharing a master did not get the same shapes, the
        # attribution is wrong and no substitution is justified.
        document = self._document(3)
        document.pages[1].items[0].x = 999.0
        convert._resolve_page_numbers(document, structure_for(3, fields=True))
        self.assertEqual(self.footers(document), [" #", " #", " #"])

    def test_a_hash_outside_master_content_is_left_alone(self):
        document = model.Document()
        for _ in range(2):
            page = page_with(frame_saying("nothing here"), frame_saying("Suite #3"))
            document.pages.append(page)
        convert._resolve_page_numbers(document, structure_for(2, fields=True, shapes=1))
        second = [p.items[1].story.paragraphs[0].spans[0].text for p in document.pages]
        self.assertEqual(second, ["Suite #3", "Suite #3"])

    def test_surrounding_text_survives(self):
        document = self._document(2, text="Page # of many")
        convert._resolve_page_numbers(document, structure_for(2, fields=True))
        self.assertEqual(self.footers(document), ["Page 1 of many", "Page 2 of many"])


@needs_samples
@unittest.skipUnless(convert.PUBDUMP.exists(), "pubdump not built")
class EndToEndTest(unittest.TestCase):
    def test_the_missal_footers_number_one_to_fifteen(self):
        source = SAMPLES / "MISSAL MARIANA E PEDRO.pub"
        document = convert.parse_document(source)
        convert._resolve_page_numbers(document, pubfile.read_structure(source))
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
        convert._resolve_page_numbers(document, pubfile.read_structure(source))
        after = [s.text for p in document.pages
                 for i in model._walk(p.items) if isinstance(i, model.TextFrame)
                 for par in i.story.paragraphs for s in par.spans]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
