"""IDML output: the artefact must be loadable, and its links must resolve."""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pubidml import convert, idml, model

from . import support
from .support import event


def write_package(document: model.Document, name: str = "doc") -> Path:
    """Write a package into a temp directory and return the .idml path."""
    root = Path(tempfile.mkdtemp())
    destination = root / f"{name}.idml"
    writer = idml.IdmlWriter(document, image_dir_name=f"{destination.stem}_images")
    writer.write(destination)
    return destination


def image_document(mime: str = "image/png") -> model.Document:
    """An image arriving the usual way: a bitmap fill promoted to a picture."""
    return support.document(
        event(
            "setStyle",
            {
                "draw:fill": "bitmap",
                "librevenge:mime-type": mime,
                "draw:fill-image": "aGVsbG8=",  # b"hello"
            },
        ),
        event(
            "drawRectangle",
            {"svg:x": "1in", "svg:y": "1in", "svg:width": "2in", "svg:height": "2in"},
        ),
    )


def graphic_object_document(mime: str) -> model.Document:
    """An image arriving via drawGraphicObject, which performs no mime check.

    _promote_bitmap_fill screens the bitmap-fill route, so this is the only
    way an Image with an unrepresentable format reaches the writer.
    """
    return support.document(
        event(
            "drawGraphicObject",
            {
                "librevenge:mime-type": mime,
                "office:binary-data": "aGVsbG8=",  # b"hello"
                "svg:x": "1in",
                "svg:y": "1in",
                "svg:width": "2in",
                "svg:height": "2in",
            },
        ),
    )


class WellFormednessTest(unittest.TestCase):
    """The tool's whole contract is 'this file opens'. Verify it, every part."""

    def test_every_part_of_a_normal_package_parses(self):
        path = write_package(support.document(*support.text_frame("Hello world")))
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assertEqual(names[0], "mimetype")
            for name in names:
                if not name.endswith(".xml"):
                    continue
                with self.subTest(part=name):
                    ET.fromstring(archive.read(name))

    def test_a_control_character_is_refused_rather_than_written(self):
        # clean_text strips these, so reaching _serialise means something
        # upstream changed. The guard must still refuse to report success.
        element = ET.Element("Content")
        element.text = "a\x0cb"
        with self.assertRaises(idml.MalformedPartError):
            idml._serialise(element)

    def test_text_that_looks_like_markup_is_escaped_not_injected(self):
        document = support.document(
            *support.text_frame("</Content><Injected/>& \"quoted\" <b>")
        )
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            story = next(n for n in archive.namelist() if n.startswith("Stories/"))
            root = ET.fromstring(archive.read(story))
        contents = [e.text for e in root.iter("Content")]
        self.assertEqual(contents, ["</Content><Injected/>& \"quoted\" <b>"])
        self.assertEqual(len(list(root.iter("Injected"))), 0)

    def test_a_hostile_font_name_cannot_escape_its_attribute(self):
        document = support.build(
            *(
                support.page()
                + [
                    event("startTextObject", {"svg:width": "3in", "svg:height": "2in"}),
                    event("openParagraph", {}),
                    event("openSpan", {"style:font-name": '" onload="x'}),
                    event("insertText", text="hi"),
                    event("closeSpan"),
                    event("closeParagraph"),
                    event("endTextObject"),
                    event("endPage"),
                    event("endDocument"),
                ]
            )
        )
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            fonts = ET.fromstring(archive.read("Resources/Fonts.xml"))
        families = [e.get("Name") for e in fonts.iter("FontFamily")]
        self.assertEqual(families, ['" onload="x'])


class LinkResourceUriTest(unittest.TestCase):
    """A link that does not resolve loses the artwork silently."""

    def _uri_for(self, stem: str) -> str:
        path = write_package(image_document(), name=stem)
        with zipfile.ZipFile(path) as archive:
            spread = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            root = ET.fromstring(archive.read(spread))
        return next(root.iter("Link")).get("LinkResourceURI")

    def test_awkward_filenames_still_resolve_to_the_file_on_disk(self):
        # '#' would truncate the URI at the fragment and '%' is an invalid
        # escape; both are ordinary characters in a real archive.
        for stem in (
            "Newsletter #3",
            "Sale 50% off",
            "Blank Note Card (100_1502 Snail) (2 up)",
            "Zomerbrief 2024",
            "Cantico_dei_Cantici",
        ):
            with self.subTest(stem=stem):
                uri = self._uri_for(stem)
                self.assertTrue(uri.startswith("file:"), uri)
                path = unquote(urlsplit(uri).path)
                self.assertEqual(path, f"{stem}_images/image1.png")

    def test_the_uri_points_at_a_file_that_was_actually_written(self):
        for stem in ("Newsletter #3", "plain"):
            with self.subTest(stem=stem):
                idml_path = write_package(image_document(), name=stem)
                with zipfile.ZipFile(idml_path) as archive:
                    spread = next(
                        n for n in archive.namelist() if n.startswith("Spreads/")
                    )
                    root = ET.fromstring(archive.read(spread))
                uri = next(root.iter("Link")).get("LinkResourceURI")
                target = idml_path.parent / unquote(urlsplit(uri).path)
                self.assertTrue(target.exists(), f"{uri} -> {target}")
                self.assertEqual(target.read_bytes(), b"hello")


class ImageTypeTest(unittest.TestCase):
    def test_a_format_idml_cannot_carry_is_dropped_and_reported(self):
        # Previously this was written out as an .emf labelled "$ID/JPEG":
        # the package opened, the artwork was silently absent, ok reported.
        #
        # The vehicle is deliberately not a metafile. Those are screened at
        # parse time now -- an empty one is dropped as the placeholder stub
        # it is -- so a metafile never reaches the writer and would test
        # nothing here.
        document = graphic_object_document(mime="image/svg+xml")
        self.assertEqual(
            len([i for i in document.pages[0].items if isinstance(i, model.Image)]),
            1,
            "test must actually put an unrepresentable Image in front of the writer",
        )
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            spread = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            root = ET.fromstring(archive.read(spread))
        self.assertEqual(len(list(root.iter("Image"))), 0)
        self.assertTrue(
            any("no IDML equivalent" in w for w in document.warnings), document.warnings
        )

    def test_a_supported_format_is_labelled_correctly(self):
        for mime, expected in (
            ("image/png", "$ID/PNG"),
            ("image/jpeg", "$ID/JPEG"),
            ("image/gif", "$ID/GIF"),
        ):
            with self.subTest(mime=mime):
                path = write_package(image_document(mime=mime))
                with zipfile.ZipFile(path) as archive:
                    spread = next(
                        n for n in archive.namelist() if n.startswith("Spreads/")
                    )
                    root = ET.fromstring(archive.read(spread))
                image = next(root.iter("Image"))
                self.assertEqual(image.get("ImageTypeName"), expected)


class ContentTransformTest(unittest.TestCase):
    """A picture must land on the frame that holds it, at any rotation.

    IDML clips content to its frame, so a picture placed outside its own
    frame does not look wrong -- it looks absent. Every rotated image in
    the sample set was 100% outside, which is why a whole document read as
    having no pictures at all.
    """

    @staticmethod
    def content_box(transform: str, width: float, height: float):
        a, b, c, d, tx, ty = (float(v) for v in transform.split())
        corners = [
            (a * x + c * y + tx, b * x + d * y + ty)
            for x, y in ((0, 0), (width, 0), (0, height), (width, height))
        ]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        return min(xs), max(xs), min(ys), max(ys)

    def test_content_is_centred_on_the_frame_at_every_rotation(self):
        width, height = 267.2, 360.0
        for rotation in (0, 90, -90, 180, 270, -46, 45):
            with self.subTest(rotation=rotation):
                x0, x1, y0, y1 = self.content_box(
                    idml._content_matrix(rotation, width, height), width, height
                )
                # fmt rounds to six decimals, so the matrix components carry
                # a little rounding into the corner arithmetic. A thousandth
                # of a point is ~350 nanometres; the concern is placement,
                # not float exactness.
                self.assertAlmostEqual((x0 + x1) / 2, 0.0, places=3)
                self.assertAlmostEqual((y0 + y1) / 2, 0.0, places=3)

    def test_a_rotated_picture_covers_the_whole_frame(self):
        # Not merely overlaps: a quarter-turned picture placed at the frame's
        # own dimensions leaves the frame only 74% covered and clips the rest.
        width, height = 267.2, 360.0
        for rotation in (0, 90, -90, 180, 270, -270):
            with self.subTest(rotation=rotation):
                placed_w, placed_h = idml._content_bounds(rotation, width, height)
                x0, x1, y0, y1 = self.content_box(
                    idml._content_matrix(rotation, placed_w, placed_h),
                    placed_w,
                    placed_h,
                )
                self.assertLessEqual(x0, -width / 2 + 0.001, "frame not covered on the left")
                self.assertGreaterEqual(x1, width / 2 - 0.001, "frame not covered on the right")
                self.assertLessEqual(y0, -height / 2 + 0.001, "frame not covered on top")
                self.assertGreaterEqual(y1, height / 2 - 0.001, "frame not covered at the bottom")

    def test_a_quarter_turn_preserves_the_pictures_aspect_ratio(self):
        # The decisive evidence: a 4000x3000 photo placed into portrait
        # bounds and then turned is squashed. Placed bounds must carry the
        # frame's aspect inverted, so the turn restores it.
        width, height = 267.2, 360.0
        placed_w, placed_h = idml._content_bounds(-90, width, height)
        self.assertAlmostEqual(placed_w / placed_h, height / width, places=6)
        unrotated_w, unrotated_h = idml._content_bounds(0, width, height)
        self.assertAlmostEqual(unrotated_w / unrotated_h, width / height, places=6)

    def test_only_quarter_turns_swap_the_axes(self):
        for rotation in (0, 180, -180, 360, 10, 44):
            with self.subTest(rotation=rotation):
                self.assertEqual(idml._content_bounds(rotation, 200.0, 100.0), (200.0, 100.0))
        for rotation in (90, -90, 270, 100):
            with self.subTest(rotation=rotation):
                self.assertEqual(idml._content_bounds(rotation, 200.0, 100.0), (100.0, 200.0))

    def test_an_unrotated_picture_fills_its_frame_exactly(self):
        width, height = 200.0, 100.0
        x0, x1, y0, y1 = self.content_box(
            idml._content_matrix(0, width, height), width, height
        )
        self.assertAlmostEqual(x0, -width / 2)
        self.assertAlmostEqual(x1, width / 2)
        self.assertAlmostEqual(y0, -height / 2)
        self.assertAlmostEqual(y1, height / 2)

    def test_zero_rotation_output_is_unchanged(self):
        # Fourteen of the eighteen sample images are unrotated and were
        # already correct; this must stay byte-identical for them.
        self.assertEqual(idml._content_matrix(0, 200.0, 100.0), "1 0 0 1 -100 -50")

    def test_every_image_in_a_built_package_overlaps_its_frame(self):
        document = graphic_object_document("image/png")
        document.pages[0].items[0].content_rotation = -90.0
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            spread = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            root = ET.fromstring(archive.read(spread))
        image = next(root.iter("Image"))
        bounds = image.find("Properties/GraphicBounds")
        placed_w = float(bounds.get("Right"))
        placed_h = float(bounds.get("Bottom"))
        x0, x1, y0, y1 = self.content_box(image.get("ItemTransform"), placed_w, placed_h)
        # The frame is the Rectangle's own drawn outline, not the placed size.
        geometry = next(root.iter("PathGeometry"))
        anchors = [p.get("Anchor").split() for p in geometry.iter("PathPointType")]
        frame_w = max(float(a[0]) for a in anchors) - min(float(a[0]) for a in anchors)
        frame_h = max(float(a[1]) for a in anchors) - min(float(a[1]) for a in anchors)
        self.assertLessEqual(x0, -frame_w / 2 + 0.001)
        self.assertGreaterEqual(x1, frame_w / 2 - 0.001)
        self.assertLessEqual(y0, -frame_h / 2 + 0.001)
        self.assertGreaterEqual(y1, frame_h / 2 - 0.001)


class StructureTest(unittest.TestCase):
    def test_zip_entry_names_are_fixed_and_cannot_traverse(self):
        path = write_package(image_document(), name="../escape")
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                self.assertNotIn("..", name)
                self.assertFalse(name.startswith("/"))

    def test_a_document_with_no_text_still_produces_a_valid_package(self):
        document = support.document(
            event(
                "drawRectangle",
                {"svg:x": "0in", "svg:y": "0in", "svg:width": "1in", "svg:height": "1in"},
            )
        )
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".xml"):
                    ET.fromstring(archive.read(name))

    def test_designmap_lists_every_spread_and_story_part(self):
        document = support.document(*support.text_frame("one"), *support.text_frame("two"))
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            designmap = ET.fromstring(archive.read("designmap.xml"))
        referenced = {
            e.get("src") for e in designmap if e.get("src") is not None
        }
        self.assertTrue(referenced)
        for src in referenced:
            self.assertIn(src, names, f"designmap references missing part {src}")


class FacingPagesTest(unittest.TestCase):
    """A booklet must arrive as spreads, not as a stack of single pages.

    libmspub reads only DOCUMENT_WIDTH and DOCUMENT_HEIGHT, so nothing in
    the event stream says whether the publication was set up facing. It has
    to be asked for.
    """

    def _package(self, pages: int, facing: bool):
        document = model.Document()
        for _ in range(pages):
            page = model.Page(width=400.0, height=600.0)
            page.items.append(model.Rectangle(x=10.0, y=20.0, width=30.0, height=40.0))
            document.pages.append(page)

        root = Path(tempfile.mkdtemp())
        destination = root / "doc.idml"
        idml.IdmlWriter(
            document, image_dir_name="doc_images", facing_pages=facing
        ).write(destination)
        return destination

    @staticmethod
    def _spreads(archive) -> list:
        return [
            ET.fromstring(archive.read(n))
            for n in sorted(n for n in archive.namelist() if n.startswith("Spreads/"))
        ]

    def test_single_pages_stay_one_per_spread_by_default(self):
        with zipfile.ZipFile(self._package(5, facing=False)) as archive:
            spreads = self._spreads(archive)
            counts = [next(s.iter("Spread")).get("PageCount") for s in spreads]
            preferences = ET.fromstring(archive.read("Resources/Preferences.xml"))
        self.assertEqual(counts, ["1"] * 5)
        self.assertEqual(
            next(preferences.iter("DocumentPreferences")).get("FacingPages"), "false"
        )

    def test_facing_pages_puts_the_cover_alone_then_pairs_the_rest(self):
        # 1 | 2 3 | 4 5 -- the cover is a recto with nothing facing it.
        with zipfile.ZipFile(self._package(5, facing=True)) as archive:
            spreads = self._spreads(archive)
            counts = [next(s.iter("Spread")).get("PageCount") for s in spreads]
            names = [[p.get("Name") for p in s.iter("Page")] for s in spreads]
        self.assertEqual(counts, ["1", "2", "2"])
        self.assertEqual(names, [["1"], ["2", "3"], ["4", "5"]])

    def test_the_preference_is_declared_so_the_reader_agrees(self):
        with zipfile.ZipFile(self._package(4, facing=True)) as archive:
            preferences = ET.fromstring(archive.read("Resources/Preferences.xml"))
        self.assertEqual(
            next(preferences.iter("DocumentPreferences")).get("FacingPages"), "true"
        )

    def test_an_even_page_sits_left_of_the_spine_and_an_odd_page_right(self):
        with zipfile.ZipFile(self._package(3, facing=True)) as archive:
            pages = [p for s in self._spreads(archive) for p in s.iter("Page")]
        offsets = [p.get("ItemTransform").split()[4] for p in pages]
        # page 1 recto, page 2 verso (a full page width left), page 3 recto
        self.assertEqual(offsets, ["0", "-400", "0"])

    def test_items_follow_their_page_across_the_spine(self):
        with zipfile.ZipFile(self._package(3, facing=True)) as archive:
            spreads = self._spreads(archive)
        # Page 2 is the verso of spread 2; its rectangle must be a page
        # width further left than the identically placed one on page 3.
        verso, recto = [
            float(r.get("ItemTransform").split()[4])
            for r in spreads[1].iter("Rectangle")
        ]
        self.assertAlmostEqual(recto - verso, 400.0)

    def test_geometry_without_facing_pages_is_untouched(self):
        with zipfile.ZipFile(self._package(2, facing=False)) as archive:
            spreads = self._spreads(archive)
        for spread in spreads:
            rectangle = next(spread.iter("Rectangle"))
            # centred on the page, which is centred on the spread origin
            self.assertAlmostEqual(
                float(rectangle.get("ItemTransform").split()[4]),
                10.0 + 15.0 - 200.0,
            )

    def test_a_facing_package_is_still_well_formed_and_fully_referenced(self):
        path = self._package(5, facing=True)
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for name in names:
                if name.endswith(".xml"):
                    ET.fromstring(archive.read(name))
            designmap = ET.fromstring(archive.read("designmap.xml"))
        referenced = {e.get("src") for e in designmap if e.get("src")}
        for src in referenced:
            self.assertIn(src, names)


class LeadingTest(unittest.TestCase):
    """Publisher's line spacing has to reach IDML as leading.

    It was parsed into the model and then read by nobody, so every
    document arrived on Affinity's default leading regardless of what
    Publisher said.
    """

    def _story(self, paragraph_props: dict, *spans: dict) -> ET.Element:
        lines = [
            event("startTextObject", {"svg:width": "6in", "svg:height": "4in"}),
            event("openParagraph", paragraph_props),
        ]
        for span in spans:
            lines += [
                event("openSpan", span),
                event("insertText", text="some text"),
                event("closeSpan"),
            ]
        lines += [event("closeParagraph"), event("endTextObject")]

        path = write_package(support.document(*lines))
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.startswith("Stories/"))
            return ET.fromstring(archive.read(name))

    @staticmethod
    def _leadings(story: ET.Element) -> list:
        return [
            (e.get("type"), e.text)
            for e in story.iter("Leading")
        ]

    def test_a_multiple_of_single_spacing_becomes_absolute_leading(self):
        # 0.9 spaces on 10pt type. Single spacing is IDML's own auto-leading
        # default of 120%, so 0.9 x 1.2 x 10 = 10.8pt.
        story = self._story({"fo:line-height": "90.0000%"}, {"fo:font-size": "10pt"})
        self.assertEqual(self._leadings(story), [("unit", "10.8")])

    def test_an_exact_point_value_is_passed_straight_through(self):
        story = self._story({"fo:line-height": "10.5000pt"}, {"fo:font-size": "10pt"})
        self.assertEqual(self._leadings(story), [("unit", "10.5")])

    def test_single_spacing_is_left_to_the_reader_default(self):
        # libmspub omits the property at 1 sp, and IDML's Auto leading is
        # 120% -- single spacing. Writing nothing is therefore correct, and
        # keeps the leading proportional if the type size is changed later.
        story = self._story({}, {"fo:font-size": "10pt"})
        self.assertEqual(self._leadings(story), [])

    def test_wider_spacing_scales_with_the_type_size(self):
        story = self._story({"fo:line-height": "150.0000%"}, {"fo:font-size": "10pt"})
        self.assertEqual(self._leadings(story), [("unit", "18")])

    def test_each_run_gets_the_leading_of_its_own_type_size(self):
        # Leading is a character property in IDML, and a paragraph can mix
        # sizes; InDesign then uses the largest on the line.
        story = self._story(
            {"fo:line-height": "90.0000%"},
            {"fo:font-size": "10pt"},
            {"fo:font-size": "20pt"},
        )
        self.assertEqual(
            self._leadings(story), [("unit", "10.8"), ("unit", "21.6")]
        )

    def test_a_run_with_no_size_uses_the_idml_default_of_twelve_points(self):
        story = self._story({"fo:line-height": "90.0000%"}, {})
        self.assertEqual(self._leadings(story), [("unit", "12.96")])

    def test_leading_and_a_font_share_one_properties_element(self):
        story = self._story(
            {"fo:line-height": "90.0000%"},
            {"fo:font-size": "10pt", "style:font-name": "Arial"},
        )
        properties = list(story.iter("Properties"))
        self.assertEqual(len(properties), 1)
        tags = [child.tag for child in properties[0]]
        self.assertIn("Leading", tags)
        self.assertIn("AppliedFont", tags)


class TextColumnTest(unittest.TestCase):
    """Column count and gutter have to survive into TextFramePreference."""

    def _preference(self, **props) -> dict:
        base = {"svg:x": "1in", "svg:y": "1in", "svg:width": "6in", "svg:height": "4in"}
        base.update(props)
        document = support.document(
            event("startTextObject", base),
            event("openParagraph", {}),
            event("openSpan", {"style:font-name": "Arial", "fo:font-size": "10pt"}),
            event("insertText", text="text"),
            event("closeSpan"),
            event("closeParagraph"),
            event("endTextObject"),
        )
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            spread = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            root = ET.fromstring(archive.read(spread))
        return dict(next(root.iter("TextFramePreference")).attrib)

    def test_the_column_count_is_written(self):
        preference = self._preference(**{"fo:column-count": "3"})
        self.assertEqual(preference["TextColumnCount"], "3")

    def test_the_gutter_is_written_whenever_there_are_columns(self):
        # It has to be explicit: InDesign's own default gutter is 12pt,
        # against Publisher's 2mm, so leaving it out would silently widen
        # the gaps and narrow every column.
        preference = self._preference(
            **{"fo:column-count": "2", "fo:column-gap": "0.0787in"}
        )
        self.assertAlmostEqual(
            float(preference["TextColumnGutter"]), 0.0787 * 72.0, places=3
        )

    def test_a_column_count_with_no_gap_still_pins_the_gutter(self):
        preference = self._preference(**{"fo:column-count": "2"})
        self.assertEqual(float(preference["TextColumnGutter"]), 0.0)

    def test_a_single_column_frame_is_written_exactly_as_before(self):
        preference = self._preference(**{"fo:column-gap": "0.0787in"})
        self.assertEqual(preference["TextColumnCount"], "1")
        self.assertNotIn("TextColumnGutter", preference)


class ThreadedStoryTest(unittest.TestCase):
    """A threaded story must be written once and flowed through its frames."""

    LONG = "word " * 300

    def _threaded_package(self, count: int = 4):
        document = support.paged_document(
            *[support.text_frame(self.LONG) for _ in range(count)]
        )
        convert._thread_duplicate_stories(document)
        return write_package(document)

    @staticmethod
    def _text_frames(archive) -> list:
        """Every TextFrame in the package, in spread order."""
        frames = []
        for name in sorted(
            n for n in archive.namelist() if n.startswith("Spreads/")
        ):
            spread = ET.fromstring(archive.read(name))
            frames += spread.iter("TextFrame")
        return frames

    def test_the_chain_shares_a_single_story(self):
        with zipfile.ZipFile(self._threaded_package()) as archive:
            frames = self._text_frames(archive)
            stories = {f.get("ParentStory") for f in frames}
        self.assertEqual(len(frames), 4)
        self.assertEqual(len(stories), 1)

    def test_the_text_is_written_exactly_once(self):
        with zipfile.ZipFile(self._threaded_package()) as archive:
            parts = [n for n in archive.namelist() if n.startswith("Stories/")]
            carrying = [
                n for n in parts if b"word" in archive.read(n)
            ]
        self.assertEqual(len(parts), 1)
        self.assertEqual(len(carrying), 1)

    def test_the_frames_are_linked_head_to_tail(self):
        with zipfile.ZipFile(self._threaded_package()) as archive:
            frames = self._text_frames(archive)

        selves = [f.get("Self") for f in frames]
        previous = [f.get("PreviousTextFrame") for f in frames]
        following = [f.get("NextTextFrame") for f in frames]

        self.assertEqual(previous, ["n"] + selves[:-1])
        self.assertEqual(following, selves[1:] + ["n"])

    def test_every_link_reference_resolves_to_a_frame_in_the_package(self):
        with zipfile.ZipFile(self._threaded_package()) as archive:
            frames = self._text_frames(archive)
        known = {f.get("Self") for f in frames}
        for frame in frames:
            for attribute in ("PreviousTextFrame", "NextTextFrame"):
                reference = frame.get(attribute)
                if reference != "n":
                    self.assertIn(reference, known)

    def test_the_designmap_still_lists_the_shared_story(self):
        path = self._threaded_package()
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            designmap = ET.fromstring(archive.read("designmap.xml"))
            story_part = next(n for n in names if n.startswith("Stories/"))
        referenced = {e.get("src") for e in designmap if e.get("src") is not None}
        self.assertIn(story_part, referenced)

    def test_an_unthreaded_document_is_written_as_before(self):
        document = support.paged_document(
            support.text_frame("one"), support.text_frame("two")
        )
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            frames = self._text_frames(archive)
            parts = [n for n in archive.namelist() if n.startswith("Stories/")]
        self.assertEqual(len(parts), 2)
        self.assertEqual({f.get("ParentStory") for f in frames}.__len__(), 2)
        for frame in frames:
            self.assertEqual(frame.get("PreviousTextFrame"), "n")
            self.assertEqual(frame.get("NextTextFrame"), "n")


if __name__ == "__main__":
    unittest.main()
