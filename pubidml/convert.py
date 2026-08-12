"""Single-file conversion: .pub in, .idml (plus linked images) out."""

from __future__ import annotations

import io
import os
import time
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import idml, logsetup, metafile, model, textrepair

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


def dump_events(source: Path, pubdump: Path = PUBDUMP) -> str:
    """Run the libmspub shim and return its JSON event stream."""
    if not pubdump.exists():
        raise ConversionError(
            f"pubdump binary missing at {pubdump} — run 'make' first"
        )
    # Keep Windows from flashing a console window per file when the CLI is
    # driven from a shortcut or a future GUI wrapper.
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        completed = subprocess.run(
            [str(pubdump), str(source)],
            capture_output=True,
            timeout=300,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired:
        log.error("parser timed out after 300s on %s", source)
        raise ConversionError("timed out after 300s") from None
    except OSError as exc:
        log.error("could not launch parser %s: %s", pubdump, exc)
        raise ConversionError(f"could not launch parser: {exc}") from None

    stderr = completed.stderr.decode("utf-8", "replace").strip()
    log.debug(
        "parser exit=%d stdout=%d bytes stderr=%d bytes for %s",
        completed.returncode, len(completed.stdout), len(completed.stderr), source.name,
    )
    if stderr:
        # libmspub warns about structures it does not understand; these are
        # the single most useful clue when a file converts badly.
        log.info("parser stderr for %s: %s", source.name, stderr[:4000])

    if completed.returncode == EXIT_UNSUPPORTED:
        raise ConversionError("not a supported Publisher file (or corrupt)")
    if completed.returncode == EXIT_PARSE_FAILED:
        raise ConversionError("libmspub could not parse the document")
    if completed.returncode != 0:
        raise ConversionError(stderr or f"pubdump exited {completed.returncode}")

    return completed.stdout.decode("utf-8", "replace")


def _rasterise_metafiles(document: model.Document) -> None:
    """Turn EMF artwork into PNG, or drop it with an accurate warning.

    Only non-empty metafiles reach this point; the model already discards
    Publisher's empty placeholder stubs.
    """
    for page in document.pages:
        survivors = []
        for item in page.items:
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
        page.items = survivors


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
    destination = Path(destination)
    result = Result(source=source)
    started = time.monotonic()
    log.info("converting %s -> %s", source, destination)

    try:
        events = dump_events(source, pubdump)
    except ConversionError as exc:
        result.error = str(exc)
        log.error("%s: %s", source.name, exc)
        return result

    try:
        document = model.build(io.StringIO(events))
    except Exception as exc:  # malformed event stream
        result.error = f"model build failed: {exc}"
        log.exception("%s: model build failed", source.name)
        return result

    if not document.pages:
        result.error = "document contains no pages"
        log.error("%s: no pages in document", source.name)
        return result

    textrepair.repair_document(document, codepage)
    _rasterise_metafiles(document)
    _check_overset_text(document)

    writer = idml.IdmlWriter(
        document,
        image_dir_name=f"{destination.stem}_images",
        wrap_images=wrap_images,
    )
    try:
        writer.write(destination)
    except Exception as exc:
        result.error = f"IDML write failed: {exc}"
        log.exception("%s: IDML write failed", source.name)
        return result

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

    return result
