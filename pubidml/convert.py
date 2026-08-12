"""Single-file conversion: .pub in, .idml (plus linked images) out."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import idml, logsetup, metafile, model, pubfile, textrepair

log = logsetup.get_logger("convert")


def _locate_pubdump() -> Path:
    """Find the parser binary in both a source tree and a frozen bundle.

    PyInstaller unpacks bundled data into a temporary directory exposed as
    sys._MEIPASS, so a frozen build looks there first and falls back to the
    repository layout when running from source.
    """
    name = "pubdump.exe" if os.name == "nt" else "pubdump"
    roots = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.append(Path(bundle) / "bin")
    roots.append(Path(__file__).resolve().parent.parent / "bin")

    for root in roots:
        candidate = root / name
        if candidate.exists():
            return candidate
    return roots[0] / name


PUBDUMP = _locate_pubdump()

# pubdump exit codes
EXIT_UNSUPPORTED = 3
EXIT_PARSE_FAILED = 4
EXIT_WRITE_FAILED = 5

PARSE_TIMEOUT_S = 300


class ConversionError(Exception):
    """Raised when a .pub file cannot be converted at all."""


@dataclass
class Result:
    source: Path
    output: Optional[Path] = None
    pages: int = 0
    text_frames: int = 0
    images: int = 0
    shapes: int = 0
    characters: int = 0
    fonts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def needs_review(self) -> bool:
        """Flag files a human should look at before trusting the output."""
        return bool(self.warnings) or self.pages == 0 or (
            self.text_frames == 0 and self.images == 0
        )


def parse_document(source: Path, pubdump: Path = PUBDUMP) -> model.Document:
    """Run the libmspub shim and replay its event stream into a Document.

    The stream is consumed line by line rather than buffered whole. A
    picture-heavy Publisher file emits tens of megabytes of base64, and
    holding the raw bytes, the decoded string and the decoded images at
    once put peak memory at roughly three times the file size — per worker,
    across the whole thread pool.
    """
    if not pubdump.exists():
        raise ConversionError(
            f"pubdump binary missing at {pubdump} — run 'make' first"
        )
    # Keep Windows from flashing a console window per file when the CLI is
    # driven from a shortcut or a future GUI wrapper.
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # stderr goes to a file rather than a pipe: nothing drains it while the
    # event stream is being read, and libmspub is chatty enough about
    # structures it does not understand to fill a pipe buffer and deadlock.
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                [str(pubdump), str(source)],
                stdout=subprocess.PIPE,
                stderr=errors,
                creationflags=creation_flags,
            )
        except OSError as exc:
            log.error("could not launch parser %s: %s", pubdump, exc)
            raise ConversionError(f"could not launch parser: {exc}") from None

        # A deadline inside the read loop cannot fire while the loop is
        # blocked in readline, so the parser is killed from a timer instead.
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            process.kill()

        watchdog = threading.Timer(PARSE_TIMEOUT_S, expire)
        watchdog.start()

        document = None
        build_error = None
        try:
            # The context manager closes the pipes and reaps the child on
            # every path, including one where building the reader itself
            # fails — otherwise a parser could be left running.
            with process:
                stream = io.TextIOWrapper(
                    process.stdout, encoding="utf-8", errors="replace"
                )
                try:
                    document = model.build(stream)
                except Exception as exc:  # malformed event stream
                    build_error = exc
                finally:
                    stream.close()
        finally:
            watchdog.cancel()
        returncode = process.returncode

        errors.seek(0)
        stderr = errors.read().decode("utf-8", "replace").strip()

    log.debug("parser exit=%d for %s", returncode, source.name)
    if stderr:
        # libmspub warns about structures it does not understand; these are
        # the single most useful clue when a file converts badly.
        log.info("parser stderr for %s: %s", source.name, stderr[:4000])

    if timed_out.is_set():
        log.error("parser timed out after %ds on %s", PARSE_TIMEOUT_S, source)
        raise ConversionError(f"timed out after {PARSE_TIMEOUT_S}s")
    if build_error is not None:
        log.error("%s: malformed event stream: %s", source.name, build_error)
        raise ConversionError(f"malformed event stream: {build_error}")
    if returncode == EXIT_UNSUPPORTED:
        raise ConversionError("not a supported Publisher file (or corrupt)")
    if returncode == EXIT_PARSE_FAILED:
        raise ConversionError("libmspub could not parse the document")
    if returncode == EXIT_WRITE_FAILED:
        raise ConversionError("parser could not write its event stream")
    if returncode != 0:
        raise ConversionError(stderr or f"pubdump exited {returncode}")
    if not document.complete:
        # Syntactically valid but short: the stream was cut on a line
        # boundary, so every downstream count would be quietly wrong.
        raise ConversionError("parser output was truncated")

    return document


def _rasterise_metafiles(document: model.Document) -> None:
    """Turn EMF artwork into PNG, or drop it with an accurate warning.

    Only non-empty metafiles reach this point; the model already discards
    Publisher's empty placeholder stubs.
    """
    for page in document.pages:
        page.items = _rasterise_items(page.items, document)


def _rasterise_items(items: List[model.Item], document: model.Document) -> List[model.Item]:
    """Rasterise metafiles at any depth, returning the items that survive.

    Groups are descended into: every other document-wide pass uses
    model._walk, and a shallow pass here let grouped artwork through
    un-rasterised and un-reported, to be written out as a broken link.
    """
    survivors: List[model.Item] = []
    for item in items:
        if isinstance(item, model.Group):
            item.children = _rasterise_items(item.children, document)
            survivors.append(item)
            continue

        if not (
            isinstance(item, model.Image)
            and item.mime_type in metafile.METAFILE_MIME_TYPES
        ):
            survivors.append(item)
            continue

        png = metafile.to_png(item.data, item.width, item.height)
        if png:
            item.data = png
            item.mime_type = "image/png"
            survivors.append(item)
            continue

        info = metafile.inspect(item.data)
        if metafile.converters_available():
            reason = "conversion failed"
        else:
            reason = "install emf2svg-conv and ImageMagick to convert it"
        document.warnings.append(
            f"{info.kind.upper()} artwork dropped "
            f"({info.drawing_records} drawing records): {reason}"
        )
    return survivors


def _is_page_background(item: model.Item, page: model.Page) -> bool:
    """True for the whole-page rectangle libmspub synthesises for a fill.

    writePageBackground emits one for the master and one for the page
    before any real shape, so they have to be stepped over before the
    master's own shapes can be counted off.
    """
    if not isinstance(item, model.Rectangle):
        return False
    return (
        abs(item.x) < 1.0
        and abs(item.y) < 1.0
        and abs(item.width - page.width) < 1.0
        and abs(item.height - page.height) < 1.0
    )


def _master_items(page: model.Page, shape_count: int) -> List[model.Item]:
    """The leading items on a page that came from its master.

    libmspub replays a master's shapes onto each page ahead of that page's
    own (MSPUBCollector::writePage), so they are the first `shape_count`
    items once the synthesised backgrounds are stepped over.
    """
    start = 0
    while start < len(page.items) and _is_page_background(page.items[start], page):
        start += 1
    return page.items[start:start + shape_count]


def _shape_signature(item: model.Item) -> tuple:
    """Enough of an item to tell whether two pages got the same one."""
    return (
        type(item).__name__,
        round(item.x, 3), round(item.y, 3),
        round(item.width, 3), round(item.height, 3),
    )


def _resolve_page_numbers(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> None:
    """Replace Publisher's '#' page-number placeholder with the real number.

    Publisher stores a page-number field as a bare '#' in the text and
    libmspub has no field handling at all, so a footer reads '#' on every
    page. Two independent facts make substituting it safe rather than a
    guess: the document must carry a field table (no TOKN chunk means
    every '#' in it was typed), and the '#' must sit in content the file
    says came from a master.

    Anything that does not line up leaves the text exactly as it was.
    """
    if structure is None or not structure.has_fields:
        return
    if len(structure.pages) != len(document.pages):
        # The side-channel could not be aligned with what libmspub emitted,
        # so nothing here can be attributed with confidence.
        log.info("page structure did not align; leaving '#' alone")
        return

    attributed = []
    for index, page in enumerate(document.pages):
        master = structure.master_for(index)
        if master is None or not master.shape_count:
            attributed.append(None)
            continue
        attributed.append(_master_items(page, master.shape_count))

    # Pages sharing a master must have been given the same shapes. If they
    # were not, the attribution is wrong and no substitution is justified.
    by_master: dict = {}
    for index, items in enumerate(attributed):
        if items is None:
            continue
        master = structure.master_for(index)
        signature = tuple(_shape_signature(i) for i in items)
        if by_master.setdefault(master.seq, signature) != signature:
            log.info("master content differs between pages; leaving '#' alone")
            return

    replaced = 0
    for index, items in enumerate(attributed):
        for item in items or ():
            if not isinstance(item, model.TextFrame):
                continue
            for paragraph in item.story.paragraphs:
                for span in paragraph.spans:
                    if "#" in span.text:
                        span.text = span.text.replace("#", str(index + 1))
                        replaced += 1

    if replaced:
        document.warnings.append(
            f"page-number field resolved on {replaced} frame(s): Publisher "
            f"stores it as '#', which would otherwise read '#' on every page"
        )


def _check_overset_text(document: model.Document) -> None:
    """Flag text frames far too small to show the text they contain.

    libmspub sometimes reports a degenerate size for a text object — one
    file in the sample set has a 5.5 x 5.7 pt frame carrying 3,869
    characters, which Affinity renders as an empty box. Guessing the
    intended geometry would be inventing layout, so the frame is left
    alone and the operator is told exactly which file needs a human.
    """
    for page in document.pages:
        for item in model._walk(page.items):
            if not isinstance(item, model.TextFrame):
                continue
            characters = sum(
                len(span.text)
                for paragraph in item.story.paragraphs
                for span in paragraph.spans
            )
            if characters < 20:
                continue

            sizes = [
                span.size_pt
                for paragraph in item.story.paragraphs
                for span in paragraph.spans
                if span.size_pt
            ]
            point_size = (sum(sizes) / len(sizes)) if sizes else 10.0
            # Rough capacity: average glyph advance ~0.5em, leading ~1.2em.
            columns = item.width / max(1e-6, 0.5 * point_size)
            rows = item.height / max(1e-6, 1.2 * point_size)
            capacity = max(0.0, columns * rows)

            if characters > 10 * max(capacity, 1.0):
                document.warnings.append(
                    f"text frame too small for its content: {characters} characters "
                    f"in a {item.width:.1f}x{item.height:.1f}pt frame "
                    f"(libmspub reported a degenerate size; resize it in Affinity)"
                )


def convert(
    source: Path,
    destination: Path,
    pubdump: Path = PUBDUMP,
    codepage: Optional[str] = "auto",
    wrap_images: bool = True,
) -> Result:
    """Convert one .pub file to an .idml package.

    Linked images are written to a sibling folder named after the output
    file, so `report.idml` is accompanied by `report_images/`.
    """
    source = Path(source)
    result = Result(source=source)
    try:
        _convert(result, source, Path(destination), pubdump, codepage, wrap_images)
    except ConversionError as exc:
        result.error = str(exc)
        log.error("%s: %s", source.name, exc)
    except Exception as exc:
        # A worker must never propagate. cli.py collects results across a
        # whole batch, and one escaped exception would discard every result
        # gathered so far along with the report that makes them usable.
        result.error = f"unexpected error: {exc.__class__.__name__}: {exc}"
        log.exception("%s: unexpected error", source.name)
    return result


def _convert(
    result: Result,
    source: Path,
    destination: Path,
    pubdump: Path,
    codepage: Optional[str],
    wrap_images: bool,
) -> None:
    started = time.monotonic()
    log.info("converting %s -> %s", source, destination)

    document = parse_document(source, pubdump)

    if not document.pages:
        raise ConversionError("document contains no pages")

    textrepair.repair_document(document, codepage)
    _resolve_page_numbers(document, pubfile.read_structure(source))
    _rasterise_metafiles(document)
    _check_overset_text(document)

    writer = idml.IdmlWriter(
        document,
        image_dir_name=f"{destination.stem}_images",
        wrap_images=wrap_images,
    )
    try:
        writer.write(destination)
    except idml.MalformedPartError as exc:
        raise ConversionError(str(exc)) from exc
    except OSError as exc:
        raise ConversionError(f"IDML write failed: {exc}") from exc

    result.output = destination
    result.pages = len(document.pages)
    result.fonts = document.fonts
    result.warnings = list(document.warnings)

    for page in document.pages:
        for item in model._walk(page.items):
            if isinstance(item, model.TextFrame):
                result.text_frames += 1
                for paragraph in item.story.paragraphs:
                    for span in paragraph.spans:
                        result.characters += len(span.text)
            elif isinstance(item, model.Image):
                result.images += 1
            elif isinstance(item, (model.Rectangle, model.Ellipse, model.Polygon, model.Path)):
                result.shapes += 1

    log.info(
        "%s: ok in %.2fs - %d pages, %d frames, %d images, %d shapes, %d chars, fonts=%s",
        source.name, time.monotonic() - started, result.pages, result.text_frames,
        result.images, result.shapes, result.characters, ", ".join(result.fonts) or "none",
    )
    for warning in result.warnings:
        log.warning("%s: %s", source.name, warning)
