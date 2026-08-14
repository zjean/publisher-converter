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


class LineSpacingTest(unittest.TestCase):
    """Publisher's line spacing, as libmspub reports it.

    MSPUBCollector inserts Publisher's raw "spaces" figure as a *percent*
    and its point figure as points:

        if (type == LINE_SPACING_SP)  insert("fo:line-height", n, RVNG_PERCENT)
        else if (type == LINE_SPACING_PT) insert("fo:line-height", n, RVNG_POINT)

    So "90.0000%" means 0.9 spaces -- nine tenths of single line spacing --
    and not 90% of the font size. It also skips the property entirely at
    exactly 1 sp, so absence means single spacing.
    """

    def _paragraph(self, **props) -> model.Paragraph:
        doc = support.document(
            event("startTextObject", {"svg:width": "3in", "svg:height": "2in"}),
            event("openParagraph", props),
            event("openSpan", {"fo:font-size": "10pt"}),
            event("insertText", text="text"),
            event("closeSpan"),
            event("closeParagraph"),
            event("endTextObject"),
        )
        return doc.pages[0].items[0].story.paragraphs[0]

    def test_a_percentage_is_read_as_a_multiple_of_single_spacing(self):
        paragraph = self._paragraph(**{"fo:line-height": "90.0000%"})
        self.assertAlmostEqual(paragraph.line_spacing_multiple, 0.9)
        self.assertIsNone(paragraph.line_spacing_pt)

    def test_a_point_value_is_read_as_exact_leading(self):
        paragraph = self._paragraph(**{"fo:line-height": "10.5000pt"})
        self.assertAlmostEqual(paragraph.line_spacing_pt, 10.5)
        self.assertIsNone(paragraph.line_spacing_multiple)

    def test_absence_means_single_spacing_and_is_left_unset(self):
        paragraph = self._paragraph()
        self.assertIsNone(paragraph.line_spacing_multiple)
        self.assertIsNone(paragraph.line_spacing_pt)

    def test_wider_spacing_is_read_faithfully(self):
        for value, expected in (("150.0000%", 1.5), ("200.0000%", 2.0), ("25.0000%", 0.25)):
            with self.subTest(value=value):
                paragraph = self._paragraph(**{"fo:line-height": value})
                self.assertAlmostEqual(paragraph.line_spacing_multiple, expected)

    def test_nonsense_is_ignored_rather_than_guessed(self):
        for value in ("", "banana", "%"):
            with self.subTest(value=value):
                paragraph = self._paragraph(**{"fo:line-height": value})
                self.assertIsNone(paragraph.line_spacing_multiple)
                self.assertIsNone(paragraph.line_spacing_pt)


class ParseColorTest(unittest.TestCase):
    def test_valid_and_invalid_forms(self):
        self.assertEqual(model.parse_color("#ff8000"), (255, 128, 0))
        for bad in (None, "", "ff8000", "#ff80", "#gggggg", "#ff80000"):
            with self.subTest(bad=bad):
                self.assertIsNone(model.parse_color(bad))


if __name__ == "__main__":
    unittest.main()


class GradientTest(unittest.TestCase):
    """Publisher gradients, as libmspub reports them.

    Only the first stop was kept, which is how a background disappears: the
    ramps in the sample corpus start white, so a white-to-cream panel
    collapsed to white on white paper.
    """

    STOPS = [
        {"svg:offset": "0.0000%", "svg:stop-color": "#ffffff",
         "svg:stop-opacity": "100.0000%"},
        {"svg:offset": "50.0000%", "svg:stop-color": "#ffeedd",
         "svg:stop-opacity": "100.0000%"},
        {"svg:offset": "100.0000%", "svg:stop-color": "#ffffff",
         "svg:stop-opacity": "100.0000%"},
    ]

    def _style(self, **extra) -> model.GraphicStyle:
        props = {"draw:fill": "gradient", "draw:angle": "90.0000in"}
        props.update(extra)
        doc = support.document(
            event("setStyle", props),
            event(
                "drawRectangle",
                {"svg:x": "1in", "svg:y": "1in", "svg:width": "2in", "svg:height": "1in"},
            ),
        )
        return doc.pages[0].items[0].style

    def test_every_stop_is_kept_in_order(self):
        gradient = self._style(**{"svg:linearGradient": self.STOPS}).gradient
        self.assertIsNotNone(gradient)
        self.assertEqual(
            [(s.location, s.color) for s in gradient.stops],
            [(0.0, (255, 255, 255)), (50.0, (255, 238, 221)), (100.0, (255, 255, 255))],
        )

    def test_the_angle_is_read_past_librevenges_bogus_unit(self):
        # libmspub inserts the angle as a plain double, so librevenge stamps
        # it with its default inch unit; the value is degrees.
        gradient = self._style(**{"svg:linearGradient": self.STOPS}).gradient
        self.assertAlmostEqual(gradient.angle, 90.0)

    def test_a_radial_gradient_is_marked_as_one(self):
        gradient = self._style(**{"svg:radialGradient": self.STOPS}).gradient
        self.assertTrue(gradient.radial)

    def test_a_linear_gradient_is_not_radial(self):
        gradient = self._style(**{"svg:linearGradient": self.STOPS}).gradient
        self.assertFalse(gradient.radial)

    def test_the_flat_fill_still_holds_the_first_stop(self):
        # Anything not gradient-aware keeps working exactly as before.
        style = self._style(**{"svg:linearGradient": self.STOPS})
        self.assertEqual(style.fill, (255, 255, 255))

    def test_a_single_stop_is_not_a_gradient(self):
        style = self._style(**{"svg:linearGradient": self.STOPS[:1]})
        self.assertIsNone(style.gradient)
        self.assertEqual(style.fill, (255, 255, 255))
        self.assertTrue(style.approximated_fill)

    def test_a_representable_gradient_is_not_called_approximated(self):
        style = self._style(**{"svg:linearGradient": self.STOPS})
        self.assertFalse(style.approximated_fill)

    def test_stop_colours_reach_the_documents_colour_table(self):
        doc = support.document(
            event("setStyle", {"draw:fill": "gradient", "draw:angle": "0in",
                               "svg:linearGradient": self.STOPS}),
            event(
                "drawRectangle",
                {"svg:x": "1in", "svg:y": "1in", "svg:width": "2in", "svg:height": "1in"},
            ),
        )
        self.assertIn((255, 238, 221), doc.colors)


class GradientOpacityTest(unittest.TestCase):
    """A see-through ramp, which IDML can only state for a whole object."""

    def _stops(self, *opacities):
        return [
            {"svg:offset": f"{i * 50}.0000%", "svg:stop-color": "#a8bad4",
             "svg:stop-opacity": opacity}
            for i, opacity in enumerate(opacities)
        ]

    def _style(self, stops, **extra) -> model.GraphicStyle:
        props = {"draw:fill": "gradient", "draw:angle": "0in",
                 "svg:linearGradient": stops}
        props.update(extra)
        doc = support.document(
            event("setStyle", props),
            event(
                "drawRectangle",
                {"svg:x": "1in", "svg:y": "1in", "svg:width": "2in", "svg:height": "1in"},
            ),
        )
        return doc.pages[0].items[0].style

    def test_each_stops_opacity_is_read(self):
        style = self._style(self._stops("60.0000%", "60.0000%"))
        self.assertEqual([s.opacity for s in style.gradient.stops], [0.6, 0.6])

    def test_a_ramp_that_agrees_becomes_the_objects_opacity(self):
        # Every see-through gradient in the corpus is this case.
        style = self._style(self._stops("60.0000%", "60.0000%"))
        self.assertAlmostEqual(style.fill_opacity, 0.6)
        self.assertFalse(style.uneven_stop_opacity)

    def test_an_opaque_ramp_leaves_the_opacity_alone(self):
        style = self._style(self._stops("100.0000%", "100.0000%"))
        self.assertAlmostEqual(style.fill_opacity, 1.0)

    def test_stops_that_disagree_are_flagged_rather_than_averaged(self):
        style = self._style(self._stops("100.0000%", "60.0000%"))
        self.assertTrue(style.uneven_stop_opacity)
        self.assertAlmostEqual(style.fill_opacity, 1.0)

    def test_the_shapes_own_opacity_still_applies_on_top(self):
        style = self._style(
            self._stops("50.0000%", "50.0000%"), **{"draw:opacity": "50.0000%"}
        )
        self.assertAlmostEqual(style.fill_opacity, 0.25)

    def test_a_single_stop_ramp_still_carries_its_transparency(self):
        # The flat fallback fill should be as see-through as the stop was.
        style = self._style(self._stops("60.0000%"))
        self.assertIsNone(style.gradient)
        self.assertAlmostEqual(style.fill_opacity, 0.6)


class ShadowTest(unittest.TestCase):
    """Publisher's shadow, which libmspub reports in full and we dropped."""

    SHADOW = {
        "draw:shadow": "visible",
        "draw:shadow-color": "#c0c0c0",
        "draw:shadow-offset-x": "0.0278in",
        "draw:shadow-offset-y": "0.0417in",
        "draw:shadow-opacity": "50.0000%",
    }

    def _style(self, **props) -> model.GraphicStyle:
        style = {"draw:fill": "solid", "draw:fill-color": "#ffffff"}
        style.update(props)
        doc = support.document(
            event("setStyle", style),
            event(
                "drawRectangle",
                {"svg:x": "1in", "svg:y": "1in", "svg:width": "2in", "svg:height": "1in"},
            ),
        )
        return doc.pages[0].items[0].style

    def test_colour_offsets_and_opacity_are_all_read(self):
        shadow = self._style(**self.SHADOW).shadow
        self.assertIsNotNone(shadow)
        self.assertEqual(shadow.color, (0xC0, 0xC0, 0xC0))
        self.assertAlmostEqual(shadow.offset_x, 2.0016)
        self.assertAlmostEqual(shadow.offset_y, 3.0024)
        self.assertAlmostEqual(shadow.opacity, 0.5)

    def test_a_shape_with_no_shadow_has_none(self):
        self.assertIsNone(self._style().shadow)

    def test_a_hidden_shadow_is_not_carried(self):
        props = dict(self.SHADOW, **{"draw:shadow": "hidden"})
        self.assertIsNone(self._style(**props).shadow)

    def test_a_shadow_with_no_offset_at_all_is_not_one(self):
        # Publisher would be hiding it exactly behind its own shape.
        props = dict(
            self.SHADOW,
            **{"draw:shadow-offset-x": "0in", "draw:shadow-offset-y": "0in"},
        )
        self.assertIsNone(self._style(**props).shadow)

    def test_a_shadow_without_a_colour_is_not_carried(self):
        props = dict(self.SHADOW)
        del props["draw:shadow-color"]
        self.assertIsNone(self._style(**props).shadow)

    def test_a_negative_offset_survives(self):
        props = dict(self.SHADOW, **{"draw:shadow-offset-x": "-0.0278in"})
        self.assertAlmostEqual(self._style(**props).shadow.offset_x, -2.0016)

    def test_the_shadow_colour_reaches_the_documents_colour_table(self):
        # Or _color_ref falls back and the shadow comes out black.
        doc = support.document(
            event("setStyle", dict({"draw:fill": "solid"}, **self.SHADOW)),
            event(
                "drawRectangle",
                {"svg:x": "1in", "svg:y": "1in", "svg:width": "2in", "svg:height": "1in"},
            ),
        )
        self.assertIn((0xC0, 0xC0, 0xC0), doc.colors)


def table_events(
    columns: list, rows: list, *, x="1in", y="1in", width="5in", height="3in"
) -> list:
    """A table as libmspub reports one.

    `columns` is a list of width strings; `rows` is a list of row specs,
    each (height, cells) where a cell is (text, colspan, rowspan).
    """
    lines = [
        event(
            "startTableObject",
            {
                "svg:x": x, "svg:y": y, "svg:width": width, "svg:height": height,
                "librevenge:table-columns": [
                    {"style:column-width": w} for w in columns
                ],
            },
        )
    ]
    for row_index, (height_value, cells) in enumerate(rows):
        lines.append(event("openTableRow", {"librevenge:row-height": height_value}))
        column = 0
        for text, colspan, rowspan in cells:
            props = {"librevenge:column": str(column), "librevenge:row": str(row_index)}
            if colspan > 1:
                props["table:number-columns-spanned"] = str(colspan)
            if rowspan > 1:
                props["table:number-rows-spanned"] = str(rowspan)
            lines.append(event("openTableCell", props))
            if text is not None:
                lines += [
                    event("openParagraph", {}),
                    event("openSpan", {"fo:font-size": "10pt"}),
                    event("insertText", text=text),
                    event("closeSpan"),
                    event("closeParagraph"),
                ]
            lines.append(event("closeTableCell"))
            for covered in range(1, colspan):
                lines.append(event(
                    "insertCoveredTableCell",
                    {"librevenge:column": str(column + covered),
                     "librevenge:row": str(row_index)},
                ))
            column += colspan
        lines.append(event("closeTableRow"))
    lines.append(event("endTableObject"))
    return lines


class TableTest(unittest.TestCase):
    """Publisher tables, which libmspub describes completely.

    Column widths, row heights, cell coordinates and spans all arrive. They
    used to be thrown away: every cell was flowed into one text frame as
    consecutive paragraphs, so the copy survived and the grid did not.
    """

    def _table(self) -> model.Table:
        doc = support.document(
            *table_events(
                ["2in", "1in"],
                [
                    ("0.5in", [("top left", 1, 1), ("top right", 1, 1)]),
                    ("0.25in", [("spanning", 2, 1)]),
                ],
            )
        )
        return doc.pages[0].items[0]

    def test_a_table_becomes_a_table_not_a_text_frame(self):
        table = self._table()
        self.assertIsInstance(table, model.Table)

    def test_column_widths_are_converted_to_points(self):
        self.assertEqual(self._table().column_widths, [144.0, 72.0])

    def test_row_heights_are_converted_to_points(self):
        self.assertEqual(self._table().row_heights, [36.0, 18.0])

    def test_the_box_is_where_libmspub_put_it(self):
        table = self._table()
        self.assertEqual(
            (table.x, table.y, table.width, table.height), (72.0, 72.0, 360.0, 216.0)
        )

    def test_each_cell_keeps_its_own_text(self):
        cells = {(c.row, c.column): c for c in self._table().cells}
        self.assertEqual(
            [s.text for p in cells[(0, 0)].story.paragraphs for s in p.spans],
            ["top left"],
        )
        self.assertEqual(
            [s.text for p in cells[(0, 1)].story.paragraphs for s in p.spans],
            ["top right"],
        )

    def test_a_span_is_recorded_and_covered_cells_are_not_cells(self):
        table = self._table()
        spanning = next(c for c in table.cells if c.row == 1)
        self.assertEqual(spanning.column_span, 2)
        self.assertEqual(spanning.column, 0)
        # The covered cell must not turn up as a second cell in that row.
        self.assertEqual(len([c for c in table.cells if c.row == 1]), 1)

    def test_row_spans_are_recorded(self):
        doc = support.document(
            *table_events(
                ["1in", "1in"],
                [("0.5in", [("tall", 1, 2), ("beside", 1, 1)]),
                 ("0.5in", [("under", 1, 1)])],
            )
        )
        tall = next(c for c in doc.pages[0].items[0].cells if c.row == 0 and c.column == 0)
        self.assertEqual(tall.row_span, 2)

    def test_an_empty_cell_is_still_a_cell(self):
        doc = support.document(
            *table_events(["1in", "1in"], [("0.5in", [(None, 1, 1), ("x", 1, 1)])])
        )
        table = doc.pages[0].items[0]
        self.assertEqual(len(table.cells), 2)
        self.assertTrue(table.cells[0].story.is_empty())

    def test_flattening_is_no_longer_reported(self):
        doc = support.document(
            *table_events(["1in"], [("0.5in", [("only", 1, 1)])])
        )
        self.assertEqual(
            [w for w in doc.warnings if "flattened" in w], []
        )


class EmptyTableTest(unittest.TestCase):
    """An empty grid contributes nothing, exactly as an empty frame does."""

    def test_a_table_with_no_text_and_no_fill_is_dropped(self):
        doc = support.document(
            *table_events(["1in", "1in"], [("0.5in", [(None, 1, 1), (None, 1, 1)])])
        )
        self.assertEqual(doc.pages[0].items, [])

    def test_a_table_with_any_text_is_kept(self):
        doc = support.document(
            *table_events(["1in", "1in"], [("0.5in", [(None, 1, 1), ("x", 1, 1)])])
        )
        self.assertEqual(len(doc.pages[0].items), 1)

    def test_an_empty_table_with_a_fill_is_kept(self):
        doc = support.document(
            event("setStyle", {"draw:fill": "solid", "draw:fill-color": "#ff0000"}),
            *table_events(["1in"], [("0.5in", [(None, 1, 1)])]),
        )
        self.assertEqual(len(doc.pages[0].items), 1)
