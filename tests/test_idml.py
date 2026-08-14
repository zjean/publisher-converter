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


class SubpathTest(unittest.TestCase):
    """A path with several subpaths must not be welded into one outline.

    libmspub reports most Publisher paths as disconnected edges -- 50 of
    the 56 paths in the sample corpus have more than one subpath. Joining
    them end to end turned two horizontal rules into a filled bowtie
    stretched across the page, which is worse than drawing nothing.
    """

    # Two horizontal rules, 31.5pt apart, exactly as libmspub reports the
    # ones on page 4 of 1336 kerkbode.pub.
    TWO_RULES = [
        ("M", 172.4, 292.1), ("L", 342.9, 292.1), ("Z",), ("Z",),
        ("M", 174.4, 323.6), ("L", 345.0, 323.6), ("Z",),
    ]

    def _geometry(self, ops: list, **style) -> ET.Element:
        document = model.Document(pages=[model.Page(width=400.0, height=600.0)])
        xs = [op[1] for op in ops if len(op) > 1]
        ys = [op[2] for op in ops if len(op) > 2]
        document.pages[0].items.append(
            model.Path(
                x=min(xs), y=min(ys),
                width=max(xs) - min(xs), height=max(ys) - min(ys),
                ops=ops, style=model.GraphicStyle(**style),
            )
        )
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            return ET.fromstring(archive.read(name))

    def test_each_subpath_gets_its_own_geometry(self):
        spread = self._geometry(self.TWO_RULES, fill=(0, 0, 0))
        self.assertEqual(len(list(spread.iter("GeometryPathType"))), 2)

    def test_the_subpaths_are_not_welded_into_one_outline(self):
        spread = self._geometry(self.TWO_RULES, fill=(0, 0, 0))
        for path in spread.iter("GeometryPathType"):
            points = list(path.iter("PathPointType"))
            self.assertEqual(len(points), 2, "a rule has two ends, not four")

    def test_the_second_rule_keeps_its_own_vertical_position(self):
        # Welding put the second rule's start where the first one's end
        # belonged, which is precisely what crossed the outline over.
        spread = self._geometry(self.TWO_RULES, fill=(0, 0, 0))
        rules = [
            [p.get("Anchor").split()[1] for p in path.iter("PathPointType")]
            for path in spread.iter("GeometryPathType")
        ]
        for anchors in rules:
            self.assertEqual(len(set(anchors)), 1, "a horizontal rule is level")
        self.assertNotEqual(rules[0][0], rules[1][0])

    def test_a_single_subpath_is_written_exactly_as_before(self):
        ops = [("M", 10.0, 10.0), ("L", 40.0, 10.0), ("L", 40.0, 30.0), ("Z",)]
        spread = self._geometry(ops, fill=(1, 2, 3))
        paths = list(spread.iter("GeometryPathType"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(list(paths[0].iter("PathPointType"))), 3)
        self.assertEqual(paths[0].get("PathOpen"), "false")

    def test_an_open_subpath_stays_open(self):
        ops = [("M", 10.0, 10.0), ("L", 40.0, 10.0)]
        spread = self._geometry(ops, stroke=(0, 0, 0))
        self.assertEqual(
            next(spread.iter("GeometryPathType")).get("PathOpen"), "true"
        )

    def test_curves_keep_their_handles_within_a_subpath(self):
        ops = [
            ("M", 0.0, 0.0),
            ("C", 10.0, 0.0, 20.0, 10.0, 20.0, 20.0),
            ("Z",),
            ("M", 40.0, 40.0),
            ("L", 60.0, 40.0),
        ]
        spread = self._geometry(ops, fill=(0, 0, 0))
        paths = list(spread.iter("GeometryPathType"))
        self.assertEqual(len(paths), 2)
        first = list(paths[0].iter("PathPointType"))
        self.assertEqual(len(first), 2)
        # The curve's control point must not collapse onto the anchor.
        self.assertNotEqual(first[0].get("RightDirection"), first[0].get("Anchor"))


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


class GradientFillTest(unittest.TestCase):
    """A gradient has to become a real IDML gradient resource.

    Collapsing it to its first stop is how the white-to-cream panels in the
    sample corpus vanished: the ramp starts white.
    """

    @staticmethod
    def _document(*gradients) -> model.Document:
        document = model.Document(pages=[model.Page(width=400.0, height=600.0)])
        for gradient in gradients:
            document.pages[0].items.append(
                model.Rectangle(
                    x=10.0, y=10.0, width=100.0, height=50.0,
                    style=model.GraphicStyle(
                        fill=gradient.stops[0].color, gradient=gradient
                    ),
                )
            )
        return document

    @staticmethod
    def _ramp(*colours, angle=0.0, radial=False) -> model.Gradient:
        step = 100.0 / max(1, len(colours) - 1)
        return model.Gradient(
            stops=tuple(
                model.GradientStop(location=index * step, color=colour)
                for index, colour in enumerate(colours)
            ),
            angle=angle,
            radial=radial,
        )

    def _parts(self, document: model.Document):
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            graphic = ET.fromstring(archive.read("Resources/Graphic.xml"))
            spread = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            return graphic, ET.fromstring(archive.read(spread))

    def test_the_ramp_becomes_a_gradient_resource(self):
        graphic, _ = self._parts(
            self._document(self._ramp((255, 255, 255), (255, 238, 221), (255, 255, 255)))
        )
        gradients = list(graphic.iter("Gradient"))
        self.assertEqual(len(gradients), 1)
        stops = list(gradients[0].iter("GradientStop"))
        self.assertEqual(len(stops), 3)
        self.assertEqual(
            [s.get("StopColor") for s in stops],
            ["Color/C_FFFFFF", "Color/C_FFEEDD", "Color/C_FFFFFF"],
        )
        self.assertEqual([s.get("Location") for s in stops], ["0", "50", "100"])

    def test_the_shape_fills_with_the_gradient_not_a_flat_colour(self):
        _, spread = self._parts(
            self._document(self._ramp((255, 255, 255), (255, 238, 221)))
        )
        fill = next(spread.iter("Rectangle")).get("FillColor")
        self.assertTrue(fill.startswith("Gradient/"), fill)

    def test_the_angle_is_carried(self):
        _, spread = self._parts(
            self._document(self._ramp((0, 0, 0), (255, 255, 255), angle=90.0))
        )
        self.assertEqual(next(spread.iter("Rectangle")).get("GradientFillAngle"), "90")

    def test_a_radial_ramp_is_typed_radial(self):
        graphic, _ = self._parts(
            self._document(self._ramp((0, 0, 0), (255, 255, 255), radial=True))
        )
        self.assertEqual(next(graphic.iter("Gradient")).get("Type"), "Radial")

    def test_a_linear_ramp_is_typed_linear(self):
        graphic, _ = self._parts(
            self._document(self._ramp((0, 0, 0), (255, 255, 255)))
        )
        self.assertEqual(next(graphic.iter("Gradient")).get("Type"), "Linear")

    def test_two_shapes_with_the_same_ramp_share_one_resource(self):
        ramp = self._ramp((0, 0, 0), (255, 255, 255))
        graphic, spread = self._parts(self._document(ramp, ramp))
        self.assertEqual(len(list(graphic.iter("Gradient"))), 1)
        fills = {r.get("FillColor") for r in spread.iter("Rectangle")}
        self.assertEqual(len(fills), 1)

    def test_different_ramps_get_their_own_resources(self):
        graphic, _ = self._parts(
            self._document(
                self._ramp((0, 0, 0), (255, 255, 255)),
                self._ramp((255, 0, 0), (0, 0, 255)),
            )
        )
        self.assertEqual(len(list(graphic.iter("Gradient"))), 2)

    def test_every_stop_colour_exists_as_a_swatch(self):
        graphic, _ = self._parts(
            self._document(self._ramp((255, 255, 255), (255, 238, 221)))
        )
        colours = {c.get("Self") for c in graphic.iter("Color")}
        for stop in graphic.iter("GradientStop"):
            self.assertIn(stop.get("StopColor"), colours)

    def test_a_flat_fill_is_written_exactly_as_before(self):
        document = model.Document(pages=[model.Page(width=400.0, height=600.0)])
        document.pages[0].items.append(
            model.Rectangle(
                x=10.0, y=10.0, width=100.0, height=50.0,
                style=model.GraphicStyle(fill=(1, 2, 3)),
            )
        )
        graphic, spread = self._parts(document)
        self.assertEqual(len(list(graphic.iter("Gradient"))), 0)
        rectangle = next(spread.iter("Rectangle"))
        self.assertEqual(rectangle.get("FillColor"), "Color/C_010203")
        self.assertIsNone(rectangle.get("GradientFillAngle"))

    def _partial(self, first: float, last: float) -> model.Gradient:
        """A ramp occupying only part of its range, as Publisher's do."""
        return model.Gradient(
            stops=(
                model.GradientStop(location=first, color=(168, 186, 212)),
                model.GradientStop(location=last, color=(223, 230, 239)),
            )
        )

    def test_a_partial_ramp_is_given_both_its_ends(self):
        # Publisher holds the end colours outside the ramp. Left unstated,
        # a reader is equally entitled to stretch the ramp over the shape,
        # which looks nothing like it.
        graphic, _ = self._parts(self._document(self._partial(32.0, 49.0)))
        stops = list(graphic.iter("GradientStop"))
        self.assertEqual(
            [s.get("Location") for s in stops], ["0", "32", "49", "100"]
        )
        self.assertEqual(
            [s.get("StopColor") for s in stops],
            ["Color/C_A8BAD4", "Color/C_A8BAD4", "Color/C_DFE6EF", "Color/C_DFE6EF"],
        )

    def test_a_ramp_that_already_spans_the_range_is_left_alone(self):
        graphic, _ = self._parts(self._document(self._partial(0.0, 100.0)))
        stops = list(graphic.iter("GradientStop"))
        self.assertEqual([s.get("Location") for s in stops], ["0", "100"])

    def test_padding_keeps_the_repeated_offsets_that_make_a_hard_edge(self):
        ramp = model.Gradient(
            stops=(
                model.GradientStop(location=10.0, color=(255, 255, 255)),
                model.GradientStop(location=50.0, color=(255, 255, 255)),
                model.GradientStop(location=50.0, color=(0, 0, 0)),
            )
        )
        graphic, _ = self._parts(self._document(ramp))
        self.assertEqual(
            [s.get("Location") for s in graphic.iter("GradientStop")],
            ["0", "10", "50", "50", "100"],
        )

    def test_an_angle_outside_a_half_turn_is_folded_into_range(self):
        # The corpus reports -225, which IDML states as its equal, 135.
        style = model.GraphicStyle.from_props({
            "draw:fill": "gradient",
            "draw:angle": "-225.0000in",
            "svg:linearGradient": [
                {"svg:offset": "0%", "svg:stop-color": "#000000"},
                {"svg:offset": "100%", "svg:stop-color": "#ffffff"},
            ],
        })
        self.assertAlmostEqual(style.gradient.angle, 135.0)


class TextGradientTest(unittest.TestCase):
    """A recovered WordArt headline is painted the way a shape is.

    Its ramp lives on the run rather than on a frame, which is the one
    place in the document where text carries more than a flat colour.
    """

    RAMP = model.Gradient(
        stops=(
            model.GradientStop(location=0.0, color=(145, 56, 1)),
            model.GradientStop(location=100.0, color=(255, 209, 125)),
        ),
        angle=45.0,
    )

    def _parts(self, span: model.Span):
        document = model.Document(pages=[model.Page(width=400.0, height=600.0)])
        frame = model.TextFrame(x=10.0, y=10.0, width=200.0, height=40.0)
        paragraph = model.Paragraph()
        paragraph.spans.append(span)
        frame.story.paragraphs.append(paragraph)
        document.pages[0].items.append(frame)
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            graphic = ET.fromstring(archive.read("Resources/Graphic.xml"))
            story = next(n for n in archive.namelist() if n.startswith("Stories/"))
            return graphic, ET.fromstring(archive.read(story))

    def test_the_runs_ramp_becomes_a_gradient_resource(self):
        graphic, _ = self._parts(
            model.Span(text="Kerkbode", color=(145, 56, 1), gradient=self.RAMP)
        )
        self.assertEqual(len(list(graphic.iter("Gradient"))), 1)

    def test_the_run_fills_with_the_gradient_not_its_first_stop(self):
        _, story = self._parts(
            model.Span(text="Kerkbode", color=(145, 56, 1), gradient=self.RAMP)
        )
        run = next(story.iter("CharacterStyleRange"))
        self.assertEqual(run.get("FillColor"), "Gradient/G_1")
        self.assertEqual(run.get("GradientFillAngle"), "45")

    def test_every_stop_colour_reaches_the_swatches(self):
        graphic, _ = self._parts(
            model.Span(text="Kerkbode", color=(145, 56, 1), gradient=self.RAMP)
        )
        colours = {c.get("Self") for c in graphic.iter("Color")}
        self.assertIn("Color/C_FFD17D", colours)      # the end that was lost
        for stop in graphic.iter("GradientStop"):
            self.assertIn(stop.get("StopColor"), colours)

    def test_the_ramp_is_given_the_distance_to_run_over(self):
        # Without a length IDML ramps over nothing: everything before the
        # start point takes the first stop and everything after it the
        # last, so the masthead came out brown for its left half and gold
        # for its right, with a hard edge between them.
        flat = model.Gradient(stops=self.RAMP.stops, angle=0.0)
        _, story = self._parts(
            model.Span(text="Kerkbode", color=(145, 56, 1), gradient=flat)
        )
        run = next(story.iter("CharacterStyleRange"))
        # The frame is 200 x 40, and the ramp is horizontal, so it starts
        # at the left edge and runs the full width.
        self.assertEqual(run.get("GradientFillLength"), "200")
        self.assertEqual(run.get("GradientFillStart"), "-100 0")

    def test_the_distance_follows_the_angle(self):
        upright = model.Gradient(stops=self.RAMP.stops, angle=90.0)
        _, story = self._parts(
            model.Span(text="Kerkbode", color=(145, 56, 1), gradient=upright)
        )
        run = next(story.iter("CharacterStyleRange"))
        # Turned a quarter, the ramp runs the frame's height instead.
        self.assertEqual(run.get("GradientFillLength"), "40")
        self.assertEqual(run.get("GradientFillStart"), "0 20")

    def test_a_run_with_no_ramp_states_no_geometry(self):
        _, story = self._parts(model.Span(text="body", color=(0, 0, 0)))
        run = next(story.iter("CharacterStyleRange"))
        self.assertIsNone(run.get("GradientFillLength"))
        self.assertIsNone(run.get("GradientFillStart"))

    def test_an_outline_on_the_glyphs_is_written_as_a_stroke(self):
        _, story = self._parts(
            model.Span(
                text="Kerkbode", color=(145, 56, 1), gradient=self.RAMP,
                stroke=(54, 27, 0), stroke_width=0.75,
            )
        )
        run = next(story.iter("CharacterStyleRange"))
        self.assertEqual(run.get("StrokeColor"), "Color/C_361B00")
        self.assertEqual(run.get("StrokeWeight"), "0.75")

    def test_ordinary_text_is_written_exactly_as_before(self):
        graphic, story = self._parts(model.Span(text="body", color=(0, 0, 0)))
        self.assertEqual(len(list(graphic.iter("Gradient"))), 0)
        run = next(story.iter("CharacterStyleRange"))
        self.assertEqual(run.get("FillColor"), "Color/C_000000")
        self.assertIsNone(run.get("GradientFillAngle"))
        self.assertIsNone(run.get("StrokeColor"))

    def test_a_shape_and_a_run_sharing_a_ramp_share_one_resource(self):
        document = model.Document(pages=[model.Page(width=400.0, height=600.0)])
        document.pages[0].items.append(
            model.Rectangle(
                x=10.0, y=10.0, width=100.0, height=50.0,
                style=model.GraphicStyle(fill=(145, 56, 1), gradient=self.RAMP),
            )
        )
        frame = model.TextFrame(x=10.0, y=100.0, width=200.0, height=40.0)
        paragraph = model.Paragraph()
        paragraph.spans.append(
            model.Span(text="Kerkbode", color=(145, 56, 1), gradient=self.RAMP)
        )
        frame.story.paragraphs.append(paragraph)
        document.pages[0].items.append(frame)
        with zipfile.ZipFile(write_package(document)) as archive:
            graphic = ET.fromstring(archive.read("Resources/Graphic.xml"))
        self.assertEqual(len(list(graphic.iter("Gradient"))), 1)


class TablePlacementTest(unittest.TestCase):
    """A table starts at the top-left of the frame that holds it."""

    def _frame(self):
        document = model.Document(pages=[model.Page(width=600.0, height=800.0)])
        table = model.Table(
            x=20.0, y=30.0, width=300.0, height=100.0,
            column_widths=[150.0, 150.0], row_heights=[50.0, 50.0],
        )
        table.cells.append(model.TableCell(row=0, column=0))
        document.pages[0].items.append(table)
        with zipfile.ZipFile(write_package(document)) as archive:
            spread = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            return next(ET.fromstring(archive.read(spread)).iter("TextFrame"))

    def test_the_first_baseline_is_left_to_the_reader(self):
        # Pinning it reads like the right thing for a table, which has no
        # baseline to offset. Affinity answers a fixed height of zero by
        # lifting the table a whole frame height off its position, so the
        # attribute stays off the frame -- see
        # research/probe_table_placement.py, which measured it.
        preference = next(self._frame().iter("TextFramePreference"))
        self.assertIsNone(preference.get("FirstBaselineOffset"))
        self.assertIsNone(preference.get("MinimumFirstBaselineOffset"))

    def test_the_frame_still_carries_no_inset(self):
        preference = next(self._frame().iter("TextFramePreference"))
        self.assertEqual(preference.get("InsetSpacing"), "0 0 0 0")


class TableOutputTest(unittest.TestCase):
    """A model.Table has to become a real IDML Table inside its story."""

    def _document(self) -> model.Document:
        document = model.Document(pages=[model.Page(width=600.0, height=800.0)])
        table = model.Table(
            x=20.0, y=30.0, width=300.0, height=100.0,
            column_widths=[100.0, 200.0],
            row_heights=[40.0, 60.0],
        )

        def cell(row, column, text, **spans):
            item = model.TableCell(row=row, column=column, **spans)
            paragraph = model.Paragraph()
            paragraph.spans.append(model.Span(text=text, size_pt=10.0))
            item.story.paragraphs.append(paragraph)
            return item

        table.cells = [
            cell(0, 0, "top left"),
            cell(0, 1, "top right"),
            cell(1, 0, "spanning", column_span=2),
        ]
        document.pages[0].items.append(table)
        return document

    def _parts(self):
        path = write_package(self._document())
        with zipfile.ZipFile(path) as archive:
            story = next(n for n in archive.namelist() if n.startswith("Stories/"))
            spread = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            return (
                ET.fromstring(archive.read(story)),
                ET.fromstring(archive.read(spread)),
            )

    def test_the_story_holds_a_table(self):
        story, _ = self._parts()
        tables = list(story.iter("Table"))
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].get("ColumnCount"), "2")
        self.assertEqual(tables[0].get("BodyRowCount"), "2")

    def test_rows_and_columns_carry_their_measurements(self):
        story, _ = self._parts()
        table = next(story.iter("Table"))
        self.assertEqual(
            [r.get("SingleRowHeight") for r in table.iter("Row")], ["40", "60"]
        )
        self.assertEqual(
            [c.get("SingleColumnWidth") for c in table.iter("Column")], ["100", "200"]
        )

    def test_cells_are_named_column_then_row(self):
        story, _ = self._parts()
        names = [c.get("Name") for c in next(story.iter("Table")).iter("Cell")]
        self.assertEqual(names, ["0:0", "1:0", "0:1"])

    def test_each_cell_carries_its_own_text(self):
        story, _ = self._parts()
        found = {}
        for cell in next(story.iter("Table")).iter("Cell"):
            found[cell.get("Name")] = "".join(
                e.text or "" for e in cell.iter("Content")
            )
        self.assertEqual(found["0:0"], "top left")
        self.assertEqual(found["1:0"], "top right")
        self.assertEqual(found["0:1"], "spanning")

    def test_a_span_is_written(self):
        story, _ = self._parts()
        spanning = next(
            c for c in next(story.iter("Table")).iter("Cell") if c.get("Name") == "0:1"
        )
        self.assertEqual(spanning.get("ColumnSpan"), "2")
        self.assertEqual(spanning.get("RowSpan"), "1")

    def test_a_frame_on_the_page_holds_the_table_story(self):
        story, spread = self._parts()
        frames = list(spread.iter("TextFrame"))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].get("ParentStory"), story.find("Story").get("Self"))

    def test_the_rows_come_before_the_columns_and_the_cells(self):
        # IDML expects Row*, Column*, then Cell*.
        story, _ = self._parts()
        tags = [child.tag for child in next(story.iter("Table"))]
        self.assertEqual(
            tags, ["Row", "Row", "Column", "Column", "Cell", "Cell", "Cell"]
        )

    def test_the_package_is_well_formed(self):
        path = write_package(self._document())
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".xml"):
                    ET.fromstring(archive.read(name))


class ShadowOutputTest(unittest.TestCase):
    """Publisher's shadow, as an IDML drop shadow on the page item."""

    def _spread(self, *items) -> ET.Element:
        document = model.Document(pages=[model.Page(width=600.0, height=800.0)])
        document.pages[0].items.extend(items)
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            return ET.fromstring(archive.read(name))

    def _rectangle(self, shadow) -> model.Rectangle:
        return model.Rectangle(
            x=10.0, y=20.0, width=100.0, height=50.0,
            style=model.GraphicStyle(fill=(255, 255, 255), shadow=shadow),
        )

    def _setting(self, shadow) -> ET.Element:
        spread = self._spread(self._rectangle(shadow))
        settings = list(spread.iter("DropShadowSetting"))
        self.assertEqual(len(settings), 1)
        return settings[0]

    def test_a_shape_without_a_shadow_says_nothing_about_one(self):
        spread = self._spread(self._rectangle(None))
        self.assertEqual(list(spread.iter("TransparencySetting")), [])
        self.assertEqual(list(spread.iter("DropShadowSetting")), [])

    def test_the_offset_opacity_and_colour_are_written(self):
        setting = self._setting(
            model.Shadow(color=(0xC0, 0xC0, 0xC0), offset_x=2.0, offset_y=3.0, opacity=0.5)
        )
        self.assertEqual(setting.get("Mode"), "Drop")
        self.assertEqual(setting.get("XOffset"), "2")
        self.assertEqual(setting.get("YOffset"), "3")
        self.assertEqual(setting.get("Opacity"), "50")
        self.assertEqual(setting.get("EffectColor"), "Color/C_C0C0C0")

    def test_publishers_shadow_is_hard_edged(self):
        # A reader's own default blur would soften every one of them.
        setting = self._setting(model.Shadow(color=(0, 0, 0), offset_x=2.0, offset_y=2.0))
        self.assertEqual(setting.get("Size"), "0")
        self.assertEqual(setting.get("Spread"), "0")
        self.assertEqual(setting.get("Noise"), "0")

    def test_a_down_right_shadow_puts_the_light_at_the_top_left(self):
        # 135 degrees with both offsets positive is InDesign's own default,
        # which is what fixes the convention: the angle is the light.
        setting = self._setting(model.Shadow(color=(0, 0, 0), offset_x=9.0, offset_y=9.0))
        self.assertEqual(setting.get("Angle"), "135")
        self.assertAlmostEqual(float(setting.get("Distance")), 12.72792, places=4)

    def test_an_up_left_shadow_is_the_opposite_angle(self):
        setting = self._setting(model.Shadow(color=(0, 0, 0), offset_x=-9.0, offset_y=-9.0))
        self.assertEqual(setting.get("Angle"), "315")

    def test_a_global_light_angle_must_not_override_the_offset(self):
        setting = self._setting(model.Shadow(color=(0, 0, 0), offset_x=2.0, offset_y=2.0))
        self.assertEqual(setting.get("UseGlobalLight"), "false")

    def test_a_shadowed_text_frame_carries_it_too(self):
        frame = model.TextFrame(
            x=10.0, y=20.0, width=100.0, height=50.0,
            style=model.GraphicStyle(
                shadow=model.Shadow(color=(0, 0, 0), offset_x=2.0, offset_y=2.0)
            ),
        )
        frame.story.paragraphs.append(model.Paragraph())
        spread = self._spread(frame)
        self.assertEqual(len(list(spread.iter("DropShadowSetting"))), 1)

    def test_the_package_is_well_formed(self):
        document = model.Document(pages=[model.Page(width=600.0, height=800.0)])
        document.pages[0].items.append(
            self._rectangle(model.Shadow(color=(1, 2, 3), offset_x=2.0, offset_y=2.0))
        )
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".xml"):
                    ET.fromstring(archive.read(name))


class OpacityOutputTest(unittest.TestCase):
    """A see-through fill, stated as opacity rather than as a pale tint."""

    def _spread(self, style: model.GraphicStyle) -> ET.Element:
        document = model.Document(pages=[model.Page(width=600.0, height=800.0)])
        document.pages[0].items.append(
            model.Rectangle(x=10.0, y=20.0, width=100.0, height=50.0, style=style)
        )
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.startswith("Spreads/"))
            return ET.fromstring(archive.read(name))

    def test_opacity_is_written_as_a_blending_setting(self):
        spread = self._spread(
            model.GraphicStyle(fill=(255, 0, 0), fill_opacity=0.6)
        )
        settings = list(spread.iter("BlendingSetting"))
        self.assertEqual(len(settings), 1)
        self.assertEqual(settings[0].get("Opacity"), "60")

    def test_a_tint_is_no_longer_used_to_fake_it(self):
        # FillTint mixes the colour with the paper; it is not transparency.
        spread = self._spread(
            model.GraphicStyle(fill=(255, 0, 0), fill_opacity=0.6)
        )
        rectangle = next(spread.iter("Rectangle"))
        self.assertIsNone(rectangle.get("FillTint"))

    def test_an_opaque_shape_says_nothing_about_transparency(self):
        spread = self._spread(model.GraphicStyle(fill=(255, 0, 0)))
        self.assertEqual(list(spread.iter("TransparencySetting")), [])

    def test_opacity_and_a_shadow_share_one_element(self):
        spread = self._spread(
            model.GraphicStyle(
                fill=(255, 0, 0),
                fill_opacity=0.6,
                shadow=model.Shadow(color=(0, 0, 0), offset_x=2.0, offset_y=2.0),
            )
        )
        settings = list(spread.iter("TransparencySetting"))
        self.assertEqual(len(settings), 1)
        self.assertEqual(
            [child.tag for child in settings[0]],
            ["BlendingSetting", "DropShadowSetting"],
        )


class CellInsetOutputTest(unittest.TestCase):
    """Publisher's cell padding, written where the reader will honour it."""

    def _cells(self, insets):
        document = model.Document(pages=[model.Page(width=600.0, height=800.0)])
        table = model.Table(
            x=0.0, y=0.0, width=200.0, height=50.0,
            column_widths=[100.0, 100.0],
            row_heights=[50.0],
        )
        table.cells = [
            model.TableCell(row=0, column=0, insets=insets),
            model.TableCell(row=0, column=1),
        ]
        document.pages[0].items.append(table)
        path = write_package(document)
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.startswith("Stories/"))
            story = ET.fromstring(archive.read(name))
        return {c.get("Name"): c for c in story.iter("Cell")}

    def test_the_four_sides_are_written_in_points(self):
        cells = self._cells(model.CellInsets(left=2.88, top=0.75, right=1.0, bottom=0.0))
        cell = cells["0:0"]
        self.assertEqual(cell.get("LeftInset"), "2.88")
        self.assertEqual(cell.get("TopInset"), "0.75")
        self.assertEqual(cell.get("RightInset"), "1")
        self.assertEqual(cell.get("BottomInset"), "0")

    def test_a_cell_we_know_nothing_about_keeps_the_readers_default(self):
        # Writing zeros here would be a claim the file never made.
        cells = self._cells(model.CellInsets())
        for attribute in ("LeftInset", "TopInset", "RightInset", "BottomInset"):
            self.assertIsNone(cells["1:0"].get(attribute))

    def test_a_zero_inset_is_written_rather_than_left_out(self):
        cells = self._cells(model.CellInsets())
        self.assertEqual(cells["0:0"].get("LeftInset"), "0")


class HangingIndentTabTest(unittest.TestCase):
    """A hanging indent needs the tab stop that makes it hang.

    libmspub reports five paragraph properties and no tab stop is among
    them, so every tab lands on the reader's default half-inch grid. The
    one position recoverable without them is the one a hanging indent
    implies: the tab after the outdented label goes to the left indent.
    """

    def _paragraph(self, **kwargs) -> ET.Element:
        document = model.Document(pages=[model.Page(width=400.0, height=600.0)])
        frame = model.TextFrame(x=10.0, y=10.0, width=300.0, height=200.0)
        paragraph = model.Paragraph(**kwargs)
        paragraph.spans.append(model.Span(text="Overgegaan\tnaar de PKN", size_pt=9.0))
        frame.story.paragraphs.append(paragraph)
        document.pages[0].items.append(frame)
        with zipfile.ZipFile(write_package(document)) as archive:
            story = next(n for n in archive.namelist() if n.startswith("Stories/"))
            return next(ET.fromstring(archive.read(story)).iter("ParagraphStyleRange"))

    def test_a_hanging_indent_gets_a_stop_at_its_left_indent(self):
        found = self._paragraph(margin_left=84.75, first_line_indent=-84.75)
        stop = found.find(".//TabList/ListItem/Position")
        self.assertIsNotNone(stop, ET.tostring(found))
        self.assertEqual(stop.text, "84.75")

    def test_the_stop_is_a_plain_left_aligned_one(self):
        found = self._paragraph(margin_left=84.75, first_line_indent=-84.75)
        item = found.find(".//TabList/ListItem")
        self.assertEqual(item.find("Alignment").text, "LeftAlign")
        self.assertEqual(item.find("Leader").text, None)

    def test_an_ordinary_paragraph_is_left_alone(self):
        # Inventing a stop here would move text that is already right.
        self.assertIsNone(self._paragraph().find(".//TabList"))

    def test_a_plain_left_indent_is_left_alone(self):
        found = self._paragraph(margin_left=36.0)
        self.assertIsNone(found.find(".//TabList"))

    def test_an_outdent_with_no_left_indent_is_left_alone(self):
        found = self._paragraph(first_line_indent=-18.0)
        self.assertIsNone(found.find(".//TabList"))


class StatedTabStopTest(unittest.TestCase):
    """The stops the .pub itself records, which beat anything inferred."""

    def _stops(self, *stops, **kwargs) -> ET.Element:
        document = model.Document(pages=[model.Page(width=400.0, height=600.0)])
        frame = model.TextFrame(x=10.0, y=10.0, width=300.0, height=200.0)
        paragraph = model.Paragraph(tab_stops=list(stops), **kwargs)
        paragraph.spans.append(model.Span(text="Consegna\tlavori", size_pt=9.0))
        frame.story.paragraphs.append(paragraph)
        document.pages[0].items.append(frame)
        with zipfile.ZipFile(write_package(document)) as archive:
            story = next(n for n in archive.namelist() if n.startswith("Stories/"))
            found = next(ET.fromstring(archive.read(story)).iter("ParagraphStyleRange"))
            return found.findall(".//TabList/ListItem")

    def positions(self, items):
        return [item.find("Position").text for item in items]

    def test_a_stated_stop_is_written_where_the_file_puts_it(self):
        items = self._stops(model.TabStop(position=27.4))
        self.assertEqual(self.positions(items), ["27.4"])

    def test_the_stops_arrive_in_order_however_they_were_read(self):
        items = self._stops(
            model.TabStop(position=48.0), model.TabStop(position=12.0)
        )
        self.assertEqual(self.positions(items), ["12", "48"])

    def test_a_centre_and_a_right_tab_keep_their_alignment(self):
        items = self._stops(
            model.TabStop(position=165.11, alignment="center"),
            model.TabStop(position=329.22, alignment="right"),
        )
        self.assertEqual(
            [item.find("Alignment").text for item in items],
            ["CenterAlign", "RightAlign"],
        )

    def test_a_stop_left_of_the_text_cannot_be_written_and_is_dropped(self):
        # One style in the corpus puts three stops at negative positions;
        # IDML measures from the text edge and has nowhere to put them.
        items = self._stops(
            model.TabStop(position=-13.95), model.TabStop(position=14.5)
        )
        self.assertEqual(self.positions(items), ["14.5"])

    def test_a_hanging_indent_still_gets_its_implied_stop(self):
        # The file states one stop; the outdent implies another, and both
        # are real -- Publisher honours the hanging position regardless.
        items = self._stops(
            model.TabStop(position=120.0), margin_left=36.0, first_line_indent=-36.0
        )
        self.assertEqual(self.positions(items), ["36", "120"])

    def test_the_implied_stop_is_not_repeated_where_the_file_states_it(self):
        items = self._stops(
            model.TabStop(position=36.0), margin_left=36.0, first_line_indent=-36.0
        )
        self.assertEqual(self.positions(items), ["36"])
