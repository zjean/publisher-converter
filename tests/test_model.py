"""Event replay: the contract between the C++ parser and the document tree."""

from __future__ import annotations

import unittest

from pubidml import model

from . import support
from .support import event


class CleanTextTest(unittest.TestCase):
    """clean_text is the only guard between Publisher's control bytes and XML."""

    def test_strips_every_c0_control_xml_forbids(self):
        # Publisher uses several of these structurally: 0x0C page break,
        # 0x0E column break, 0x13-0x15 field delimiters, 0x1F optional
        # hyphen. Any survivor makes the Story part unparseable.
        for code in range(0x20):
            if code in (0x09, 0x0B):
                continue
            with self.subTest(code=hex(code)):
                self.assertEqual(model.clean_text(f"a{chr(code)}b"), "ab")

    def test_keeps_tab_because_idml_splits_runs_on_it(self):
        self.assertEqual(model.clean_text("a\tb"), "a\tb")

    def test_vertical_tab_becomes_the_unicode_line_separator(self):
        self.assertEqual(model.clean_text("a\x0bb"), "a\u2028b")

    def test_strips_surrogates_and_non_characters(self):
        self.assertEqual(model.clean_text("a\ud800b\uffffc\ufffed"), "abcd")

    def test_leaves_ordinary_text_byte_identical(self):
        for sample in ("Grüße", "Русский текст", "日本語", "a b", "Zoë"):
            with self.subTest(sample=sample):
                self.assertEqual(model.clean_text(sample), sample)


class LineBreakTest(unittest.TestCase):
    """A forced break arrives by two routes; they must agree."""

    def test_callback_and_raw_byte_produce_the_same_character(self):
        via_callback = support.document(
            *[
                event("startTextObject", {"svg:width": "3in", "svg:height": "2in"}),
                event("openParagraph", {}),
                event("openSpan", {}),
                event("insertText", text="a"),
                event("insertLineBreak"),
                event("insertText", text="b"),
                event("closeSpan"),
                event("closeParagraph"),
                event("endTextObject"),
            ]
        )
        via_raw_byte = support.document(*support.text_frame("a\x0bb"))
        self.assertEqual(support.only_span(via_callback).text, "a\u2028b")
        self.assertEqual(
            support.only_span(via_callback).text, support.only_span(via_raw_byte).text
        )


class CompletenessTest(unittest.TestCase):
    """A stream cut on a line boundary is short, not malformed."""

    def test_stream_with_end_document_is_complete(self):
        doc = support.document(*support.text_frame("hello"))
        self.assertTrue(doc.complete)

    def test_truncated_stream_is_not_complete(self):
        doc = support.build(*(support.page() + support.text_frame("hello")))
        # Syntactically perfect and it even has content — only the sentinel
        # distinguishes it from a whole document.
        self.assertEqual(len(doc.pages), 1)
        self.assertFalse(doc.complete)


class GroupTest(unittest.TestCase):
    """No .pub in the corpus contains a group, so this is the only coverage."""

    def _grouped_image(self) -> model.Document:
        return support.document(
            event("openGroup", {}),
            event(
                "setStyle",
                {
                    "draw:fill": "bitmap",
                    "librevenge:mime-type": "image/png",
                    "draw:fill-image": "aGVsbG8=",  # b"hello"
                },
            ),
            event(
                "drawRectangle",
                {
                    "svg:x": "1in",
                    "svg:y": "1in",
                    "svg:width": "2in",
                    "svg:height": "2in",
                },
            ),
            event("closeGroup"),
        )

    def test_group_children_are_reachable_and_promoted(self):
        doc = self._grouped_image()
        groups = [i for i in model._walk(doc.pages[0].items) if isinstance(i, model.Group)]
        images = [i for i in model._walk(doc.pages[0].items) if isinstance(i, model.Image)]
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].data, b"hello")

    def test_group_bounds_enclose_the_children(self):
        group = next(
            i for i in self._grouped_image().pages[0].items if isinstance(i, model.Group)
        )
        self.assertAlmostEqual(group.x, 72.0)
        self.assertAlmostEqual(group.width, 144.0)

    def test_empty_group_is_dropped(self):
        doc = support.document(event("openGroup", {}), event("closeGroup"))
        self.assertEqual(doc.pages[0].items, [])


class HostileStreamTest(unittest.TestCase):
    def test_unknown_event_is_ignored(self):
        doc = support.document(event("somethingLibrevengeAdded", {"x": "1"}))
        self.assertTrue(doc.complete)

    def test_event_name_cannot_reach_arbitrary_attributes(self):
        # Dispatch is getattr(self, f"_on_{event}"), so the prefix is what
        # confines it to handlers. A name that would otherwise resolve must
        # still be a no-op.
        doc = support.document(event("_walk", {"x": "1"}), event("finish", {}))
        self.assertTrue(doc.complete)

    def test_malformed_json_line_raises(self):
        with self.assertRaises(Exception):
            support.build("{not json")

    def test_empty_stream_yields_an_empty_incomplete_document(self):
        doc = support.build("")
        self.assertEqual(doc.pages, [])
        self.assertFalse(doc.complete)

    def test_absurd_geometry_is_dropped_and_reported(self):
        doc = support.document(
            event(
                "drawRectangle",
                {"svg:x": "999999in", "svg:y": "0in", "svg:width": "1in", "svg:height": "1in"},
            )
        )
        self.assertEqual(doc.pages[0].items, [])
        self.assertTrue(any("dropped" in w for w in doc.warnings))


class TextColumnTest(unittest.TestCase):
    """Publisher's per-box column settings, as libmspub reports them.

    libmspub emits `fo:column-count` only when the .pub recorded one, but
    `fo:column-gap` on essentially every text object — so the gap alone
    says nothing about whether a box has columns.
    """

    def _frame(self, **props) -> model.TextFrame:
        base = {"svg:x": "1in", "svg:y": "1in", "svg:width": "6in", "svg:height": "4in"}
        base.update(props)
        doc = support.document(
            event("startTextObject", base),
            event("openParagraph", {}),
            event("openSpan", {"style:font-name": "Arial", "fo:font-size": "10pt"}),
            event("insertText", text="text"),
            event("closeSpan"),
            event("closeParagraph"),
            event("endTextObject"),
        )
        return doc.pages[0].items[0]

    def test_a_column_count_is_recorded(self):
        # pubdump serialises every property with getStr(), so an int
        # property arrives as a string.
        self.assertEqual(self._frame(**{"fo:column-count": "3"}).columns, 3)

    def test_the_column_gap_is_converted_to_points(self):
        frame = self._frame(**{"fo:column-count": "2", "fo:column-gap": "0.0787in"})
        self.assertAlmostEqual(frame.column_gap, 0.0787 * 72.0, places=4)

    def test_a_gap_without_a_count_leaves_the_box_single_column(self):
        # Every text object in the sample corpus looks like this.
        frame = self._frame(**{"fo:column-gap": "0.0787in"})
        self.assertEqual(frame.columns, 1)

    def test_a_plain_box_is_single_column_with_no_gap(self):
        frame = self._frame()
        self.assertEqual((frame.columns, frame.column_gap), (1, 0.0))

    def test_a_degenerate_count_never_drops_below_one(self):
        for value in ("0", "-2", "", "banana"):
            with self.subTest(value=value):
                self.assertEqual(self._frame(**{"fo:column-count": value}).columns, 1)

    def test_an_integer_count_is_also_accepted(self):
        self.assertEqual(self._frame(**{"fo:column-count": 4}).columns, 4)


class ParseColorTest(unittest.TestCase):
    def test_valid_and_invalid_forms(self):
        self.assertEqual(model.parse_color("#ff8000"), (255, 128, 0))
        for bad in (None, "", "ff8000", "#ff80", "#gggggg", "#ff80000"):
            with self.subTest(bad=bad):
                self.assertIsNone(model.parse_color(bad))


if __name__ == "__main__":
    unittest.main()
