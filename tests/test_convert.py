"""Single-file conversion, including the real parser when it is built."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from pubidml import convert, model

from . import support
from .support import event

REPO = Path(__file__).resolve().parent.parent
SAMPLES = REPO / "files"
CORPUS = REPO / "tests" / "data"
PUBDUMP = convert.PUBDUMP

needs_parser = unittest.skipUnless(
    PUBDUMP.exists(), f"pubdump not built at {PUBDUMP}; run 'make'"
)


def fake_pubdump(directory: Path, script: str) -> Path:
    """A stand-in parser, so failure modes can be produced on demand."""
    path = directory / "pubdump"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@unittest.skipIf(os.name == "nt", "shell stand-ins are POSIX-only")
class ParserContractTest(unittest.TestCase):
    """Exit codes and stream completeness are the contract with the C++ half."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.source = self.work / "sample.pub"
        self.source.write_bytes(b"not really a pub file")

    def _parse(self, script: str):
        return convert.parse_document(self.source, fake_pubdump(self.work, script))

    def test_missing_binary_is_reported_clearly(self):
        with self.assertRaises(convert.ConversionError) as caught:
            convert.parse_document(self.source, self.work / "does-not-exist")
        self.assertIn("run 'make'", str(caught.exception))

    def test_unsupported_file_exit_code(self):
        with self.assertRaises(convert.ConversionError) as caught:
            self._parse("exit 3\n")
        self.assertIn("not a supported Publisher file", str(caught.exception))

    def test_parse_failure_exit_code(self):
        with self.assertRaises(convert.ConversionError) as caught:
            self._parse("exit 4\n")
        self.assertIn("libmspub could not parse", str(caught.exception))

    def test_write_failure_exit_code(self):
        with self.assertRaises(convert.ConversionError) as caught:
            self._parse("exit 5\n")
        self.assertIn("could not write its event stream", str(caught.exception))

    def test_a_stream_truncated_on_a_line_boundary_is_refused(self):
        # This is the dangerous shape: valid JSON, fewer pages, and without
        # the sentinel it would be reported as a complete success.
        script = (
            "printf '%s\\n' "
            "'{\"e\":\"startPage\",\"p\":{\"svg:width\":\"8.5in\",\"svg:height\":\"11in\"}}' "
            "'{\"e\":\"endPage\"}'\n"
            "exit 0\n"
        )
        with self.assertRaises(convert.ConversionError) as caught:
            self._parse(script)
        self.assertIn("truncated", str(caught.exception))

    def test_a_complete_stream_is_accepted(self):
        script = (
            "printf '%s\\n' "
            "'{\"e\":\"startPage\",\"p\":{\"svg:width\":\"8.5in\",\"svg:height\":\"11in\"}}' "
            "'{\"e\":\"endPage\"}' '{\"e\":\"endDocument\"}'\n"
            "exit 0\n"
        )
        document = self._parse(script)
        self.assertTrue(document.complete)
        self.assertEqual(len(document.pages), 1)

    def test_a_malformed_line_is_reported_as_such(self):
        with self.assertRaises(convert.ConversionError) as caught:
            self._parse("echo 'this is not json'\nexit 0\n")
        self.assertIn("malformed event stream", str(caught.exception))

    def test_a_chatty_parser_does_not_deadlock(self):
        # stderr goes to a file, not a pipe: nothing drains a pipe while the
        # event stream is being read, so a noisy libmspub would hang here.
        script = (
            "i=0; while [ $i -lt 4000 ]; do "
            "echo 'libmspub: unknown structure' >&2; i=$((i+1)); done\n"
            "printf '%s\\n' '{\"e\":\"endDocument\"}'\n"
            "exit 0\n"
        )
        document = self._parse(script)
        self.assertTrue(document.complete)


class WorkerIsolationTest(unittest.TestCase):
    """convert() must never propagate: cli collects results across a batch."""

    def test_an_unexpected_exception_becomes_a_failed_result(self):
        def explode(*args, **kwargs):
            raise RuntimeError("something nobody predicted")

        original = convert.parse_document
        convert.parse_document = explode
        try:
            result = convert.convert(Path("whatever.pub"), Path("out.idml"))
        finally:
            convert.parse_document = original

        self.assertFalse(result.ok)
        self.assertIn("unexpected error", result.error)
        self.assertIn("something nobody predicted", result.error)

    def test_a_conversion_error_becomes_a_failed_result(self):
        result = convert.convert(
            Path("missing.pub"), Path("out.idml"), pubdump=Path("/nonexistent/pubdump")
        )
        self.assertFalse(result.ok)
        self.assertIsNone(result.output)


class RasteriseWalkTest(unittest.TestCase):
    """Metafile handling must reach artwork at any depth."""

    def _document_with_grouped_metafile(self) -> model.Document:
        document = model.Document(pages=[model.Page()])
        artwork = model.Image(
            data=b"\x00" * 200, mime_type="image/x-emf", width=72.0, height=72.0
        )
        document.pages[0].items.append(model.Group(children=[artwork]))
        return document

    def test_grouped_metafile_artwork_is_not_left_behind(self):
        document = self._document_with_grouped_metafile()
        convert._rasterise_metafiles(document)
        remaining = [
            i for i in model._walk(document.pages[0].items) if isinstance(i, model.Image)
        ]
        # Unconvertible here (the payload is not a real EMF), so it must be
        # dropped rather than written out as a broken link.
        self.assertEqual(remaining, [])

    def test_a_top_level_metafile_behaves_the_same_way(self):
        document = model.Document(pages=[model.Page()])
        document.pages[0].items.append(
            model.Image(data=b"\x00" * 200, mime_type="image/x-emf")
        )
        convert._rasterise_metafiles(document)
        self.assertEqual(document.pages[0].items, [])

    def test_ordinary_items_survive_at_every_depth(self):
        document = model.Document(pages=[model.Page()])
        inner = model.Rectangle(width=10.0, height=10.0)
        outer = model.TextFrame()
        document.pages[0].items += [model.Group(children=[inner]), outer]
        convert._rasterise_metafiles(document)
        survivors = list(model._walk(document.pages[0].items))
        self.assertIn(inner, survivors)
        self.assertIn(outer, survivors)


@needs_parser
class RealFileTest(unittest.TestCase):
    """End-to-end against the actual libmspub parser."""

    def convert_one(self, source: Path):
        work = Path(tempfile.mkdtemp())
        destination = work / f"{source.stem}.idml"
        return convert.convert(source, destination), destination

    def test_every_sample_converts_to_a_well_formed_package(self):
        samples = sorted(SAMPLES.glob("*.pub"))
        self.assertTrue(samples, "no sample .pub files present")
        for source in samples:
            with self.subTest(source=source.name):
                result, destination = self.convert_one(source)
                self.assertTrue(result.ok, result.error)
                self.assertTrue(destination.exists())
                with zipfile.ZipFile(destination) as archive:
                    self.assertEqual(archive.namelist()[0], "mimetype")
                    for name in archive.namelist():
                        if name.endswith(".xml"):
                            ET.fromstring(archive.read(name))

    def test_every_declared_image_link_resolves_on_disk(self):
        from urllib.parse import unquote, urlsplit

        for source in sorted(SAMPLES.glob("*.pub")):
            with self.subTest(source=source.name):
                result, destination = self.convert_one(source)
                self.assertTrue(result.ok, result.error)
                with zipfile.ZipFile(destination) as archive:
                    for name in archive.namelist():
                        if not name.startswith("Spreads/"):
                            continue
                        root = ET.fromstring(archive.read(name))
                        for link in root.iter("Link"):
                            uri = link.get("LinkResourceURI")
                            target = destination.parent / unquote(urlsplit(uri).path)
                            self.assertTrue(target.exists(), f"{uri} -> {target}")

    def test_a_filename_with_awkward_characters_survives_end_to_end(self):
        source = next(iter(sorted(SAMPLES.glob("*.pub"))), None)
        self.assertIsNotNone(source)
        work = Path(tempfile.mkdtemp())
        awkward = work / "Newsletter #3 50% off.pub"
        awkward.write_bytes(source.read_bytes())

        result, destination = self.convert_one(awkward)
        self.assertTrue(result.ok, result.error)

        from urllib.parse import unquote, urlsplit

        with zipfile.ZipFile(destination) as archive:
            for name in archive.namelist():
                if not name.startswith("Spreads/"):
                    continue
                for link in ET.fromstring(archive.read(name)).iter("Link"):
                    uri = link.get("LinkResourceURI")
                    target = destination.parent / unquote(urlsplit(uri).path)
                    self.assertTrue(target.exists(), f"{uri} -> {target}")

    def test_hostile_corpus_files_produce_clean_results_not_tracebacks(self):
        # Two of the three libmspub regression files are rejected outright;
        # either outcome is fine, a traceback is not.
        for source in sorted(CORPUS.glob("*.pub")):
            with self.subTest(source=source.name):
                result, _ = self.convert_one(source)
                self.assertIsInstance(result, convert.Result)
                if not result.ok:
                    self.assertTrue(result.error)


if __name__ == "__main__":
    unittest.main()
