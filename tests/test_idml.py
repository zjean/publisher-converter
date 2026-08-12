"""IDML output: the artefact must be loadable, and its links must resolve."""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pubidml import idml, model

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
        document = graphic_object_document(mime="image/x-emf")
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


if __name__ == "__main__":
    unittest.main()
