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
