"""Single-file conversion, including the real parser when it is built."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from pubidml import convert, fontmetrics, model, pubfile

from . import support, test_metafile
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


class MetafileReportingTest(unittest.TestCase):
    """What the operator is told about artwork that did not make it."""

    def _document(self, *images: model.Image) -> model.Document:
        document = model.Document(pages=[model.Page()])
        document.pages[0].items.extend(images)
        return document

    def _image(self, data: bytes, mime: str) -> model.Image:
        return model.Image(data=data, mime_type=mime, width=72.0, height=72.0)

    def test_a_wrapped_photograph_is_unwrapped_with_no_external_tool(self):
        payload = test_metafile.dib(2, 2, bytes([10, 20, 30, 0] * 4))
        document = self._document(
            self._image(test_metafile.emf_with_dib(payload), "image/emf")
        )
        convert._rasterise_metafiles(document)

        images = [i for i in document.pages[0].items if isinstance(i, model.Image)]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].mime_type, "image/png")
        self.assertEqual(images[0].data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(document.warnings, [])

    def test_wmf_line_art_becomes_editable_shapes(self):
        from .test_wmf import UNIT_WINDOW, build, create_brush, polygon, select

        art = build(
            *UNIT_WINDOW,
            create_brush(0, 0x0000FF),  # solid red
            select(0),
            polygon((0, 0), (100, 0), (100, 100)),
        )
        document = self._document(self._image(art, "image/wmf"))
        convert._rasterise_metafiles(document)

        shapes = [
            i for i in model._walk(document.pages[0].items)
            if isinstance(i, model.Polygon)
        ]
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0].style.fill, (255, 0, 0))
        # Converted, so nothing to report.
        self.assertEqual(document.warnings, [])

    def test_the_shapes_are_grouped_so_the_artwork_stays_one_object(self):
        from .test_wmf import UNIT_WINDOW, build, polygon

        art = build(*UNIT_WINDOW, polygon((0, 0), (10, 0)), polygon((5, 5), (9, 9)))
        document = self._document(self._image(art, "image/wmf"))
        convert._rasterise_metafiles(document)

        top = document.pages[0].items
        self.assertEqual(len(top), 1)
        self.assertIsInstance(top[0], model.Group)

    def test_records_we_cannot_draw_are_reported_with_what_survived(self):
        from .test_wmf import META_ARC, UNIT_WINDOW, build, polygon, record
        import struct as _struct

        art = build(
            *UNIT_WINDOW,
            polygon((0, 0), (10, 0), (10, 10)),
            record(META_ARC, _struct.pack("<8h", *range(8))),
        )
        document = self._document(self._image(art, "image/wmf"))
        convert._rasterise_metafiles(document)

        self.assertEqual(len(document.warnings), 1, document.warnings)
        warning = document.warnings[0]
        self.assertIn("1", warning)
        self.assertNotIn("dropped", warning)

    def test_a_wmf_wrapping_a_bitmap_is_still_unwrapped_not_traced(self):
        payload = test_metafile.dib(2, 2, bytes([10, 20, 30, 0] * 4))
        document = self._document(
            self._image(test_metafile.emf_with_dib(payload), "image/emf")
        )
        convert._rasterise_metafiles(document)
        images = [i for i in document.pages[0].items if isinstance(i, model.Image)]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].mime_type, "image/png")

    def test_one_logo_used_many_times_is_reported_once(self):
        logo = test_metafile.wmf(
            (test_metafile.META_POLYGON, 528), (test_metafile.META_EOF, 6)
        )
        document = self._document(
            *[self._image(logo, "image/wmf") for _ in range(64)]
        )
        convert._rasterise_metafiles(document)

        self.assertEqual(len(document.warnings), 1, document.warnings)
        self.assertIn("64", document.warnings[0])

    def test_two_different_pieces_of_artwork_are_reported_separately(self):
        first = test_metafile.wmf(
            (test_metafile.META_POLYGON, 528), (test_metafile.META_EOF, 6)
        )
        second = test_metafile.wmf(
            (test_metafile.META_POLYPOLYGON, 64), (test_metafile.META_EOF, 6)
        )
        document = self._document(
            self._image(first, "image/wmf"), self._image(second, "image/wmf")
        )
        convert._rasterise_metafiles(document)
        self.assertEqual(len(document.warnings), 2, document.warnings)

    def test_a_wmf_is_not_blamed_on_a_failed_conversion(self):
        # emf2svg-conv reads EMF only, so nothing was ever attempted. The
        # old message said "conversion failed" whenever the tools happened
        # to be installed, which named the wrong culprit.
        logo = test_metafile.wmf(
            (test_metafile.META_POLYGON, 528), (test_metafile.META_EOF, 6)
        )
        document = self._document(self._image(logo, "image/wmf"))
        convert._rasterise_metafiles(document)

        warning = document.warnings[0]
        self.assertIn("WMF", warning)
        self.assertNotIn("conversion failed", warning)

    def test_the_real_drawing_record_count_is_reported(self):
        logo = test_metafile.wmf(
            (test_metafile.META_POLYGON, 528),
            (test_metafile.META_POLYPOLYGON, 64),
            (test_metafile.META_EOF, 6),
        )
        document = self._document(self._image(logo, "image/wmf"))
        convert._rasterise_metafiles(document)
        # Not "0 drawing records", which is what a WMF read as an EMF gives.
        self.assertIn("2", document.warnings[0])


class DegeneratePathTest(unittest.TestCase):
    """A filled path made only of 2-point edges cannot draw anything.

    libmspub reports most Publisher paths as disconnected edges. Joining
    them used to produce a filled bowtie across the page; keeping them
    apart is correct but leaves a shape with no area, so the artwork is
    absent either way and the operator should be told rather than left to
    spot it.
    """

    def _document(self, ops: list, **style) -> model.Document:
        document = model.Document(pages=[model.Page()])
        document.pages[0].items.append(
            model.Path(
                x=10.0, y=10.0, width=100.0, height=30.0,
                ops=ops, style=model.GraphicStyle(**style),
            )
        )
        return document

    TWO_RULES = [
        ("M", 10.0, 10.0), ("L", 110.0, 10.0), ("Z",),
        ("M", 10.0, 40.0), ("L", 110.0, 40.0), ("Z",),
    ]

    def test_a_fill_only_path_of_bare_edges_is_reported(self):
        document = self._document(self.TWO_RULES, fill=(0, 0, 0))
        convert._check_unrenderable_paths(document)
        self.assertEqual(len(document.warnings), 1, document.warnings)
        self.assertIn("no area", document.warnings[0])

    def test_several_are_collapsed_into_one_line(self):
        document = self._document(self.TWO_RULES, fill=(0, 0, 0))
        for _ in range(4):
            document.pages[0].items.append(document.pages[0].items[0])
        convert._check_unrenderable_paths(document)
        self.assertEqual(len(document.warnings), 1)
        self.assertIn("5", document.warnings[0])

    def test_a_stroked_path_of_bare_edges_draws_fine(self):
        document = self._document(self.TWO_RULES, stroke=(0, 0, 0), stroke_width=1.0)
        convert._check_unrenderable_paths(document)
        self.assertEqual(document.warnings, [])

    def test_a_path_with_real_area_is_not_reported(self):
        ops = [
            ("M", 10.0, 10.0), ("L", 110.0, 10.0),
            ("L", 110.0, 40.0), ("Z",),
        ]
        document = self._document(ops, fill=(0, 0, 0))
        convert._check_unrenderable_paths(document)
        self.assertEqual(document.warnings, [])

    def test_a_path_lifted_onto_a_master_is_reported_too(self):
        # The pass walked pages only, so a path the master pass had moved
        # went quiet -- and on a master it draws nothing on every page
        # applying it, which is the loss most worth naming.
        document = self._document(self.TWO_RULES, fill=(0, 0, 0))
        document.masters.append(
            model.Master(name="A", items=list(document.pages[0].items))
        )
        document.pages[0].items.clear()
        convert._check_unrenderable_paths(document)
        self.assertEqual(len(document.warnings), 1, document.warnings)


class GradientLossTest(unittest.TestCase):
    """A ramp that could not be written as one has to be said out loud.

    Both of these are worked out where the fill is read and were then set
    and never looked at, which made a flattened gradient the one loss in
    the conversion indistinguishable from a deliberate flat fill.
    """

    def _document(self, count: int = 1, **flags) -> model.Document:
        document = model.Document(pages=[model.Page()])
        for _ in range(count):
            document.pages[0].items.append(
                model.Rectangle(
                    x=0.0, y=0.0, width=10.0, height=10.0,
                    style=model.GraphicStyle(fill=(0, 0, 0), **flags),
                )
            )
        return document

    def test_a_ramp_flattened_to_one_stop_is_reported(self):
        document = self._document(approximated_fill=True)
        convert._check_gradient_losses(document)
        self.assertEqual(len(document.warnings), 1, document.warnings)
        self.assertIn("flattened to one colour", document.warnings[0])

    def test_several_are_counted_into_one_line(self):
        document = self._document(count=10, approximated_fill=True)
        convert._check_gradient_losses(document)
        self.assertEqual(len(document.warnings), 1)
        self.assertIn("10", document.warnings[0])

    def test_stops_that_disagree_on_opacity_are_reported(self):
        document = self._document(uneven_stop_opacity=True)
        convert._check_gradient_losses(document)
        self.assertIn("differing opacity", " ".join(document.warnings))

    def test_a_ramp_that_came_across_whole_says_nothing(self):
        ramp = model.Gradient(
            stops=(
                model.GradientStop(location=0.0, color=(0, 0, 0)),
                model.GradientStop(location=100.0, color=(255, 255, 255)),
            )
        )
        document = self._document(gradient=ramp)
        convert._check_gradient_losses(document)
        self.assertEqual(document.warnings, [])

    def test_a_single_stop_ramp_sets_the_flag_that_gets_reported(self):
        # The whole chain, so the flag cannot quietly stop being set.
        style = model.GraphicStyle.from_props({
            "draw:fill": "gradient",
            "svg:linearGradient": [
                {"svg:offset": "53%", "svg:stop-color": "#e1e1e1"},
            ],
        })
        self.assertTrue(style.approximated_fill)
        self.assertIsNone(style.gradient)
        document = model.Document(pages=[model.Page()])
        document.pages[0].items.append(
            model.Rectangle(x=0.0, y=0.0, width=10.0, height=10.0, style=style)
        )
        convert._check_gradient_losses(document)
        self.assertIn("flattened to one colour", " ".join(document.warnings))


class ThreadDuplicateStoriesTest(unittest.TestCase):
    """Linked Publisher text boxes arrive as one story duplicated per frame.

    librevenge's drawing interface has no notion of a threaded frame, so
    libmspub hands the complete story to every frame in the chain. Writing
    those through verbatim puts the same article on the page N times.
    """

    LONG = "word " * 300  # 1500 chars: far past a 3x2in frame at 12pt

    def _chain_of(self, count: int, text: str, **frame) -> model.Document:
        pages = [support.text_frame(text, **frame) for _ in range(count)]
        return support.paged_document(*pages)

    def _frames(self, document: model.Document):
        return [
            item
            for page in document.pages
            for item in model._walk(page.items)
            if isinstance(item, model.TextFrame)
        ]

    def test_a_duplicated_overset_story_becomes_one_chain(self):
        document = self._chain_of(3, self.LONG)
        convert._thread_duplicate_stories(document)

        frames = self._frames(document)
        chain_ids = {frame.chain_id for frame in frames}
        self.assertEqual(len(chain_ids), 1)
        self.assertNotIn(None, chain_ids)
        self.assertEqual([len(c) for c in document.text_chains.values()], [3])

    def test_only_the_first_frame_of_a_chain_keeps_the_text(self):
        document = self._chain_of(3, self.LONG)
        convert._thread_duplicate_stories(document)

        first, *rest = self._frames(document)
        self.assertFalse(first.story.is_empty())
        for frame in rest:
            self.assertTrue(frame.story.is_empty())

    def test_chain_order_follows_the_page_order_of_the_frames(self):
        document = self._chain_of(4, self.LONG)
        convert._thread_duplicate_stories(document)

        chain = next(iter(document.text_chains.values()))
        self.assertEqual(chain, self._frames(document))

    def test_a_repeated_label_that_fits_its_frame_is_left_alone(self):
        # The page-number field is the motivating case: one sample repeats a
        # single "#" across 27 frames. Threading those would move 26 of them
        # into a chain and blank the page numbers.
        document = self._chain_of(5, "#")
        convert._thread_duplicate_stories(document)

        frames = self._frames(document)
        self.assertEqual({frame.chain_id for frame in frames}, {None})
        for frame in frames:
            self.assertFalse(frame.story.is_empty())

    def test_a_story_appearing_once_is_never_threaded(self):
        document = self._chain_of(1, self.LONG)
        convert._thread_duplicate_stories(document)

        self.assertEqual(self._frames(document)[0].chain_id, None)
        self.assertEqual(document.text_chains, {})

    def test_distinct_stories_are_not_merged(self):
        document = support.paged_document(
            support.text_frame(self.LONG),
            support.text_frame(self.LONG.replace("word", "other")),
        )
        convert._thread_duplicate_stories(document)

        self.assertEqual({f.chain_id for f in self._frames(document)}, {None})

    def test_master_frames_are_not_drawn_into_a_page_chain(self):
        document = self._chain_of(2, self.LONG)
        master = model.Master(name="A")
        master.items.append(self._frames(document)[0].__class__(
            width=216.0, height=144.0, story=document.pages[0].items[0].story
        ))
        document.masters.append(master)
        convert._thread_duplicate_stories(document)

        chain = next(iter(document.text_chains.values()))
        self.assertEqual(len(chain), 2)
        self.assertNotIn(master.items[0], chain)

    def test_a_threaded_chain_no_longer_reports_a_too_small_frame(self):
        # The combined capacity of the chain is what has to hold the story,
        # so the per-frame warning was misreading a threaded article as a
        # degenerate frame size.
        document = self._chain_of(8, self.LONG)
        convert._thread_duplicate_stories(document)
        convert._check_overset_text(document)

        self.assertEqual(
            [w for w in document.warnings if "too small" in w], []
        )

    def test_a_genuinely_degenerate_frame_is_still_reported(self):
        document = support.document(
            *support.text_frame(self.LONG, width="0.08in", height="0.08in")
        )
        convert._thread_duplicate_stories(document)
        convert._check_overset_text(document)

        self.assertEqual(len([w for w in document.warnings if "too small" in w]), 1)

    def test_a_collapsed_frame_on_a_master_is_reported(self):
        # Nothing is ever threaded onto a master, so a frame there is
        # always measured on its own -- and it shows empty on every page
        # applying the master rather than on one.
        document = support.document(
            *support.text_frame(self.LONG, width="0.08in", height="0.08in")
        )
        document.masters.append(
            model.Master(name="A", items=list(document.pages[0].items))
        )
        document.pages[0].items.clear()
        convert._check_overset_text(document)

        self.assertEqual(len([w for w in document.warnings if "too small" in w]), 1)


class UnreadableStructureTest(unittest.TestCase):
    """What the report says when the .pub's own structure will not read.

    Every structure-driven pass returns early, which is right -- it must
    never cost a conversion. But five of them going quiet at once used to
    reach the operator as `ok`, the status meaning "nothing suspicious",
    on a document whose page numbers all still read '#'.
    """

    def test_the_report_says_the_structure_could_not_be_read(self):
        document = support.document(*support.text_frame("hello"))
        convert._note_unreadable_structure(document, None)
        self.assertEqual(len(document.warnings), 1)
        said = document.warnings[0]
        for expected in ("page-number", "master", "tab", "WordArt"):
            self.assertIn(expected, said)

    def test_a_structure_that_did_read_says_nothing(self):
        document = support.document(*support.text_frame("hello"))
        convert._note_unreadable_structure(document, pubfile.FileStructure())
        self.assertEqual(document.warnings, [])

    def test_the_file_no_longer_passes_as_nothing_suspicious(self):
        document = support.document(*support.text_frame("hello"))
        convert._note_unreadable_structure(document, None)
        result = convert.Result(source=Path("x.pub"), output=Path("x.idml"), pages=1)
        result.warnings = list(document.warnings)
        convert._count_content(document, result)
        self.assertTrue(result.needs_review)


class FileStatedChainTest(unittest.TestCase):
    """Threading what the .pub says is linked, rather than what looks it.

    The measured guess below can only see a chain whose story oversets its
    roomiest frame, because identical text alone also describes a repeated
    label. The file has no such difficulty: it names the story each frame
    holds and that frame's place in it.
    """

    SHORT = "a short paragraph that fits its frame with room to spare"
    LONG = "word " * 300

    def _frames(self, document: model.Document):
        return [
            item
            for page in document.pages
            for item in model._walk(page.items)
            if isinstance(item, model.TextFrame)
        ]

    def _anchored(self, document: model.Document, *chains) -> pubfile.FileStructure:
        """Give every frame a shape seqnum, and state the chains by it.

        Seqnums run 100, 101, ... in the order the frames arrived, so a
        chain written [1, 0] is the file saying the second frame comes
        first. Each frame is nudged sideways first so that no two share a
        centre, which is what lets §14's mapping tell the page chunks
        apart -- the real files manage it because their pages differ.
        """
        structure = pubfile.FileStructure()
        for index, (page_index, page, frame) in enumerate(
            (page_index, page, item)
            for page_index, page in enumerate(document.pages)
            for item in model._walk(page.items)
            if isinstance(item, model.TextFrame)
        ):
            frame.x += 12.0 * index
            structure.anchors.append(pubfile.ShapeAnchor(
                shape_seq=100 + index,
                centre_x=frame.x + frame.width / 2.0 - page.width / 2.0,
                centre_y=frame.y + frame.height / 2.0 - page.height / 2.0,
            ))
            structure.shape_pages[100 + index] = 300 + page_index
        structure.story_chains = [[100 + i for i in chain] for chain in chains]
        return structure

    def _chain_of(self, count: int, text: str, **frame) -> model.Document:
        return support.paged_document(
            *[support.text_frame(text, **frame) for _ in range(count)]
        )

    def test_the_file_s_order_beats_the_order_the_frames_arrived_in(self):
        # The whole of the item: page order is a guess, and a story that
        # flowed against the page sequence used to arrive threaded backwards.
        document = self._chain_of(3, self.LONG)
        first, second, third = self._frames(document)
        convert._thread_duplicate_stories(
            document, self._anchored(document, [2, 0, 1])
        )
        self.assertEqual(
            list(document.text_chains.values()), [[third, first, second]]
        )

    def test_a_chain_whose_text_fits_its_first_frame_is_threaded(self):
        # What the measured guess cannot reach. `1336 kerkbode.pub` carries
        # exactly this: 1,905 characters in a frame roomy enough for about
        # 2,325, duplicated into the frame it flows on to.
        document = self._chain_of(2, self.SHORT)
        convert._thread_duplicate_stories(
            document, self._anchored(document, [0, 1])
        )
        first, second = self._frames(document)
        self.assertEqual(first.chain_id, second.chain_id)
        self.assertIsNotNone(first.chain_id)
        self.assertFalse(first.story.is_empty())
        self.assertTrue(second.story.is_empty())

    def test_frames_the_file_links_to_nothing_are_left_alone(self):
        document = self._chain_of(2, self.SHORT)
        convert._thread_duplicate_stories(document, self._anchored(document))
        for frame in self._frames(document):
            self.assertIsNone(frame.chain_id)
            self.assertFalse(frame.story.is_empty())

    def test_a_chain_whose_frames_disagree_about_the_text_is_left_alone(self):
        # libmspub hands the whole story to every frame of a chain, so links
        # that do not agree are not the thing this pass models -- and
        # blanking them would throw text away rather than thread it.
        document = support.paged_document(
            support.text_frame(self.LONG),
            support.text_frame(self.LONG.replace("word", "other")),
        )
        convert._thread_duplicate_stories(
            document, self._anchored(document, [0, 1])
        )
        self.assertEqual(document.text_chains, {})
        for frame in self._frames(document):
            self.assertFalse(frame.story.is_empty())

    def test_a_chain_that_does_not_resolve_whole_is_threaded_by_neither(self):
        # Threading the links that did resolve would leave the rest holding
        # their copy of the story, so the article still arrives twice --
        # worse than leaving it alone, and harder to see.
        document = self._chain_of(2, self.SHORT)
        structure = self._anchored(document, [0, 1])
        structure.anchors = structure.anchors[:1]
        convert._thread_duplicate_stories(document, structure)
        self.assertEqual(document.text_chains, {})
        for frame in self._frames(document):
            self.assertFalse(frame.story.is_empty())

    def test_a_chain_that_does_not_resolve_whole_falls_back_to_the_guess(self):
        # And where the guess can see it, it still gets threaded -- which is
        # `Cantico_dei_Cantici.pub`, whose three columns overset plainly
        # while only two of its three shapes can be placed.
        document = self._chain_of(3, self.LONG)
        structure = self._anchored(document, [0, 1, 2])
        structure.anchors = structure.anchors[:2]
        convert._thread_duplicate_stories(document, structure)
        self.assertEqual([len(c) for c in document.text_chains.values()], [3])

    def test_the_measured_guess_still_runs_for_what_the_file_leaves_out(self):
        # Two chains: one the file states, one it does not. The file's is
        # threaded from the record and the other from the oversetting, so a
        # file whose shapes cannot be read loses nothing it used to have.
        document = support.paged_document(
            support.text_frame(self.SHORT),
            support.text_frame(self.SHORT),
            support.text_frame(self.LONG),
            support.text_frame(self.LONG),
        )
        convert._thread_duplicate_stories(
            document, self._anchored(document, [0, 1])
        )
        self.assertEqual([len(c) for c in document.text_chains.values()], [2, 2])

    def test_a_frame_already_in_a_stated_chain_is_not_threaded_twice(self):
        document = self._chain_of(3, self.LONG)
        convert._thread_duplicate_stories(
            document, self._anchored(document, [0, 1, 2])
        )
        self.assertEqual([len(c) for c in document.text_chains.values()], [3])

    def test_no_structure_at_all_changes_nothing(self):
        document = self._chain_of(3, self.LONG)
        convert._thread_duplicate_stories(document, None)
        self.assertEqual([len(c) for c in document.text_chains.values()], [3])


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


class ContentCensusTest(unittest.TestCase):
    """The report has to count what the output actually carries.

    Tables became real tables rather than flattened paragraphs, and a
    Table is not a TextFrame -- so without this the cell text vanished from
    the character count while being present in the package.
    """

    def _result(self, *items) -> convert.Result:
        document = model.Document(pages=[model.Page()])
        document.pages[0].items.extend(items)
        result = convert.Result(source=Path("x.pub"))
        convert._count_content(document, result)
        return result

    @staticmethod
    def _frame(text: str) -> model.TextFrame:
        frame = model.TextFrame()
        paragraph = model.Paragraph()
        paragraph.spans.append(model.Span(text=text))
        frame.story.paragraphs.append(paragraph)
        return frame

    @staticmethod
    def _table(*texts: str) -> model.Table:
        table = model.Table(column_widths=[10.0], row_heights=[10.0])
        for index, text in enumerate(texts):
            cell = model.TableCell(row=index, column=0)
            paragraph = model.Paragraph()
            paragraph.spans.append(model.Span(text=text))
            cell.story.paragraphs.append(paragraph)
            table.cells.append(cell)
        return table

    def test_a_frames_text_is_counted(self):
        result = self._result(self._frame("hello"))
        self.assertEqual((result.text_frames, result.characters), (1, 5))

    def test_table_cell_text_is_counted_too(self):
        result = self._result(self._table("ab", "cde"))
        self.assertEqual(result.characters, 5)

    def test_a_table_counts_as_a_frame_because_that_is_what_it_becomes(self):
        result = self._result(self._table("x"))
        self.assertEqual(result.text_frames, 1)

    def test_images_and_shapes_are_counted_as_before(self):
        result = self._result(
            model.Image(), model.Rectangle(), model.Ellipse(), model.Polygon()
        )
        self.assertEqual((result.images, result.shapes), (1, 3))

    def test_a_table_is_not_counted_as_a_shape(self):
        result = self._result(self._table("x"))
        self.assertEqual(result.shapes, 0)


class UnnamedLanguageTest(unittest.TestCase):
    """What happens to a locale IDML has no display name for."""

    def document(self, *languages) -> model.Document:
        frame = model.TextFrame(x=0.0, y=0.0, width=200.0, height=100.0)
        paragraph = model.Paragraph()
        for language in languages:
            paragraph.spans.append(model.Span(text="text", language=language))
        frame.story.paragraphs.append(paragraph)
        page = model.Page(width=612.0, height=792.0)
        page.items.append(frame)
        return model.Document(pages=[page])

    def test_a_locale_with_no_idml_name_is_reported(self):
        document = self.document("en-AU", "en-AU")
        convert._check_unnamed_languages(document)
        self.assertEqual(len(document.warnings), 1)
        self.assertIn("en-AU (2)", document.warnings[0])

    def test_a_locale_idml_names_is_not_reported(self):
        document = self.document("nl-NL", "fr-CA")
        convert._check_unnamed_languages(document)
        self.assertEqual(document.warnings, [])

    def test_text_with_no_language_at_all_is_not_reported(self):
        document = self.document(None)
        convert._check_unnamed_languages(document)
        self.assertEqual(document.warnings, [])


if __name__ == "__main__":
    unittest.main()


def fake_measure(table, default=(0.70, 0.55, 0.50, "average"), above=0.8):
    """A stand-in for fontmetrics.measure keyed by the string measured.

    Sizing is tested against numbers the test states, so it needs no font
    installed -- which matters, because the two faces 46 of the corpus's
    headlines use are not installed on every machine.

    `above` is the share of the ink that sits above the baseline, which
    decides where in the band the first baseline goes. Stated once here
    rather than per entry, because only the tests about the baseline care
    what it is.
    """
    def measure(family, bold, italic, text):
        ink, width_per_glyph, advance, source = table.get(text, default)
        return fontmetrics.Metrics(
            ink_per_em=ink,
            ink_above_baseline_per_em=ink * above,
            width_per_em=width_per_glyph * max(len(text), 1),
            mean_advance_per_em=advance,
            source=source,
        )
    return measure


class WordArtFitTest(unittest.TestCase):
    """The size and condensation a band and a font decide between them."""

    def art(self, **kwargs):
        fields = dict(
            text="Kerkbode", font="Kerk Display", width=200.0, height=40.0,
            size=None, fitted=True, stretch=True,
        )
        fields.update(kwargs)
        return pubfile.WordArt(**fields)

    def test_the_size_fills_the_band_s_height(self):
        # One line, 40 pt of band, a font inking 0.8 of its em: 50 pt.
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, _scale, _source = convert._wordart_fit(
            self.art(), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 50.0)

    def test_each_line_gets_its_own_share_of_the_band(self):
        # Two lines in the same 40 pt band is 20 pt each.
        measure = fake_measure({
            "Kerk": (0.80, 0.50, 0.50, "font"),
            "bode": (0.80, 0.50, 0.50, "font"),
        })
        size, _scale, _source = convert._wordart_fit(
            self.art(text="Kerk\rbode"), ["Kerk", "bode"], measure
        )
        self.assertAlmostEqual(size, 25.0)

    def test_the_tallest_line_binds_the_size(self):
        # A line with descenders inks more of its em, so it decides.
        measure = fake_measure({
            "Kerk": (0.80, 0.50, 0.50, "font"),
            "bygy": (1.00, 0.50, 0.50, "font"),
        })
        size, _scale, _source = convert._wordart_fit(
            self.art(text="Kerk\rbygy"), ["Kerk", "bygy"], measure
        )
        self.assertAlmostEqual(size, 20.0)

    def test_the_width_becomes_a_horizontal_scale_not_a_smaller_size(self):
        # 8 glyphs at 0.5 em is 4 em; at 50 pt that sets 200 pt wide, in a
        # 200 pt band, so nothing is condensed.
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, scale, _source = convert._wordart_fit(
            self.art(), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 50.0)
        self.assertAlmostEqual(scale, 100.0)

    def test_a_headline_wider_than_its_band_is_condensed(self):
        # Same headline in a 100 pt band: it has to set at half the width.
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, scale, _source = convert._wordart_fit(
            self.art(width=100.0), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 50.0)
        self.assertAlmostEqual(scale, 50.0)

    def test_the_scale_is_clamped(self):
        measure = fake_measure({"II": (0.80, 0.10, 0.10, "font")})
        _size, scale, _source = convert._wordart_fit(
            self.art(text="II", width=400.0), ["II"], measure
        )
        self.assertAlmostEqual(scale, convert._MAX_HORIZONTAL_SCALE)

    def test_a_single_glyph_headline_takes_no_scale(self):
        # A dropped initial. Its band is the bounding box of slanted text
        # and on one glyph the slant's overhang is a fifth of it, so the
        # band's width is not a measure of the glyph's width. Publisher
        # draws the corpus's two at 95.7% and 97.0% where fitting the
        # advances to the band asks 121.2% and 151.9%
        # (research/wordart_stretch.py).
        measure = fake_measure({"D": (0.80, 0.50, 0.50, "font")})
        size, scale, _source = convert._wordart_fit(
            self.art(text="D", width=44.9), ["D"], measure
        )
        self.assertAlmostEqual(size, 50.0)
        self.assertIsNone(scale)

    def test_two_glyphs_are_still_fitted_to_the_band(self):
        # The rule above is about one glyph only: nothing measured says a
        # short word should stop being fitted.
        measure = fake_measure({"De": (0.80, 0.50, 0.50, "font")})
        _size, scale, _source = convert._wordart_fit(
            self.art(text="De", width=50.0), ["De"], measure
        )
        self.assertAlmostEqual(scale, 100.0)

    def test_spacing_widens_the_headline_before_it_is_fitted(self):
        # WordArt's multiple scales every advance, so a loose headline sets
        # wider and is condensed harder to reach the same band.
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        _size, scale, _source = convert._wordart_fit(
            self.art(spacing=1.2), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(scale, 100.0 / 1.2)

    def test_a_stated_size_is_overridden_when_the_shape_stretches(self):
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, _scale, _source = convert._wordart_fit(
            self.art(size=20.0, fitted=False), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 50.0)

    def test_a_stated_size_stands_when_the_shape_does_not_stretch(self):
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "font")})
        size, scale, _source = convert._wordart_fit(
            self.art(size=20.0, fitted=False, stretch=False), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 20.0)
        self.assertIsNone(scale)

    def test_metrics_that_are_not_exact_take_no_scale(self):
        measure = fake_measure({"Kerkbode": (0.80, 0.50, 0.50, "table")})
        _size, scale, source = convert._wordart_fit(
            self.art(), ["Kerkbode"], measure
        )
        self.assertIsNone(scale)
        self.assertEqual(source, "table")

    def test_without_exact_metrics_the_width_binds_the_size_as_before(self):
        # Today's rule exactly: min(height share / ink, band / natural width).
        # 8 glyphs at 0.55 em is 4.4 em; 100 pt / 4.4 em is 22.7 pt, which
        # is smaller than the 50 pt the height alone would give.
        measure = fake_measure({"Kerkbode": (0.80, 0.55, 0.50, "average")})
        size, scale, _source = convert._wordart_fit(
            self.art(width=100.0), ["Kerkbode"], measure
        )
        self.assertAlmostEqual(size, 100.0 / (0.55 * 8))
        self.assertIsNone(scale)


class WordArtTrackingTest(unittest.TestCase):
    def test_normal_spacing_states_no_tracking(self):
        self.assertIsNone(convert._wordart_tracking(None, 0.5))
        self.assertIsNone(convert._wordart_tracking(1.0, 0.5))

    def test_tracking_uses_the_measured_advance(self):
        # Loose (1.2) against a real mean advance of 0.44 em, not a guess
        # of 0.5: 0.2 x 0.44 x 1000.
        self.assertEqual(convert._wordart_tracking(1.2, 0.44), 88.0)

    def test_the_old_constant_is_what_an_unmeasured_font_still_gets(self):
        self.assertEqual(convert._wordart_tracking(1.2, 0.50), 100.0)
