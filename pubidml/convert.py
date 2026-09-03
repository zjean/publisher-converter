"""Single-file conversion: .pub in, one self-contained .idml out."""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import tempfile
import threading
import time
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import (
    fontmetrics, idml, logsetup, metafile, model, pubfile, textrepair, wmf,
)

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
    wordart: int = 0
    #: Whether the document was laid out as reader's spreads, and whether
    #: that was read from the file rather than asked for on the command
    #: line. A layout decided rather than requested has to be visible.
    facing_pages: bool = False
    facing_detected: bool = False
    fonts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    #: True for a file passed over because its .idml was already there. It
    #: still gets a Result so that it reaches the report: the CSV is written
    #: from scratch on every run, so a file absent from the results is a file
    #: absent from the report -- and a collection converted once and then run
    #: again would otherwise be described by a header row and nothing else.
    #: None of the counts mean anything on one of these; nothing was read.
    skipped: bool = False

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


@dataclass
class _DroppedArtwork:
    """One distinct piece of artwork that could not be converted."""

    kind: str
    drawing_records: int
    reason: str
    copies: int = 0


@dataclass
class _PartialArtwork:
    """Artwork that converted, but not every drawing record in it."""

    converted: int
    total: int
    copies: int = 0


def _rasterise_metafiles(document: model.Document) -> None:
    """Turn metafile artwork into images, or drop it with an accurate warning.

    Artwork is accounted for by content rather than by frame: Publisher
    repeats one logo across every page of a newsletter, and reporting the
    same loss 64 times buries everything else in the report.
    """
    dropped: Dict[bytes, _DroppedArtwork] = {}
    partial: Dict[bytes, _PartialArtwork] = {}
    for page in document.pages:
        page.items = _rasterise_items(page.items, dropped, partial)
    # Masters are extracted before this runs -- they have to be, since
    # rasterisation can drop artwork and that would break the shape count
    # the attribution relies on -- so they need visiting separately.
    for master in document.masters:
        master.items = _rasterise_items(master.items, dropped, partial)

    for entry in dropped.values():
        copies = f", {entry.copies} copies" if entry.copies > 1 else ""
        document.warnings.append(
            f"{entry.kind} artwork dropped "
            f"({entry.drawing_records} drawing record(s){copies}): {entry.reason}"
        )
    for entry in partial.values():
        copies = f", {entry.copies} copies" if entry.copies > 1 else ""
        document.warnings.append(
            f"WMF artwork partly converted ({entry.converted} of "
            f"{entry.total} drawing record(s) became shapes{copies}): "
            f"the rest have no IDML equivalent"
        )


def _drop_reason(info: metafile.MetafileInfo) -> str:
    """Why this artwork was lost, in terms of what was actually attempted."""
    if info.kind == "wmf":
        # Nothing was tried at all: emf2svg-conv reads EMF only. Blaming a
        # failed conversion here named the wrong culprit, and did so
        # whenever the tools merely happened to be installed.
        return "no WMF converter available (emf2svg-conv reads EMF only)"
    if metafile.converters_available():
        return "conversion failed"
    return "install emf2svg-conv and ImageMagick to convert it"


def _rasterise_items(
    items: List[model.Item],
    dropped: Dict[bytes, _DroppedArtwork],
    partial: Dict[bytes, _PartialArtwork],
) -> List[model.Item]:
    """Convert metafiles at any depth, returning the items that survive.

    Groups are descended into: every other document-wide pass uses
    model._walk, and a shallow pass here let grouped artwork through
    unconverted and unreported, to be written out as a broken link.
    """
    survivors: List[model.Item] = []
    for item in items:
        if isinstance(item, model.Group):
            item.children = _rasterise_items(item.children, dropped, partial)
            survivors.append(item)
            continue

        if not (
            isinstance(item, model.Image)
            and item.mime_type in metafile.METAFILE_MIME_TYPES
        ):
            survivors.append(item)
            continue

        info = metafile.inspect(item.data)
        if info.is_empty:
            # Publisher's placeholder stub. The model drops these on the
            # bitmap-fill route already; checking again here keeps the two
            # entry points agreeing and keeps the report free of artwork
            # that never existed.
            continue

        # A bitmap in a metafile envelope needs no external tool, so it is
        # tried first: it works where nothing is installed, and it avoids
        # resampling a photograph through SVG. It also has to come before
        # the vector path, or a wrapped photograph would be traced as
        # though it were line art.
        unwrapped = metafile.embedded_bitmap(item.data)
        if unwrapped:
            item.data, item.mime_type = unwrapped
            survivors.append(item)
            continue

        # WMF line art becomes real IDML paths. No external tool can do
        # this, and vectors beat a raster in a layout anyway.
        artwork = wmf.to_items(item.data, item.x, item.y, item.width, item.height)
        if artwork:
            survivors.append(model.Group(
                x=item.x, y=item.y, width=item.width, height=item.height,
                children=artwork.items,
            ))
            if artwork.unsupported:
                # Counted by content, not by frame: one partly-converted
                # logo repeated across a newsletter is one problem.
                key = hashlib.sha1(item.data).digest()
                entry = partial.get(key)
                if entry is None:
                    entry = _PartialArtwork(
                        converted=artwork.converted,
                        total=artwork.converted + artwork.unsupported,
                    )
                    partial[key] = entry
                entry.copies += 1
            continue

        png = metafile.to_png(item.data, item.width, item.height)
        if png:
            item.data = png
            item.mime_type = "image/png"
            survivors.append(item)
            continue

        key = hashlib.sha1(item.data).digest()
        entry = dropped.get(key)
        if entry is None:
            entry = _DroppedArtwork(
                kind=info.kind.upper(),
                drawing_records=info.drawing_records,
                reason=_drop_reason(info),
            )
            dropped[key] = entry
        entry.copies += 1
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


def _carries_page_number(item: model.Item, has_fields: bool) -> bool:
    """True for a master frame holding what is probably a page-number field."""
    if not has_fields or not isinstance(item, model.TextFrame):
        return False
    return any(
        "#" in span.text
        for paragraph in item.story.paragraphs
        for span in paragraph.spans
    )


def _agreed_master(
    structure: "pubfile.FileStructure", chunks: List["pubfile.PageStructure"]
) -> Optional[tuple]:
    """What the masters these page chunks apply agree on.

    Asked of the chunks a page could still be, so that a page the mapping
    cannot place is not thereby lost: whichever of them it turns out to be,
    anything they all say is true of it. They may agree on how many shapes
    the master holds -- which is all it takes to know how much of the page
    came from one -- without agreeing on which master that was.

    Returns (shape count, master seq or None), or None where they agree on
    nothing useful. A chunk applying no master at all is such a case: its
    page has no master content, and so nothing to agree about.
    """
    masters = [structure.master_of_chunk(chunk.seq) for chunk in chunks]
    if not masters or any(master is None for master in masters):
        return None
    counts = {master.shape_count for master in masters}
    if len(counts) != 1:
        return None
    seqs = {master.seq for master in masters}
    return counts.pop(), (seqs.pop() if len(seqs) == 1 else None)


def _attribute_masters(
    document: model.Document, structure: "pubfile.FileStructure"
) -> Optional[List[tuple]]:
    """Which leading items on each page came from that page's master.

    Each page is asked of the page chunk its own shapes identify, never of
    the chunk sitting at its index: the file's chunk order is a permutation
    of libmspub's page order, and indexing hands 16 of `1338 kerkbode.pub`'s
    28 pages the other master of the pair (`backlog.md` §14).

    A page no chunk identifies falls back to what the chunks still going
    spare agree on, which is a fact rather than a guess -- and to nothing
    where they disagree. Returns None when the whole attribution cannot be
    trusted, because pages sharing a master were not given the same shapes.
    """
    by_seq = {chunk.seq: chunk for chunk in structure.pages}
    settled = _page_by_chunk(document, structure)
    claimed: Dict[int, List[int]] = {}
    for chunk, index in settled.items():
        claimed.setdefault(index, []).append(chunk)

    # The fallback needs the two halves to hold the same number of pages,
    # since it rests on every page being one of the chunks left over. Where
    # they do not, an unplaced page is simply unplaced.
    spare = None
    if len(structure.pages) == len(document.pages):
        spare = _agreed_master(
            structure, [c for seq, c in by_seq.items() if seq not in settled]
        )

    attributed: List[tuple] = []
    for index, page in enumerate(document.pages):
        chunks = claimed.get(index, [])
        if len(chunks) == 1:
            agreed = _agreed_master(structure, [by_seq[chunks[0]]])
        elif chunks:
            # Two chunks measured onto one page. The chunks are disjoint, so
            # one of them is wrong and there is no telling which.
            agreed = None
        else:
            agreed = spare
        if agreed is None or not agreed[0]:
            attributed.append(([], None))
            continue
        count, seq = agreed
        items = _master_items(page, count)
        # The sheet is part of the identity as well as the shapes: content
        # can only be shared by pages of one size, or lifting it would put
        # it on a page of the wrong dimensions.
        signature = (
            tuple(_shape_signature(i) for i in items),
            round(page.width, 3),
            round(page.height, 3),
        )
        # No seq means the chunks agreed on the count but not on the master.
        # The items are known; where they belong is not, so they get no key
        # and stay where they are.
        attributed.append((items, None if seq is None else (seq, signature)))

    # A Publisher master covers either one page or a facing pair, so one
    # master sequence number may legitimately present two different
    # layouts -- a left and a right. More than two means the attribution
    # is wrong rather than the document unusual, and nothing should move.
    variants: dict = {}
    for _items, key in attributed:
        if key is not None:
            variants.setdefault(key[0], set()).add(key[1])
    for seq, signatures in variants.items():
        if len(signatures) > 2:
            log.info(
                "master %s presents %d different layouts; leaving pages flattened",
                seq, len(signatures),
            )
            return None
    return attributed


def _note_restored_blanks(document: model.Document, restored: int) -> None:
    """Say which pages were put back, since they are put back empty.

    Publisher prints such a page with whatever its master carries -- a
    number, a running head -- and the master is applied per page from the
    items libmspub drew, of which a blank page has none. So the page is the
    right page in the right place, and bare. Worth a look, and worth
    knowing about before the document is imposed.
    """
    if not restored:
        return
    document.warnings.append(
        f"{restored} blank page(s) restored: libmspub reports only pages "
        "carrying shapes of their own, so a page whose content comes from "
        "its master alone never arrives. The file's page order says where "
        "they belong and they were put back there, empty -- without them "
        "every later page carries the wrong number, and a booklet imposes "
        "with the wrong pages facing. Check they are blank in Publisher too"
    )


#: A sheet carrying bleed and crop marks is bigger than the pages on it,
#: so the sheet only has to *reach* two pages, not match them. It must not
#: reach three: that is an imposition reader's spreads cannot describe.
_TWO_UP_SLACK = 1.0


def _note_detected_facing(
    document: model.Document,
    facing: bool,
    structure: Optional["pubfile.FileStructure"],
) -> None:
    """Say that the spread layout was decided rather than asked for.

    The layout is the most visible thing about a converted document, so a
    reading that changes it has to name the evidence it changed it on --
    and it is evidence rather than the file's word (`_detect_facing_pages`).
    """
    if not facing or structure is None or structure.print_sheet is None:
        return
    sheet_width, sheet_height = structure.print_sheet
    page = document.pages[0]
    document.warnings.append(
        f"laid out as facing spreads, read from the file rather than asked "
        f"for: it states a {sheet_width:.1f} x {sheet_height:.1f}pt print "
        f"sheet, which reaches two {page.width:.1f}pt pages side by side, "
        f"and {len(document.pages)} pages is a count a saddle stitch folds. "
        f"Publisher's own layout type is not readable (actions.md 12), so "
        f"this is the shape of a booklet and not the file's word for one -- "
        f"pass --no-facing-pages if it is not one"
    )


def _detect_facing_pages(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> bool:
    """Does the file describe a booklet?

    Publisher's own layout type is not readable. libmspub has no fold or
    facing concept anywhere -- `parseDocumentChunk` reads the document size
    and the page list and skips every other block -- and the one field that
    separates the corpus's booklets from the rest sits in a printer devmode
    blob rather than in the document (actions.md 12).

    So this reads the shape of a booklet rather than the file's word for
    one, from the two things the file does state plainly: a print sheet
    that reaches two of these pages side by side, and a page count a saddle
    stitch could fold. In the newsletters the sheet is 914.0 x 681.4pt
    against a 421.0 x 595.0pt page, which is what Publisher's own exported
    PDF of them is imposed onto.

    Both halves are needed and neither is enough. A count of four alone
    describes any four-page document; a two-up sheet alone describes a flyer
    printed two to a page. Pages that differ in size are never guessed at:
    reader's spreads assume one sheet throughout.

    A folded card has this shape too, and would be laid out facing -- which
    is what a folded card wants. `--no-facing-pages` is the way out.
    """
    if structure is None or structure.print_sheet is None:
        return False
    if not document.pages:
        return False
    width = document.pages[0].width
    height = document.pages[0].height
    if any(page.width != width or page.height != height for page in document.pages):
        return False
    # Only a multiple of four folds into sheets of four pages, and the
    # blank pages have already been put back by the time this is asked, so
    # the count is the document's rather than libmspub's.
    if len(document.pages) % 4 or not len(document.pages):
        return False
    sheet_width, sheet_height = structure.print_sheet
    return (
        sheet_width >= 2 * width - _TWO_UP_SLACK
        and sheet_width < 3 * width - _TWO_UP_SLACK
        and sheet_height >= height - _TWO_UP_SLACK
    )


def _restore_blank_pages(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> int:
    """Put back the pages libmspub dropped for carrying no shapes.

    A page whose content comes from its master alone never reaches the
    event stream (`pubfile._blank_pages`), and the gap is usually in the
    middle of the document rather than at its end. Left alone it costs
    every later page its number, and -- laid out facing -- its side of the
    spine, which is what makes a booklet come out imposed wrong.

    The positions are offsets into the finished document, so they are only
    meaningful if the pages that did arrive are the ones the file says
    arrived. Where the two halves disagree on that, nothing moves: a page
    inserted in the wrong place is worse than one missing, because the
    missing one is visible and the misplaced one is not.

    A restored page is added to the structure as well as to the document.
    The two are read side by side -- `_attribute_masters` falls back to the
    chunks going spare only while they hold the same number of pages -- and
    growing one without the other would cost the document its masters.
    """
    if structure is None or not structure.blank_pages:
        return 0
    if len(structure.pages) != len(document.pages):
        log.info(
            "%d page(s) reported against %d in the file; leaving the blanks out",
            len(document.pages), len(structure.pages),
        )
        return 0

    total = len(document.pages) + len(structure.blank_pages)
    if any(not 1 <= position <= total for position, _page in structure.blank_pages):
        return 0

    for position, chunk in sorted(structure.blank_pages):
        index = position - 1
        # The sheet the document is already on, taken from the page the
        # blank is being pushed down or, at the very end, the one before it.
        neighbour = document.pages[min(index, len(document.pages) - 1)]
        document.pages.insert(
            index, model.Page(width=neighbour.width, height=neighbour.height)
        )
        structure.pages.append(chunk)
    log.info("restored %d blank page(s) libmspub did not report",
             len(structure.blank_pages))
    return len(structure.blank_pages)


def _master_name(index: int) -> str:
    """A, B, ... Z, AA, AB, ... -- the name of the nth master.

    The name is the master's identity all the way out to the package: it
    becomes the MasterSpread's `Self`, the part's filename and what every
    page applying it names. Wrapping back onto 'A' after the 26th would
    have two masters claim one part, and the second one written would
    silently take the first one's pages with it.
    """
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    name = ""
    while True:
        index, remainder = divmod(index, len(letters))
        name = letters[remainder] + name
        if not index:
            return name
        index -= 1


def _apply_master_pages(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> None:
    """Lift repeated master content onto real masters, and fix page numbers.

    libmspub replays a master's shapes onto every page and says nothing
    about where they came from, so a footer arrives fifteen times over. The
    .pub itself says which page is a master and which master each page
    applies, so the copies can be reduced back to one.

    Frames holding a page-number field stay behind. Publisher stores that
    field as a bare '#', and a single copy on a master cannot read '1' on
    one page and '2' on the next -- so those keep their per-page position
    and get the real number substituted instead. A '#' is only ever touched
    when the document carries a field table, since a document with none
    cannot contain a field and every '#' in it was typed.
    """
    if structure is None:
        return
    attributed = _attribute_masters(document, structure)
    if attributed is None:
        return

    masters: dict = {}
    lifted = numbered = flattened = 0

    for index, (items, key) in enumerate(attributed):
        if not items:
            continue
        page = document.pages[index]

        # Compared by identity: two master items can be genuinely equal as
        # values (two identical rules, say) and `in` would confuse them.
        keep_ids = {
            id(i) for i in items if _carries_page_number(i, structure.has_fields)
        }
        move = [i for i in items if id(i) not in keep_ids]
        keep = [i for i in items if id(i) in keep_ids]

        for frame in keep:
            for paragraph in frame.story.paragraphs:
                for span in paragraph.spans:
                    if "#" in span.text:
                        span.text = span.text.replace("#", str(index + 1))
                        numbered += 1

        if not move:
            continue

        if key is None:
            # The page's own chunk was never identified, and the chunks it
            # could be name different masters (§14). The number above needed
            # only the page's index, but lifting needs to know whose content
            # this is: two shapes alike enough to share a signature can
            # still belong to two different masters, and merging them would
            # put one master's content on the other's pages.
            flattened += len(move)
            continue

        # Keyed by layout, not just by master: a facing-pages master holds
        # a left and a right page, which become two IDML masters. The page
        # looks the same either way; only the editing structure differs.
        if key not in masters:
            master = model.Master(
                name=_master_name(len(masters)),
                width=page.width,
                height=page.height,
                items=list(move),
            )
            masters[key] = master
            document.masters.append(master)
            lifted += len(move)

        page.master = masters[key].name
        move_ids = {id(i) for i in move}
        page.items = [i for i in page.items if id(i) not in move_ids]

    if numbered:
        document.warnings.append(
            f"page-number field resolved on {numbered} frame(s): Publisher "
            f"stores it as '#', which would otherwise read '#' on every page"
        )
    if lifted:
        document.warnings.append(
            f"{lifted} repeated item(s) moved onto {len(masters)} master "
            f"page(s) rather than copied onto every page"
        )
    if flattened:
        document.warnings.append(
            f"{flattened} repeated item(s) left copied onto their pages: the "
            f"file does not say which page libmspub drew them on, and the "
            f"pages it could be apply different masters"
        )


def _apply_cell_insets(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> None:
    """Give every table cell the padding Publisher recorded for it.

    libmspub reads a cell's row and column and stops there -- its own
    source marks the rest of the record "width/height of content +
    margins?" and skips it -- so a converted table arrives with whatever
    padding Affinity defaults to. Publisher's is consistently tighter,
    and in the newsletters' layout grids the gutter columns are an eighth
    of an inch wide, narrower than two default insets put together, so
    the default leaves them nothing to set text in.

    The file itself carries four insets per cell, and the table's own
    grid is what ties a chunk back to the table in the event stream.

    The same record states the cell's **vertical alignment**, which
    libmspub also stops short of, so that is read here too: field 0x07,
    1 for centre and 2 for bottom, left out where the cell is top-aligned.

    Reading a cell's record settles its *rules* as well, which is why they
    are set here rather than in a pass of their own: the records state
    padding, alignment, the cell's own span and two cached text extents,
    and no rule and no shade in any of the 1,260 across the corpus -- and
    a field this format leaves out is absent rather than defaulted. So a
    cell whose record we have read is a cell Publisher recorded no lines
    for *in that record*, and saying
    nothing about its edges is the one answer that is certainly wrong: the
    reader then draws its own line around every cell, in a colour and a
    weight the .pub never states, across the layout grids these documents
    are built on.

    Publisher does rule tables, and the lines turn out to live in the
    Escher stream as a shape per shaded cell and a shape per ruled edge
    (`actions.md` §11). Until those are read, an unruled table is still the
    closer of the two answers, and the warning below says so.
    """
    if structure is None or not structure.tables:
        return

    tables = cells = silenced = 0
    for item in document.all_items():
        if not isinstance(item, model.Table):
            continue
        insets = structure.cell_insets(item.column_widths, item.row_heights)
        if insets is None:
            continue
        alignments = (
            structure.cell_alignments(item.column_widths, item.row_heights) or {}
        )
        drawn = structure.cell_rules(item.column_widths, item.row_heights) or {}
        shades = structure.cell_shades(item.column_widths, item.row_heights) or {}
        tables += 1
        for cell in item.cells:
            found = insets.get((cell.row, cell.column))
            if found is None:
                continue
            cell.insets = model.CellInsets(*found)
            cell.unruled = True
            cell.vertical_align = alignments.get((cell.row, cell.column))
            cell.rules = _rules_of(cell, drawn)
            cell.shade = shades.get((cell.row, cell.column))
            cells += 1
        if not drawn and not shades:
            silenced += 1

    if tables:
        log.info(
            "cell insets read for %d table(s), %d cell(s); %d drawn",
            tables, cells, tables - silenced,
        )
    if silenced:
        document.warnings.append(
            f"{silenced} table(s) written with every cell rule off: the "
            f"file draws no line and no shade on any of their cells, and "
            f"a cell edge left unstated is one the reader rules itself. "
            f"Publisher keeps cell rules and shading in the Escher stream "
            f"rather than in the cell records, so this is what that stream "
            f"states — check them against the original before adding lines "
            f"by hand"
        )


def _rules_of(cell: model.TableCell, drawn: dict) -> Dict[str, model.CellRule]:
    """The lines drawn on one cell, out of the lines drawn on the grid.

    A rule is stated per grid position and a merged cell covers several,
    so its four sides are the sides of its *footprint*: the top of every
    column it spans, the bottom of the last row, and so on. A line lying
    inside the footprint is one IDML cannot draw -- there is a single
    stroke per side -- and is left out rather than promoted to a whole
    side Publisher never ruled.
    """
    rows = range(cell.row, cell.row + cell.row_span)
    columns = range(cell.column, cell.column + cell.column_span)
    sides = {
        "top": [(cell.row, column) for column in columns],
        "bottom": [(cell.row + cell.row_span - 1, column) for column in columns],
        "left": [(row, cell.column) for row in rows],
        "right": [(row, cell.column + cell.column_span - 1) for row in rows],
    }
    found: Dict[str, model.CellRule] = {}
    for side, positions in sides.items():
        for position in positions:
            rule = drawn.get((*position, side))
            if rule is not None:
                found[side] = model.CellRule(rule.weight, rule.color)
                break
    return found


def _drop_blank_tables(document: model.Document) -> None:
    """Take out the grids that contribute nothing to the page.

    An empty grid is as much nothing as an empty text frame is, and a
    third of the corpus's tables are one. But "empty" has to mean empty
    of everything, and a table's lines and fills are not in the event
    stream at all: they are shapes in the drawing stream, read onto the
    cells by `_apply_cell_insets`. So this runs after that pass, and only
    that ordering makes the test honest -- asked any earlier, a blank
    ruled grid is indistinguishable from a blank one, and Publisher's
    layout grids are mostly blank and mostly ruled.
    """

    def blank(item: model.Item) -> bool:
        if not isinstance(item, model.Table):
            return False
        if item.style.fill or item.style.stroke:
            return False
        return all(
            cell.story.is_empty() and not cell.rules and cell.shade is None
            for cell in item.cells
        )

    dropped = 0

    def prune(items: List[model.Item]) -> List[model.Item]:
        nonlocal dropped
        kept: List[model.Item] = []
        for item in items:
            if blank(item):
                dropped += 1
                continue
            if isinstance(item, model.Group):
                item.children = prune(item.children)
            kept.append(item)
        return kept

    for page in document.pages:
        page.items = prune(page.items)
    for master in document.masters:
        master.items = prune(master.items)

    if dropped:
        # Worth a line even though dropping it is right: these are the
        # grids a page is laid out on, so a reader looking for one in the
        # package should be told it went, and why.
        document.warnings.append(
            f"{dropped} empty table(s) dropped: no text, no rule, no shade, "
            f"no fill, no stroke"
        )


def _apply_page_margins(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> None:
    """Put Publisher's margin guides back on every page.

    librevenge's drawing interface has two properties for a page,
    `svg:width` and `svg:height`, so a converted document arrives with
    whatever margins the reader defaults to and anyone continuing the
    layout has to measure the original by eye. The .pub states the guides
    itself, as positions on the page rather than insets from its edges,
    which is why the page size is needed to resolve them -- and why a page
    the guides do not fit inside keeps its default rather than being given
    a margin that cannot be true.

    Publisher keeps one set per publication, so every page gets the same
    guides resolved against its own size.
    """
    if structure is None or structure.guides is None:
        return

    guides = structure.guides
    applied = 0
    for page in list(document.pages) + list(document.masters):
        found = guides.margins(page.width, page.height)
        if found is None:
            continue
        left, top, right, bottom = found
        page.margins = model.PageMargins(
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            columns=tuple(column - guides.left for column in guides.columns),
        )
        applied += 1

    if applied:
        log.info(
            "page margins read for %d page(s): %.1f %.1f %.1f %.1f pt",
            applied, guides.left, guides.top, guides.right, guides.bottom,
        )
    else:
        document.warnings.append(
            "page margin guides were read but fit none of the pages, so "
            "every page keeps the reader's default margins"
        )


#: Below this a corrected ramp turn is the turn it already had, and saying
#: so would be counting a shape that needed nothing. At 0.005 degrees the
#: widest ramp in the corpus moves four hundredths of a point end to end.
_RAMP_ANGLE_EPSILON = 0.005


def _ramp_span(found: "pubfile.ShapeGradient") -> float:
    """How far the ramp runs, measured across the box the file states.

    Not the box the item carries. The anchor measures a shape with its
    outline while libmspub reports the path inside it -- 16pt apart on the
    page-8 panel of the newsletter corpus -- and a turned shape's
    page-aligned box is bigger than the shape in both directions, half
    again as tall on the masthead ribbon. Measured across the item either
    way leaves the ramp squeezed or stretched, reaching its end colours
    early or not at all.

    The angle used is the ramp's *inside* the shape, before the shape is
    turned or mirrored, because that is the frame the file's box is in.
    """
    angle = idml._ramp_angle(found.angle, found.width, found.height)
    return (
        abs(found.width * math.cos(math.radians(angle)))
        + abs(found.height * math.sin(math.radians(angle)))
    )


def _ramp_turn(found: "pubfile.ShapeGradient", item: model.Item) -> float:
    """The turn a ramp owes the page, once the shape is placed on it.

    Publisher turns a shape and its shade together, and libmspub reports
    neither on the ramp: for a polygon the turn goes into the order of the
    points, where a ramp stated as an angle cannot see it. So it is carried
    here and handed to the writer, which adds it *after* laying the ramp
    across the box -- a rigid turn of the whole shape is not the diagonal
    that `idml._ramp_angle` stretches, and stretching it too is what left
    the masthead ribbon twelve degrees off the axis Publisher draws.

    Two signs are in it. Publisher states a turn the other way about from
    the way it draws it -- the same negation `_restore_floored_turns`
    works in -- so the file's value is negated. And a turn the item already
    carries is one the reader will apply to the ramp itself, so it is taken
    off again rather than counted twice. That second term is not measured:
    libmspub reports no rotation at all on any gradient shape in the
    corpus, so every one of them has an item turn of zero.
    """
    return model._fold_angle(-found.rotation - item.rotation)


def _restore_gradient_ramps(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> None:
    """Put back the ends of every ramp libmspub reports only the middle of.

    Publisher states a gradient as two colours -- the shape's fill and its
    fill-back -- with waypoints between them. libmspub reads all three
    (`getShapeFill`) and then, whenever there is a waypoint list at all,
    builds the ramp from that list alone and drops both ends. Where the
    list holds one waypoint that leaves a single stop, which is nothing to
    ramp between and paints the shape flat; where it holds several the
    ramp survives but starts and finishes in the wrong colours. A heading
    bar that runs navy to white arrived as light blue to pale blue, and a
    banner that runs white to brown arrived as flat grey.

    So the ramp is read from the file wherever the file states one, not
    only where it collapsed. Two things make that safe rather than a
    second opinion: on the ramps libmspub does report in full, the
    waypoints this reconstruction produces are identical to its own, stop
    for stop, and so are the angles. What is added is the pair of colours
    it drops. A shape whose fill states no waypoint list is left alone --
    libmspub builds those from the two end colours itself, correctly.
    """
    if structure is None or not structure.gradients:
        return

    restored = 0

    def visit(items: List[model.Item], width: float, height: float) -> None:
        nonlocal restored
        for item in items:
            if isinstance(item, model.Group):
                visit(item.children, width, height)
                continue
            if item.style.gradient is None and not item.style.approximated_fill:
                continue
            found = structure.gradient_for(
                item.x + item.width / 2.0 - width / 2.0,
                item.y + item.height / 2.0 - height / 2.0,
                item.width,
                item.height,
            )
            if found is None:
                continue
            turn = _ramp_turn(found, item)
            if len(found.stops) < 2:
                # No ramp to rebuild: libmspub reads a fill with no
                # waypoint list correctly. Only where the shape sits is
                # missing from it -- the turn and the flip. The masthead
                # ribbon on page 1 of every issue in the corpus is this
                # case, set at -12.192 degrees and arriving straight up and
                # down.
                placed = item.style.gradient
                if placed is not None and (
                    abs(turn - placed.turn) > _RAMP_ANGLE_EPSILON
                    or found.flipped_h != placed.flipped_h
                    or found.flipped_v != placed.flipped_v
                    or placed.span is None
                ):
                    item.style.gradient = replace(
                        placed, turn=turn, span=_ramp_span(found),
                        flipped_h=found.flipped_h, flipped_v=found.flipped_v,
                    )
                    restored += 1
                continue
            item.style.gradient = model.Gradient(
                stops=tuple(
                    model.GradientStop(location=position * 100.0, color=colour)
                    for position, colour in found.stops
                ),
                # A flattened fill never became a Gradient at all, so its
                # angle went with the ramp; the file states it, and
                # `pubfile` hands it over the way libmspub would have.
                # That angle is the ramp's inside the shape and stays so.
                # Where the shape then *sits* is the turn and the flip,
                # kept apart from it because the writer lays the ramp
                # across the box before placing it.
                angle=found.angle,
                turn=turn,
                flipped_h=found.flipped_h,
                flipped_v=found.flipped_v,
                span=_ramp_span(found),
                radial=(
                    item.style.gradient.radial
                    if item.style.gradient is not None else False
                ),
            )
            item.style.fill = found.stops[0][1]
            item.style.approximated_fill = False
            restored += 1

    for page in document.pages:
        visit(page.items, page.width, page.height)
    for master in document.masters:
        visit(master.items, master.width, master.height)

    if restored:
        log.info("gradient ramps read from the file for %d shape(s)", restored)


#: A turn stated in whole degrees has no fraction to put back, and most
#: of the corpus states whole degrees. This leaves room around that zero
#: for the arithmetic: at 0.005 degrees the widest band in the corpus
#: moves four hundredths of a point, end to end.
_TURN_EPSILON = 0.005


def _restore_floored_turns(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> None:
    """Put back the fraction of a turn libmspub floors off a shape.

    Publisher states a shape's turn in 16.16 fixed point, and libmspub
    keeps only the whole degrees of it -- by flooring, which is what
    reading the top half of the word amounts to. A band stating
    -540.042145 is drawn as though the file had said -541; a shape is
    drawn at the turn it states the other way about, so that is 181
    degrees on the page where the file asks for 180.042. One degree out
    is 6.5pt of drop across an A5 page, and every section-heading band in
    the kerkbode corpus states that turn, eight to an issue.

    The file settles it and Publisher's own PDF export agrees with the
    file: in `1336 kerkbode.pdf` the band is drawn at 0.042 degrees off
    square, not one degree, and turning libmspub's own points back by
    `floor - stated` lands within 0.009pt of all four of Publisher's
    corners. So the points are turned by that much and nothing else --
    libmspub has the shape's centre and its side lengths right, only its
    bearing wrong, and its centre is what a shape is turned about.

    Only shapes the file states a gradient for can be reached: that is
    the one record carrying both a turn and a box to match an item by.
    It is also the whole of the damage the corpus shows, because the only
    other fractional turns it states are on WordArt, which `_recover_wordart`
    already takes from the file rather than from libmspub.
    """
    if structure is None or not structure.gradients:
        return

    turned = 0

    def visit(items: List[model.Item], width: float, height: float) -> None:
        nonlocal turned
        for item in items:
            if isinstance(item, model.Group):
                visit(item.children, width, height)
                continue
            if not isinstance(item, model.Polygon) or len(item.points) < 3:
                continue
            found = structure.gradient_for(
                item.x + item.width / 2.0 - width / 2.0,
                item.y + item.height / 2.0 - height / 2.0,
                item.width,
                item.height,
            )
            if found is None:
                continue
            correction = math.floor(found.rotation) - found.rotation
            if abs(correction) < _TURN_EPSILON:
                continue
            radians = math.radians(correction)
            cos, sin = math.cos(radians), math.sin(radians)
            centre_x = item.x + item.width / 2.0
            centre_y = item.y + item.height / 2.0
            item.points = [
                (
                    centre_x + (px - centre_x) * cos - (py - centre_y) * sin,
                    centre_y + (px - centre_x) * sin + (py - centre_y) * cos,
                )
                for px, py in item.points
            ]
            xs = [px for px, _ in item.points]
            ys = [py for _, py in item.points]
            item.x, item.y = min(xs), min(ys)
            item.width, item.height = max(xs) - item.x, max(ys) - item.y
            turned += 1

    for page in document.pages:
        visit(page.items, page.width, page.height)
    for master in document.masters:
        visit(master.items, master.width, master.height)

    if turned:
        log.info("turn read from the file for %d shape(s)", turned)


def _text_width(frame: model.TextFrame) -> float:
    """How wide one column of a frame's text is, in points."""
    _top, right, _bottom, left = frame.padding
    inside = frame.width - left - right
    columns = max(frame.columns, 1)
    return max((inside - frame.column_gap * (columns - 1)) / columns, 0.0)


def _cell_width(table: model.Table, cell: model.TableCell) -> float:
    """How wide one cell's text is, in points, spans and insets included."""
    widths = table.column_widths or [table.width / table.column_count]
    span = widths[cell.column:cell.column + max(cell.column_span, 1)]
    inside = sum(span) if span else 0.0
    if cell.insets:
        inside -= cell.insets.left + cell.insets.right
    return max(inside, 0.0)


def _tabbed_paragraphs(document: model.Document):
    """Every paragraph in the document that contains a tab, with the width
    its tabs have to run across."""
    for item in document.all_items():
        stories = []
        if isinstance(item, model.TextFrame):
            stories.append((item.story, _text_width(item)))
        elif isinstance(item, model.Table):
            stories += [
                (cell.story, _cell_width(item, cell)) for cell in item.cells
            ]
        for story, width in stories:
            for paragraph in story.paragraphs:
                if "\t" in paragraph.text():
                    yield paragraph, width


def _apply_tab_stops(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> None:
    """Put each paragraph's tabs where Publisher put them.

    libmspub reports five paragraph properties and a tab stop is not among
    them -- it parses them and then never reads the member it parsed them
    into -- so every tab in a converted document lands on the reader's own
    grid, half an inch in InDesign, rather than where the author set it.

    Nothing in the event stream says which paragraph is being reported, so
    the text is the only handle: a paragraph is given the stops the file
    states for that same text. Where one text is stated two ways the file
    is not saying which paragraph is which, and neither is applied --
    the same rule two tables drawing one grid get.

    The rest -- most of them, 200 of the corpus's 203 tabs -- were lined up
    on the document's own default grid instead, the "Default tab stops"
    interval of Publisher's Format -> Tabs dialog. That interval is in the
    file (`pubfile._default_tab_stop`) and is written out as an explicit
    ruler of left stops, because InDesign has a default grid of its own at
    half an inch and would otherwise put every one of those tabs somewhere
    else. A document that states the same half inch needs no ruler; one
    that states no interval at all gets none either, because the file does
    not say what grid it was on, and that is said in the report.
    """
    if structure is None:
        return

    stated: Dict[str, Optional[tuple]] = {}
    for text, stops in structure.paragraph_stops:
        key = model.clean_text(text)
        # A text that turns up twice is only usable while both agree, and a
        # paragraph stating no stops disagrees with one that states some.
        if stated.setdefault(key, stops) != stops:
            stated[key] = None

    widest_page = max((page.width for page in document.pages), default=0.0)

    placed = ruled = unplaced = 0
    for paragraph, width in _tabbed_paragraphs(document):
        stops = stated.get(paragraph.text())
        if stops:
            paragraph.tab_stops = [
                model.TabStop(position=position, alignment=alignment)
                for position, alignment in stops
            ]
            placed += 1
            continue
        ruler = _default_ruler(structure.default_tab_stop, width, widest_page)
        if ruler:
            paragraph.tab_stops = ruler
            ruled += 1
        else:
            unplaced += 1

    if placed:
        log.info("tab stops read for %d paragraph(s)", placed)
    if unplaced:
        log.info(
            "%d paragraph(s) left on the reader's own tab grid of %.0fpt",
            unplaced, pubfile.READER_DEFAULT_TAB_STOP,
        )
        # A document stating the reader's own interval is already where it
        # wants to be and there is nothing to check. One stating no
        # interval is a different case: half an inch is InDesign's default,
        # not Publisher's, so the grid these tabs were on is unknown.
        if structure.default_tab_stop is None:
            document.warnings.append(
                f"{unplaced} tabbed paragraph(s) left on the reader's own "
                f"grid of {pubfile.READER_DEFAULT_TAB_STOP:.0f}pt: the file "
                f"states no default interval of its own, and half an inch is "
                f"the reader's default rather than Publisher's, so the grid "
                f"they were set on is unknown — anything tabbed into "
                f"columns is worth a look"
            )
    if ruled:
        interval = structure.default_tab_stop
        log.info(
            "%d paragraph(s) given the document's default grid of %.4fpt",
            ruled, interval,
        )
        document.warnings.append(
            f"{ruled} paragraph(s) state no tab stop of their own and were "
            f"put on the document's default grid of {interval:.2f}pt, the "
            f"interval the file states and Publisher reads back: anything "
            f"tabbed into columns is worth a look"
        )


# A ruler is written out stop by stop, so a narrow interval across a wide
# frame is a lot of them. Publisher allows an interval down to 1pt, which
# over a full page would be some 600 stops on every tabbed paragraph; past
# this many the ruler stops early and the remaining tabs fall back on the
# reader's grid, which is no worse than what they had before.
_MAX_RULER_STOPS = 120


def _default_ruler(
    interval: Optional[float], width: float, fallback_width: float
) -> List[model.TabStop]:
    """The document's default grid, as stops a paragraph can state.

    A stop is measured from the frame's text edge, not from the
    paragraph's indent -- the same edge the hanging-indent stop in `idml`
    is measured from -- so the grid is the same for every paragraph in a
    frame and an indent does not move it. An indent only makes the stops
    behind it unreachable, which costs nothing.

    The ruler covers the width rather than fitting inside it: where the
    interval does not divide the width, one more stop is written past the
    edge, so no tab in the last part of a frame is left behind.

    One frame in the corpus reports a width of 5.5pt while holding 34
    paragraphs of text, so a frame too narrow to hold a single stop is
    taken as a width not worth believing and the page's width is used
    instead. Erring long is free: a stop the text never reaches does
    nothing, where a ruler that stops short drops the tabs after it back
    onto the reader's grid, which is the error being fixed.
    """
    if not interval or interval <= 0:
        return []
    # Nothing to carry when the document is already on the grid InDesign
    # would use: writing the ruler out anyway would only add noise.
    if abs(interval - pubfile.READER_DEFAULT_TAB_STOP) < 0.01:
        return []
    room = width if width >= interval else fallback_width
    count = int(room // interval)
    # Publisher's grid has no end: a tab past the last multiple that fits
    # goes to the next one, off the frame's edge and onto the following
    # line. A ruler that stops at the last multiple *inside* the frame
    # instead leaves that tab to the reader's grid, which is the error
    # being fixed -- 8.08pt across a 168pt column of `1336 kerkbode` fits
    # 20 stops and leaves the last 6pt of the column short. So the grid is
    # covered rather than fitted, and the stop past the edge is one no
    # text can reach anyway.
    if count * interval < room - 0.001:
        count += 1
    count = min(count, _MAX_RULER_STOPS)
    return [
        model.TabStop(position=interval * step)
        for step in range(1, count + 1)
    ]


def _frame_text(frame: model.TextFrame) -> str:
    return "".join(
        span.text
        for paragraph in frame.story.paragraphs
        for span in paragraph.spans
    )


def _point_size(frame: model.TextFrame) -> float:
    sizes = [
        span.size_pt
        for paragraph in frame.story.paragraphs
        for span in paragraph.spans
        if span.size_pt
    ]
    return (sum(sizes) / len(sizes)) if sizes else 10.0


def _frame_capacity(frame: model.TextFrame, point_size: Optional[float] = None) -> float:
    """Roughly how many characters the frame can show.

    Average glyph advance ~0.5em and leading ~1.2em. Only the order of
    magnitude matters to the callers, both of which are asking "does this
    text plainly not fit?". `point_size` is passed in for a frame that
    carries no text of its own to measure — a link continuing a chain.
    """
    if point_size is None:
        point_size = _point_size(frame)
    columns = frame.width / max(1e-6, 0.5 * point_size)
    rows = frame.height / max(1e-6, 1.2 * point_size)
    return max(0.0, columns * rows)


def _frames_by_shape(
    document: model.Document, structure: "pubfile.FileStructure"
) -> Dict[int, model.TextFrame]:
    """The text frame each Escher shape turned into, where that is certain.

    The same handle everything else matched from the .pub uses: both sides
    measure the same shape, so both put its centre in the same place. Only
    text frames are candidates, since only a text frame can be a link, and
    a centre two of them share settles nothing and is dropped.

    The search is one page wide, not the document, and that is what makes
    it work at all: a newsletter repeats its two-column layout, so a column
    on page 10 sits exactly where the column on page 12 does. The file says
    which page chunk holds the shape and §14's mapping says which page
    libmspub made of that chunk, which narrows the question to one page
    before the centre is asked at all.
    """
    page_of_chunk = _page_by_chunk(document, structure)
    frames: Dict[int, model.TextFrame] = {}
    for anchor in structure.anchors:
        index = page_of_chunk.get(structure.page_seq_of(anchor.shape_seq))
        if index is None or index >= len(document.pages):
            continue
        page = document.pages[index]
        seen = [
            item for item in model._walk(page.items)
            if isinstance(item, model.TextFrame)
            and abs(item.x + item.width / 2.0 - page.width / 2.0 - anchor.centre_x) <= 0.5
            and abs(item.y + item.height / 2.0 - page.height / 2.0 - anchor.centre_y) <= 0.5
        ]
        if len(seen) == 1:
            frames[anchor.shape_seq] = seen[0]
    return frames


def _stated_chains(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> List[List[model.TextFrame]]:
    """The chains the .pub states, as frames, in the order the story flows.

    A chain survives only where the event stream drew *every* link and all
    of them agree about the text. libmspub hands the complete story to each
    frame of a chain, so links that disagree are not what this models, and
    emptying them would throw text away rather than thread it.

    Every link, rather than the ones that happened to resolve, because
    threading part of a chain is worse than threading none of it: the links
    left out keep their copy of the story and the article still arrives
    twice. A chain that does not resolve whole is left to the measured
    pass, which reaches `Cantico_dei_Cantici.pub`'s three columns even
    though only two of its three shapes can be placed.
    """
    if structure is None or not structure.story_chains:
        return []

    frames_by_shape = _frames_by_shape(document, structure)
    chains = []
    for shapes in structure.story_chains:
        if len(shapes) < 2 or not all(seq in frames_by_shape for seq in shapes):
            continue
        frames = [frames_by_shape[seq] for seq in shapes]
        texts = {_frame_text(frame) for frame in frames}
        if len(texts) != 1 or not next(iter(texts)).strip():
            continue
        chains.append(frames)
    return chains


def _measured_chains(
    document: model.Document, already: set
) -> List[List[model.TextFrame]]:
    """The chains no record names, inferred from the text and the room.

    Identical text alone is not enough -- a page-number field repeats a
    single "#" across every page, and a running header repeats its title.
    Those are genuine copies, and blanking all but the first would erase
    them. What distinguishes a chain is that the story cannot fit the frame
    holding it: that is *why* the boxes were linked. So a group is threaded
    only when the text oversets even the roomiest frame in it, which leaves
    repeated labels alone.

    This is what a file whose shapes cannot be read still gets, and it can
    only see a chain that oversets -- the pair in `1336 kerkbode.pub` with
    1,905 characters against room for about 2,325 is invisible to it.
    """
    groups: dict = {}
    for page in document.pages:
        for item in model._walk(page.items):
            if not isinstance(item, model.TextFrame) or id(item) in already:
                continue
            text = _frame_text(item)
            if text.strip():
                groups.setdefault(text, []).append(item)

    return [
        frames
        for text, frames in groups.items()
        if len(frames) > 1
        and len(text) > max(_frame_capacity(frame) for frame in frames)
    ]


def _note_unreadable_structure(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> None:
    """Say so when the .pub's own structure could not be read at all.

    Every pass that reads the file directly returns early on a None
    structure, which is the contract: recovery is an improvement on the
    output, never a prerequisite for one. But five of them going quiet
    together is not nothing, and it used to reach the operator as `ok` --
    the status that means "converted, nothing suspicious" -- on a document
    with a literal '#' on every page. What is lost is worth naming, since
    a reader who knows can fix a footer by hand and a reader who does not
    will trust the page numbers.
    """
    if structure is not None:
        return
    document.warnings.append(
        "the .pub's own structure could not be read, so nothing that depends "
        "on it was applied: page-number fields stay '#', master content stays "
        "copied onto every page, and cell insets, tab stops, gradient ramps "
        "and WordArt headlines are all left as libmspub reported them"
    )


def _file_paragraphs(story: str) -> List[str]:
    """One story's text as the paragraphs it is made of.

    The carriage return is Publisher's paragraph *terminator*, so a story
    ending in one does not end with an empty paragraph -- and `clean_text`
    is what the event stream's own text goes through, so applying it here
    is what makes the two comparable at all.
    """
    pieces = story.split("\r")
    if pieces and not pieces[-1]:
        pieces.pop()
    return [model.clean_text(piece) for piece in pieces]


def _extend_story(story: model.Story, paragraphs: List[str], delivered: int) -> None:
    """Add the text from `delivered` characters on to a story that stops there.

    The tail continues in the format of the last run that did arrive: it is
    the same sentence carrying on, and the character it continues from is
    the only statement about its format that this pass has. Paragraphs
    after it copy the last one's shape for the same reason.
    """
    index, offset = 0, delivered
    while index < len(paragraphs) and offset > len(paragraphs[index]):
        offset -= len(paragraphs[index])
        index += 1

    last = story.paragraphs[-1]
    template = last.spans[-1] if last.spans else model.Span()

    def run(text: str) -> model.Span:
        return replace(template, text=text)

    rest = paragraphs[index][offset:] if index < len(paragraphs) else ""
    if rest:
        last.spans.append(run(rest))
    for text in paragraphs[index + 1:]:
        story.paragraphs.append(
            replace(last, spans=[run(text)], tab_stops=list(last.tab_stops))
        )


def _restore_truncated_stories(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> List[int]:
    """Put back the text libmspub stopped short of delivering.

    libmspub builds its runs from the character-run tables, and where it
    misreads them for a story it emits a run per character and then stops.
    One frame of `1337 kerkbode.pub` arrives holding 81 of the 4,562
    characters the file states for it, cut mid-word at 'voor over|leg' --
    two pages of an article that reach the page as one line. The words are
    not in doubt; only libmspub's reading of the formatting over them is.

    A frame is completed only when the file settles what is missing beyond
    argument: exactly one story has the delivered text as a prefix, and no
    story in the file *is* that text. The second half is what keeps a
    complete frame alone -- a short label like 'Datum' can open a longer
    story elsewhere in the document, and a frame holding all of its own
    story must never be extended with somebody else's.

    Both frames of a linked chain are completed, since libmspub hands the
    story to each of them; threading collapses them afterwards. What comes
    back is one count per *story*, not per frame, because a chain restored
    across two frames is one article recovered and not two.
    """
    if structure is None or not structure.story_texts:
        return []

    stories = [_file_paragraphs(text) for text in structure.story_texts]
    joined = ["".join(paragraphs) for paragraphs in stories]
    restored: Dict[int, int] = {}
    for item in model._walk([i for page in document.pages for i in page.items]):
        if not isinstance(item, model.TextFrame) or not item.story.paragraphs:
            continue
        delivered = _frame_text(item)
        if not delivered.strip():
            continue
        if any(text == delivered for text in joined):
            continue
        found = [
            index for index, text in enumerate(joined)
            if len(text) > len(delivered) and text.startswith(delivered)
        ]
        if len(found) != 1:
            continue
        _extend_story(item.story, stories[found[0]], len(delivered))
        missing = len(joined[found[0]]) - len(delivered)
        restored[found[0]] = missing
        log.info(
            "restored %d character(s) of a story libmspub cut short", missing
        )
    return list(restored.values())


def _fill_undelivered_stories(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> List[int]:
    """Put the words in the frames libmspub opened and never filled.

    The failure `_restore_truncated_stories` repairs has a limit case that
    it cannot touch: for five frames of `1337 kerkbode.pub` libmspub emits
    `startTextObject` and then `endTextObject` with not one paragraph in
    between, so two pages of an Open Monumentendag letter reach the page as
    a photograph and a page number. Matching on the text cannot help here,
    because there is no text -- and every story in the file begins with
    nothing, so a prefix rule asked about an empty frame is ambiguous by
    construction rather than by accident.

    The file settles it without going through the words at all. A shape
    names its story by id, SYID lists those ids in the order STRS cuts the
    text, and `story_of_shape` composes the two. That is an identity the
    file states, not an inference from what happens to be on the page.

    Two guards. The shape-to-frame mapping must be *injective*: in
    `Lisa Hoogendijk.pub` a decorative shape sits 0.2pt from a text frame
    -- inside the half-point the match allows -- so both it and the frame's
    own shape claim that frame, and the decoration's story is not the one
    there. Where two shapes claim one frame, neither speaks for it. And
    only frames libmspub left *entirely* empty are filled, so a frame that
    received text keeps it, and a story that is itself empty stays empty.
    """
    if structure is None or not structure.story_texts:
        return []

    frames = _frames_by_shape(document, structure)
    claimed = Counter(id(frame) for frame in frames.values())
    filled: Dict[int, int] = {}
    for shape_seq, frame in frames.items():
        if claimed[id(frame)] != 1 or _frame_text(frame).strip():
            continue
        index = structure.story_of_shape(shape_seq)
        if index is None:
            continue
        paragraphs = _file_paragraphs(structure.story_texts[index])
        if not any(text.strip() for text in paragraphs):
            continue
        frame.story.paragraphs[:] = [
            model.Paragraph(spans=[model.Span(text=text)]) for text in paragraphs
        ]
        filled[index] = sum(len(text) for text in paragraphs)
        log.info(
            "filled a frame libmspub left empty with %d character(s) from the file",
            filled[index],
        )
    return list(filled.values())


def _trim_trailing_blank_paragraphs(document: model.Document) -> int:
    """Drop the paragraphs at the end of a story that hold only whitespace.

    A recycled Publisher template accumulates them. The caption frame on
    the meditation page of `1338 kerkbode.pub` states 25 paragraphs where
    Publisher prints two: the quotation, its reference, and then a second
    quotation left over from an earlier issue followed by 38 tabs, a stray
    `1`, and ten paragraphs of nothing at all. Every one of those costs a
    line of height in a reader that lays out what it is given.

    Only the trailing run, and only whitespace. A blank paragraph between
    two others is spacing somebody asked for, and this cannot tell that
    from an accident; at the end of a story there is nothing left to space
    away from, and a paragraph of spaces and tabs draws nothing wherever it
    sits. A story that is *entirely* whitespace is left alone -- there is
    no content to be trailing after, and emptying it would hand the frame
    to `_drop_blank_frames`, which is a bigger decision than this pass is
    making.

    What it does not touch: the leftover text itself. Nothing in the file
    or the event stream distinguishes a stale quotation from a wanted one,
    and a rule that guessed would delete real copy elsewhere.
    """
    trimmed = 0
    for holder in list(document.pages) + list(document.masters):
        for item in model._walk(holder.items):
            if not isinstance(item, model.TextFrame):
                continue
            paragraphs = item.story.paragraphs
            if not any(p.text().strip() for p in paragraphs):
                continue
            while paragraphs and not paragraphs[-1].text().strip():
                paragraphs.pop()
                trimmed += 1
    if trimmed:
        log.info("%d trailing blank paragraph(s) trimmed", trimmed)
    return trimmed


def _apply_wrap_offsets(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> int:
    """Give every object the room it keeps text at, as the file states it.

    libmspub reports no wrap of any kind, so the converter decides for
    itself *which* objects text flows around -- but not by how much. The
    file says that outright, one distance per side on the shape, and until
    this pass read it the offset was written as zero on all four.

    Measured on page 11 of `1337 kerkbode.pub`, where the copy runs down
    the side of a portrait: Publisher starts those nine lines at x=125 and
    a zero offset starts them at 117.3. The bottom edge matters more than
    the extra air, because it decides *how many* lines are narrow -- the
    wrap ended at 161.25, above the last wrapped line's box at 163.0, so
    the column widened one line before Publisher widens it. With the
    file's 2.88pt the wrap reaches 164.13 and that line stays in.

    Matched by where the shape sits, and left alone where that is not
    certain: an offset taken off the wrong shape is a gap invented, which
    is the one thing worse than the zero this replaces. That is also why
    the search is one page wide -- Escher coordinates are measured from the
    centre of *a* page, so a photograph in the same corner of another page
    matches on position alone.

    Masters as well as pages, because `_apply_master_pages` runs first and
    has by then moved the items it attributed off the pages and onto a
    master. A logo repeated on every page is exactly what it lifts, and
    exactly the kind of picture copy has to keep clear of.
    """
    if structure is None or not structure.anchors:
        return 0

    page_of_chunk = _page_by_chunk(document, structure)
    shapes_on: Dict[int, set] = {}
    for anchor in structure.anchors:
        index = page_of_chunk.get(structure.page_seq_of(anchor.shape_seq))
        if index is not None:
            shapes_on.setdefault(index, set()).add(anchor.shape_seq)
    # A master's content is held under a master chunk, so those are the
    # shapes to search for it -- and they are not in `shapes_on`, whose
    # mapping only covers the chunks libmspub turned into pages.
    on_master = {
        anchor.shape_seq for anchor in structure.anchors
        if structure.page_seq_of(anchor.shape_seq) in structure.masters
    }

    found = 0
    holders = [(page, shapes_on.get(index)) for index, page in enumerate(document.pages)]
    holders += [(master, on_master) for master in document.masters]
    for holder, shapes in holders:
        for item in model._walk(holder.items):
            offsets = structure.wrap_near(
                item.x + item.width / 2.0 - holder.width / 2.0,
                item.y + item.height / 2.0 - holder.height / 2.0,
                item.width,
                item.height,
                shapes=shapes,
            )
            if offsets is not None:
                item.wrap_offsets = offsets
                found += 1
    log.info("wrap distance read for %d item(s)", found)
    return found


def _fill_undelivered_tables(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> List[int]:
    """Lay a table's story out over its cells the way the file states it.

    Two failures, one answer. Page 27 of `1337 kerkbode.pub` is a cleaning
    rota whose three tables arrive with every one of their 90 cells empty,
    so `_drop_blank_tables` takes them and the page prints as a headline
    and a page number. The rota in `1338 kerkbode.pub` arrives worse than
    empty: libmspub puts the entire story into the *first* cell and leaves
    the other forty blank, which is not a page a reader can fix by hand.

    Both are the same thing -- libmspub failing to divide a story over the
    cells it belongs to -- and the file states that division outright.
    `pubfile.table_cell_texts` composes the three records it takes and
    answers None unless all of them agree, so what comes back is a map of
    the grid rather than a guess at one.

    Where the file and the event stream already agree about a cell, the
    cell is left exactly as it arrived. That is what keeps libmspub's
    reading of the type on the seventeen tables in the corpus it gets
    right: those are untouched, span for span, and only a cell whose text
    the file places differently is rewritten. Publisher's own sorting of a
    table's rows is handled by the same map, since the cell records name
    the position each piece is drawn at.
    """
    if structure is None:
        return []

    moved = []
    for item in document.all_items():
        if not isinstance(item, model.Table) or not item.cells:
            continue
        placed = structure.table_cell_texts(item.column_widths, item.row_heights)
        if placed is None or len(placed) != len(item.cells):
            continue
        if {(cell.row, cell.column) for cell in item.cells} != set(placed):
            continue
        paragraphs = {
            position: _file_paragraphs(text) for position, text in placed.items()
        }
        if not any(t.strip() for texts in paragraphs.values() for t in texts):
            continue
        characters = 0
        for cell in item.cells:
            texts = paragraphs[(cell.row, cell.column)]
            if _cell_text(cell).strip() == "".join(texts).strip():
                continue
            cell.story.paragraphs[:] = [
                model.Paragraph(spans=[model.Span(text=text)]) for text in texts
            ]
            characters += sum(len(text) for text in texts)
        if characters:
            moved.append(characters)
            log.info(
                "laid %d character(s) of a table out over its cells from the file",
                characters,
            )
    return moved


def _cell_text(cell: "model.TableCell") -> str:
    """Everything one cell holds, as one string."""
    return "".join(
        span.text for paragraph in cell.story.paragraphs for span in paragraph.spans
    )


def _note_filled_text(document: model.Document, filled: List[int]) -> None:
    """Say which frames hold words libmspub never delivered at all.

    A heavier caveat than the restored text above carries, and it is worth
    saying separately. There the tail continued a run that had arrived, so
    the type came from Publisher. Here nothing arrived, so nothing in the
    event stream says what this text should look like and it lands in the
    document's default face at its default size. The words are the file's;
    the type is nobody's.
    """
    if not filled:
        return
    document.warnings.append(
        f"{sum(filled)} character(s) put into {len(filled)} frame(s) and "
        "table(s) libmspub did not fill: a frame it opened and left empty, or "
        "a table whose story it never divided over the cells -- in one file it "
        "puts a whole rota into the first cell and leaves the other forty "
        "blank. The words and, for a table, which cell each one belongs in "
        "come from the file. The type does not: there was no run delivered to "
        "take it from, so this text is in the default face at the default size "
        "-- restyle it in Affinity against the original"
    )


def _drop_blank_frames(document: model.Document) -> None:
    """Drop the frames that are still empty once the file has had its say.

    libmspub's own reason for dropping them, asked at the point where the
    answer is knowable: an empty frame with no fill and no stroke draws
    nothing. What moved is only *when* -- `model` cannot tell a frame
    Publisher left blank from one libmspub failed to fill, and the file
    can, so the question waits for it.
    """
    def keep(item: model.Item) -> bool:
        if not isinstance(item, model.TextFrame):
            return True
        return not item.story.is_empty() or bool(item.style.fill or item.style.stroke)

    for page in document.pages:
        page.items[:] = [item for item in page.items if keep(item)]
        for item in model._walk(page.items):
            if isinstance(item, model.Group):
                item.children[:] = [c for c in item.children if keep(c)]


def _note_restored_text(document: model.Document, restored: List[int]) -> None:
    """Say which text came from the file rather than from the event stream.

    It is the one thing in the document nobody has seen on a page: libmspub
    never delivered it, so it has never been through the reader's own
    layout, and the formatting over it is this pass's reading rather than
    Publisher's. Where it breaks between the frames of a chain is worth the
    same look threading already asks for.
    """
    if not restored:
        return
    document.warnings.append(
        f"{sum(restored)} character(s) restored on {len(restored)} story/stories "
        "that libmspub delivered cut short: it builds its runs from the "
        "character-run tables and, where it misreads them for a story, emits "
        "a run per character and then stops -- one story here arrived as a "
        "single line of a two-page article. The words come from the file, "
        "which states each story's length, and only where exactly one story "
        "in it continues what did arrive. The restored text carries the "
        "format of the last run that did arrive, since libmspub's reading of "
        "the formatting is what failed -- check the type and where the text "
        "breaks between frames"
    )


def _thread_duplicate_stories(
    document: model.Document, structure: Optional["pubfile.FileStructure"] = None
) -> None:
    """Collapse a story duplicated across linked frames into one chain.

    Publisher lets an article flow through a row of linked text boxes.
    librevenge's drawing interface has no way to say "this frame continues
    that one", so libmspub hands the *complete* story to every frame in the
    chain -- one sample carries an 11,121-character article eight times, 73%
    of that file's apparent text. Written through verbatim, the reader gets
    the article once per frame, each one overset.

    The frames are threaded instead: the first keeps the text and the rest
    continue it, which is what IDML models natively.

    Which frames are linked, and in what order, is the .pub's to say -- a
    shape names its story and its place in it -- and the order matters:
    taking it from the page sequence arrives backwards for an article that
    flowed against that sequence. Where the file cannot be read the frames
    are measured instead, which is weaker but costs a damaged file nothing.
    """
    chains = _stated_chains(document, structure)
    stated = len(chains)
    chains += _measured_chains(document, {id(f) for c in chains for f in c})

    for frames in chains:
        chain_id = f"chain{len(document.text_chains) + 1}"
        document.text_chains[chain_id] = frames
        for position, frame in enumerate(frames):
            frame.chain_id = chain_id
            if position:
                # The text lives on the first link; the others continue it.
                frame.story = model.Story()

    if document.text_chains:
        threaded = sum(len(c) for c in document.text_chains.values())
        # Which half a chain came from is the difference between an order
        # the file stated and one inferred from where the frames sat, so a
        # reader deciding what to check is told them apart.
        guessed = len(document.text_chains) - stated
        source = f"{stated} stated by the file"
        if guessed:
            source += f", {guessed} inferred from the text"
        document.warnings.append(
            f"{threaded} linked text frame(s) threaded into "
            f"{len(document.text_chains)} story/stories ({source}): check where "
            f"the text breaks between frames"
        )


def _is_guide_pair(item: model.Item) -> bool:
    """A path made only of bare two-point edges.

    libmspub reports most Publisher paths as disconnected edges -- 50 of
    the 56 in the sample corpus -- and where every one of those edges is a
    bare two-point segment the path outlines no area at all. In the corpus
    every one of them turns out to be the guides a WordArt shape stretches
    its glyphs between, which is why `_recover_wordart` looks here first.

    This is the geometry alone. Whether the path is filled, stroked or
    both is what tells us how it was *meant* to draw, which is
    `_is_edge_only_fill`'s question, not this one.
    """
    if not isinstance(item, model.Path) or not item.ops:
        return False
    lengths = []
    current = 0
    for op in item.ops:
        if op[0] == "M":
            if current:
                lengths.append(current)
            current = 1
        elif op[0] in ("L", "C", "Q"):
            current += 1
    if current:
        lengths.append(current)
    return bool(lengths) and all(length <= 2 for length in lengths)


def _is_edge_only_fill(item: model.Item) -> bool:
    """A guide pair that was asked to fill, so draws nothing at all.

    A fill needs area and two-point edges enclose none, so such a path is
    invisible. A stroke, by contrast, draws the edges themselves -- that
    one is visible, and wrong in its own way, which is why WordArt
    recovery claims both and only this one is reported when it is left.
    """
    return (
        _is_guide_pair(item)
        and item.style.stroke is None
        and item.style.fill is not None
    )


def _recover_wordart(
    document: model.Document, structure: Optional["pubfile.FileStructure"]
) -> None:
    """Put Publisher's WordArt headlines back, as ordinary text.

    A WordArt headline arrives as two things, neither of them the
    headline: an empty text frame, which is dropped because it has no text,
    no fill and no stroke; and a filled path made of the two guide edges
    the glyphs are stretched between, which encloses no area and draws
    nothing. The words, the font, the size and the rotation are all in the
    Escher stream, which libmspub reads for geometry and fill and has no
    constants for the rest of.

    So the guide path is replaced by a text frame carrying the words. The
    band and the rotation come from the shape's own anchor, the colour and
    any shadow from what libmspub already reported about the same shape,
    and the two halves are tied together by the one thing they share: the
    centre of that band, which both sides put in the same place to a
    fraction of a point.

    WordArt keeps its character formatting on the shape rather than on the
    text, so bold, italic, underline, strikethrough and its character
    spacing would be lost with the shape; they are read from the file and
    put back on the run.

    What cannot come across is the bending -- IDML has no warped type --
    but that is rarer than it sounds. The file names the shape it asked
    for, and 47 of the corpus's 48 WordArt shapes ask for plain unbent
    type: for those, straight text in the band is not an approximation of
    the headline, it *is* the headline. Only the bent ones need redrawing,
    and the report names which.
    """
    if structure is None or not structure.wordart:
        return

    # Which guide paths belong to which WordArt shape, in document order.
    # One shape can arrive as several: libmspub makes a separate draw call
    # per paint, so a headline that is filled *and* outlined reports the
    # same two guides twice. Collecting them first is what lets the extra
    # passes be dropped rather than left drawing rules across the words.
    groups: Dict[int, List[model.Path]] = {}
    by_id: Dict[int, "pubfile.WordArt"] = {}
    # The page a shape sits on, which is what its centre is measured from.
    page_size: Dict[int, tuple] = {}

    def collect(items: List[model.Item], width: float, height: float) -> None:
        for item in items:
            if isinstance(item, model.Group):
                collect(item.children, width, height)
                continue
            if not _is_guide_pair(item):
                continue
            art = structure.wordart_near(
                item.x + item.width / 2.0 - width / 2.0,
                item.y + item.height / 2.0 - height / 2.0,
            )
            if art is None or not art.text:
                continue
            groups.setdefault(id(art), []).append(item)
            by_id[id(art)] = art
            page_size.setdefault(id(art), (width, height))

    for page in document.pages:
        collect(page.items, page.width, page.height)
    for master in document.masters:
        collect(master.items, master.width, master.height)

    # The frame each shape becomes, against the first of its paths. Every
    # later path is a further paint of the same shape and is dropped.
    frames: Dict[int, model.TextFrame] = {}
    superseded: set = set()
    for art_id, paths in groups.items():
        width, height = page_size[art_id]
        frames[id(paths[0])] = _wordart_frame(paths, by_id[art_id], width, height)
        superseded.update(id(path) for path in paths[1:])

    def rebuild(items: List[model.Item]) -> List[model.Item]:
        out: List[model.Item] = []
        for item in items:
            if isinstance(item, model.Group):
                item.children = rebuild(item.children)
                out.append(item)
                continue
            if id(item) in superseded:
                continue
            out.append(frames.get(id(item), item))
        return out

    for page in document.pages:
        page.items = rebuild(page.items)
    for master in document.masters:
        master.items = rebuild(master.items)

    recovered = len(groups)
    document.wordart += recovered
    placed = set(groups)

    # Only what a person has to act on reaches the report. A headline that
    # was measured, set straight because the file never bent it, and had
    # its duplicate paint dropped is a converted headline, not a warning --
    # and fifteen of those used to print a paragraph explaining, four
    # different ways, that nothing had gone wrong. The count goes in the
    # detail line instead, beside the frames and the images.
    bent = [by_id[art_id] for art_id in groups if by_id[art_id].warp]
    if bent:
        shapes = ", ".join(sorted({art.warp for art in bent}))
        document.warnings.append(
            f"{len(bent)} WordArt headline(s) bent into a shape IDML cannot "
            f"state ({shapes}); straight text in the band is all that comes "
            f"across \u2014 redraw {_wordart_names(bent)}"
        )

    # A font this machine cannot read leaves the headline sized from
    # averages rather than from its own proportions. Naming the font is the
    # actionable part: installing it is the fix.
    estimated = [
        by_id[art_id] for art_id in groups
        if by_id[art_id].applied_source != "font"
    ]
    if estimated:
        fonts = sorted({art.font or "an unnamed font" for art in estimated})
        document.warnings.append(
            f"{len(estimated)} WordArt headline(s) sized from averages: "
            f"{', '.join(repr(f) for f in fonts)} could not be measured on "
            f"this machine, so their size and letter-spacing are close "
            f"rather than exact \u2014 install the font and convert again"
        )

    # A WordArt shape libmspub reported nothing at all for. Its words, its
    # band and its page are all in the file; what is missing is a shape in
    # the event stream at that band. Which page it belongs to is the part
    # that has to be earned rather than assumed -- see `_place_unreported`.
    missing = [art for art in structure.wordart if id(art) not in placed]
    if missing:
        settled, unplaced = _place_unreported(document, structure, missing)
        if settled:
            document.warnings.append(
                f"{len(settled)} WordArt headline(s) placed from the file "
                f"alone: libmspub reports no shape at their band, so the "
                f"words, the band and the page come from the .pub — the page "
                f"confirmed by the shapes libmspub *does* report on it, and "
                f"the band by nothing else being drawn there; check "
                f"{_wordart_names(settled)}"
            )
        if unplaced:
            document.warnings.append(
                f"{len(unplaced)} WordArt shape(s) not placed: libmspub "
                f"reports no shape where the file puts them and "
                f"{_unplaced_reason(unplaced)}; retype "
                f"{_wordart_names([art for art, _why in unplaced])}"
            )


# How far a headline may be condensed or stretched before the result is
# worse than not condensing it. Publisher will squeeze glyphs hard, but a
# ratio this far out means the metrics are wrong -- a substituted font, or
# a band that is not the one these words were drawn in -- and an
# unreadable smear is a worse answer than a headline that overflows.
_MIN_HORIZONTAL_SCALE, _MAX_HORIZONTAL_SCALE = 25.0, 400.0


def _sentence(clauses: List[str]) -> str:
    """Clauses as one readable list, so a report reads as prose."""
    if len(clauses) == 1:
        return clauses[0]
    return ", ".join(clauses[:-1]) + f", and {clauses[-1]}"


def _wordart_names(shapes: List["pubfile.WordArt"]) -> str:
    """A few headlines by their words, each with the shape it was bent into."""
    named = ", ".join(
        repr(" ".join(art.text.split())[:40]) + (f" ({art.warp})" if art.warp else "")
        for art in shapes[:3]
    )
    return named + (f" and {len(shapes) - 3} more" if len(shapes) > 3 else "")


# Why a shape the file states could not be put on a page, in the order the
# report prefers to explain it: no page settled beats a page whose band was
# already occupied, because the first is ignorance and the second a choice.
_NO_PAGE, _BAND_OCCUPIED = "no page", "band occupied"
_UNPLACED_REASONS = {
    _NO_PAGE: "nothing else on their page chunk reached the event stream, so "
              "there is no telling which page libmspub turned it into",
    _BAND_OCCUPIED: "something libmspub did report is already drawn across "
                    "the band, which is what a headline arriving twice looks "
                    "like",
}


def _unplaced_reason(unplaced: List[tuple]) -> str:
    reasons = {why for _art, why in unplaced}
    return _sentence([_UNPLACED_REASONS[why] for why in sorted(reasons)])


def _pages_by_chunk(
    document: model.Document, structure: "pubfile.FileStructure"
) -> Dict[int, set]:
    """Which of libmspub's pages each of the file's page chunks could be.

    The file states the page of every shape -- each page chunk lists the
    seqnums on it, and the lists are disjoint -- but it does not state them
    in libmspub's page order. In every multi-page file in the corpus the two
    orders are a permutation of one another: in `1336 kerkbode.pub` page
    chunk 266 is libmspub's page 2 and chunk 335 its page 0. So the mapping
    is measured rather than assumed, by the one thing both sides state about
    the same shape: where it sits.

    Each shape of a chunk narrows the chunk down to the pages holding an
    item at that spot, and the chunk is somewhere all of its shapes allow --
    so the sets are intersected. A shape libmspub drew nowhere narrows
    nothing and is passed over: silence is not a constraint. A chunk left
    with two pages is a chunk the shapes do not tell apart, which is how a
    master -- replayed onto every page, and so matching all of them -- is
    kept from claiming a page of its own; one left with none is a
    contradiction, and says as little.
    """
    centres: List[tuple] = []
    for index, page in enumerate(document.pages):
        for item in model._walk(page.items):
            centres.append((
                item.x + item.width / 2.0 - page.width / 2.0,
                item.y + item.height / 2.0 - page.height / 2.0,
                index,
            ))

    possible: Dict[int, set] = {}
    for anchor in structure.anchors:
        chunk = structure.page_seq_of(anchor.shape_seq)
        if chunk is None:
            continue
        # The same tolerance `wordart_near` uses: both sides measure the
        # same EMU by different routes and agree to a fraction of a point.
        seen = {
            index for x, y, index in centres
            if abs(x - anchor.centre_x) <= 0.5 and abs(y - anchor.centre_y) <= 0.5
        }
        if not seen:
            continue
        possible[chunk] = possible[chunk] & seen if chunk in possible else seen
    return possible


def _page_by_chunk(
    document: model.Document, structure: "pubfile.FileStructure"
) -> Dict[int, int]:
    """The page chunks the shapes settle on one page, and which page."""
    return {
        chunk: next(iter(pages))
        for chunk, pages in _pages_by_chunk(document, structure).items()
        if len(pages) == 1
    }


def _band_is_occupied(page: model.Page, frame: model.TextFrame) -> bool:
    """Is anything libmspub drew already standing in this band?

    The risk being guarded against is a headline arriving twice: a WordArt
    whose words also came through as an ordinary frame would be written
    once by libmspub and once from the file. Anything at all overlapping
    the band is treated as that, since a band with something in it is not a
    band this converter should be filling on the file's word alone.

    A page background is not something in the way -- it covers every band
    on the page by definition, and treating it as an obstruction would
    quietly turn this whole recovery off for any page that has one.
    """
    for item in model._walk(page.items):
        if item is frame or _is_page_background(item, page):
            continue
        if (
            item.x < frame.x + frame.width
            and frame.x < item.x + item.width
            and item.y < frame.y + frame.height
            and frame.y < item.y + item.height
        ):
            return True
    return False


def _place_unreported(
    document: model.Document,
    structure: "pubfile.FileStructure",
    missing: List["pubfile.WordArt"],
) -> Tuple[List["pubfile.WordArt"], List[tuple]]:
    """Put the headlines libmspub reported no shape for on their pages.

    Everything needed is in the .pub -- the anchor gives the band and the
    rotation, the properties the words, the font and the size -- and what
    used to be missing was any confirmation from the event stream. There
    are now two pieces of it, and both are required: libmspub must have
    reported *other* shapes of the same page chunk, which is what says
    which page this is, and it must have reported nothing standing in the
    band, which is what says the headline is not already there.

    Returns the shapes placed, and the ones left alone with the reason.
    """
    by_chunk = _page_by_chunk(document, structure)
    settled: List["pubfile.WordArt"] = []
    unplaced: List[tuple] = []
    for art in missing:
        index = by_chunk.get(structure.page_seq_of(art.shape_seq))
        if index is None or not 0 <= index < len(document.pages):
            unplaced.append((art, _NO_PAGE))
            continue
        page = document.pages[index]
        frame = _wordart_frame([], art, page.width, page.height)
        if _band_is_occupied(page, frame):
            unplaced.append((art, _BAND_OCCUPIED))
            continue
        page.items.append(frame)
        settled.append(art)
    return settled, unplaced


def _wordart_tracking(
    spacing: Optional[float], mean_advance_per_em: float
) -> Optional[float]:
    """WordArt's spacing multiple as the tracking IDML would write.

    The multiple scales each glyph's advance; IDML states the space added,
    in thousandths of an em. An advance is not an em, so the conversion
    needs to know how wide this font's glyphs actually are -- which used
    to be a flat half-em for every font, and is now measured. Times New
    Roman averages 0.44 of an em across a headline and Arial Black 0.61,
    so the guess was out by a quarter either way.

    Rounded to whole thousandths, because the multiple arrives as 16.16
    fixed point and Publisher's Loose reads 1.2001 rather than 1.2.
    """
    if spacing is None or abs(spacing - 1.0) < 0.001:
        return None
    return float(round((spacing - 1.0) * mean_advance_per_em * 1000.0))


def _wordart_fit(
    art: "pubfile.WordArt", lines: List[str], measure
) -> tuple:
    """The point size and horizontal scale a headline is drawn at.

    WordArt sets the words and stretches them to the shape, so the band is
    not a box the words sit inside -- it *is* the words. Two things follow.

    The height decides the size: a band holds as many lines as the words
    are set on, so each line gets its share, and the line that inks most of
    its em is the one that must fit. The width decides the condensation
    rather than the size, because IDML can state that directly. That is the
    whole of what WordArt's stretch does, and it is why a stated point size
    is a floor rather than the truth -- the *Meditatie* headline states 20
    and Publisher draws it at about 38.

    A size worked out this way is only as good as the metrics behind it, so
    a font this machine cannot read takes the older, safer rule instead:
    the smaller of what the height allows and what the width allows, and no
    condensation at all. Condensing by a ratio derived from a guessed width
    would state a precision that is not there.

    **A headline of one glyph earns no scale either**, for the same reason
    and on measured grounds. The band is the bounding box of the *slanted*
    text -- WordArt italic, on faces that ship no italic and are therefore
    slanted by whoever draws them -- so part of its width is slant overhang
    rather than room for glyphs. On a long headline that overhang is a few
    percent of a wide band; on a single dropped initial it is a fifth of a
    narrow one. Both of the corpus's single-glyph headlines were fitted
    against Publisher's own PDF export, glyph outline against glyph
    outline, and it draws them at 95.7% and 97.0% where fitting the
    advances to the whole band asks for 121.2% and 151.9%. Stating no
    scale lands them within 4.6% and 3.4% instead
    (`research/wordart_stretch.py`).

    What that measurement does *not* settle is the formula for the rest.
    The overhang the fit recovers, 9.5pt of a 44.9pt band, predicts the
    one initial to within 0.25% and the other not at all -- and the two
    share a band exactly, so the corpus holds only one geometry to fit
    against. Until a file supplies another, a long headline keeps the rule
    it has.
    """
    measured = [measure(art.font, art.bold, art.italic, line) for line in lines]
    spacing = art.spacing or 1.0
    per_line = art.height / max(len(lines), 1)
    ink = max((m.ink_per_em for m in measured), default=0.0)
    widest = max((m.width_per_em for m in measured), default=0.0) * spacing
    exact = all(m.exact for m in measured)
    source = measured[0].source if measured else "average"

    # A shape that does not state the stretch flag was set at a size and
    # left there, so the file's word is final. Nothing in the corpus is
    # one of these, but the flag is what says so rather than an assumption.
    if art.size is not None and not art.stretch:
        return art.size, None, source

    by_height = per_line / ink if ink > 0 else art.size or per_line
    if not exact or widest <= 0:
        by_width = art.width / widest if widest > 0 else by_height
        return min(by_height, by_width), None, source

    # One glyph, and the band is mostly the slant's overhang: see above.
    if sum(len(line) for line in lines) <= 1:
        return by_height, None, source

    scale = art.width / (widest * by_height) * 100.0
    return (
        by_height,
        min(max(scale, _MIN_HORIZONTAL_SCALE), _MAX_HORIZONTAL_SCALE),
        source,
    )


def _wordart_frame(
    paths: List[model.Path],
    art: "pubfile.WordArt",
    page_width: float,
    page_height: float,
    measure=None,
) -> model.TextFrame:
    """One WordArt shape as a text frame, in the band the file gives it.

    `paths` is every guide path libmspub reported for the shape, in the
    order it drew them. They describe one headline between them -- one
    call paints the glyphs, another outlines them -- so the paints are
    merged here rather than each becoming its own object.
    """
    def first(pick):
        for path in paths:
            value = pick(path.style)
            if value is not None:
                return value
        return None

    fill, gradient = first(lambda s: s.fill), first(lambda s: s.gradient)
    stroke = first(lambda s: s.stroke)
    # A shape with no fill of its own -- WordArt filled with a texture
    # reports one as a bitmap, not a colour -- has nothing but its outline
    # to colour the words with, so the outline is spent on that instead.
    outline = stroke if (fill is not None or gradient is not None) else None
    outline_width = first(lambda s: s.stroke_width or None) or 0.0 if outline else 0.0

    # Resolved here rather than as a default argument, which would bind
    # `fontmetrics.measure` once at import and leave no way to stand in
    # for it. A test that measures whatever fonts the machine happens to
    # have is a test that passes here and fails on a build runner.
    measure = measure or fontmetrics.measure
    lines = re.split(r"\r\n|\r|\n", art.text)
    size, scale, source = _wordart_fit(art, lines, measure)
    tracking = _wordart_tracking(
        art.spacing,
        measure(art.font, art.bold, art.italic, art.text).mean_advance_per_em,
    )
    art.applied_source = source

    # Where in the band the baseline goes. A headline is sized so its ink
    # fills the band, so the share of that ink above the baseline is the
    # share of the band above the baseline -- and stating it is what stops
    # a reader placing the first baseline by the font's own ascent, which
    # is taller than the band and puts the baseline past the frame's
    # bottom, where the line is hidden rather than drawn. Measured against
    # Publisher's own PDF: this puts the dropped initial's baseline 39.2pt
    # into its 40.1pt band, which is where Publisher draws it.
    #
    # The first line's, not the tallest line's: it is the first baseline
    # being placed, and every later line steps from it by the band's own
    # share below.
    first_line = measure(art.font, art.bold, art.italic, lines[0] or " ")
    baseline = size * first_line.ink_above_baseline_per_em

    # The frame is the band, exactly. Making it taller so a headline
    # wrapped by a substituted font still had somewhere to go was tried
    # and taken back out: it only holds the words in place if the reader
    # centres them vertically, and if it does not, the headline hangs
    # half a band high instead -- a certain error traded for a possible
    # one. Growing it is also no longer the way out of a band too short
    # for the type: the baseline below says where the line sits, and the
    # frame is what the copy flows around.
    frame = model.TextFrame(
        x=page_width / 2.0 + art.centre_x - art.width / 2.0,
        y=page_height / 2.0 + art.centre_y - art.height / 2.0,
        width=art.width,
        height=art.height,
        rotation=art.rotation,
        # Top, with the first baseline stated below. Centring was what
        # this asked for until the band's own height was measured against
        # what a reader will place inside it: Affinity centres nothing it
        # has decided will not fit, and a band is by definition shorter
        # than the ascent the font asks for -- 0.69 of an em against 0.86
        # for the dropped initial. So the line is placed rather than
        # centred, which lands the ink filling the band either way, and
        # that is where WordArt's stretch put it.
        vertical_align="top",
        # And the leading, not the reader's idea of an ascent, is what
        # places it. Without this the headline is not drawn at all.
        first_baseline_from_leading=True,
        # A headline is a shape floating over the page rather than a box
        # the layout made room for, and the one place that shows is a
        # dropped initial: its band overlaps the column it begins, and the
        # paragraph is not indented to make room, so unasked the letter is
        # drawn straight through the first lines of its own paragraph.
        # libmspub reports no wrap for anything, and asking for one on
        # every headline is worse than asking for none -- a band that
        # merely clips the corner of a date box would push the date out of
        # it. So it is taken from the file, per shape, and the shapes that
        # state nothing about wrapping are left as they arrived.
        wrap_text=art.wraps_text,
        # Only the shadow carries over: a fill here would paint a solid
        # block of the text colour across the band.
        style=model.GraphicStyle(shadow=first(lambda s: s.shadow)),
    )
    # A WordArt headline can be set on more than one line, and states the
    # break as the same CR LF Publisher uses in body text; one paragraph
    # per line is what that means.
    for index, line in enumerate(lines):
        paragraph = model.Paragraph(
            align="center",
            # WordArt stretches its glyphs to the band, so a headline is set
            # at the size that fills it -- and type that size has a line box
            # taller than the band it inks. A line taller than its frame is
            # overset text, which a reader hides rather than draws, and the
            # frame cannot simply grow: it is what the copy flows around, so
            # growing it would indent the paragraph further than Publisher
            # did. What is stated instead is the line: the band's own share
            # of its height, one share per line the words are set on, which
            # is exactly the room Publisher gave them.
            #
            # Except the first, which is doing a second job: the frame asks
            # for its first baseline at its first line's leading, so that
            # one states the baseline instead of the share. Every later
            # line then steps from it by the share, which puts each one's
            # ink in its own slice of the band.
            #
            # The single-line case is what was measured in Affinity; the
            # stacking is the arithmetic that follows from it, and the one
            # headline it applies to in the corpus (`Lisa Hoogendijk.pub`,
            # set on two lines) has a band far taller than its ascent, so
            # it drew before this and draws now. Worth a look on the first
            # stacked headline whose band is tight.
            line_spacing_pt=(
                baseline if index == 0 else art.height / max(len(lines), 1)
            ),
        )
        paragraph.spans.append(
            model.Span(
                text=model.clean_text(line),
                font=art.font,
                size_pt=size,
                # What libmspub reported for this shape describes the
                # glyphs, not a box behind them, so it goes on the run: the
                # fill is their colour, the ramp their ramp, and the stroke
                # the outline WordArt draws around them. A shape whose fill
                # is a texture reports no colour at all, and then the
                # outline is the only colour the words have.
                color=fill if fill is not None else stroke,
                gradient=gradient,
                stroke=outline,
                stroke_width=outline_width,
                # WordArt states bold and italic on the shape rather than
                # on the text, so they are lost with the shape unless they
                # are put back here. Nearly every headline in the corpus
                # is one or the other.
                bold=art.bold,
                italic=art.italic,
                underline=art.underline,
                strikethrough=art.strikethrough,
                tracking=tracking,
                horizontal_scale=scale,
            )
        )
        frame.story.paragraphs.append(paragraph)
    return frame


def _check_unnamed_languages(document: model.Document) -> None:
    """Report text whose language IDML has no name for.

    Publisher states a locale on every run and IDML names languages by a
    display string rather than a locale tag, so a country variant it does
    not list -- en-AU, say, where there is no plain "English" to fall back
    to either -- can only be left to the reader's own default. Stating a
    neighbouring dictionary instead would be picking one the file never
    named, and hyphenation is exactly what the choice decides.
    """
    unnamed: Dict[str, int] = {}
    for item in document.all_items():
        if not isinstance(item, model.TextFrame):
            continue
        for paragraph in item.story.paragraphs:
            for span in paragraph.spans:
                if span.language and not idml._language_name(span.language):
                    unnamed[span.language] = unnamed.get(span.language, 0) + 1

    if unnamed:
        named = ", ".join(
            f"{locale} ({count})" for locale, count in sorted(unnamed.items())
        )
        document.warnings.append(
            f"{sum(unnamed.values())} text run(s) are in a language IDML has "
            f"no name for — {named} — so they keep the reader's own language "
            f"and will hyphenate by its rules rather than Publisher's"
        )


def _check_gradient_losses(document: model.Document) -> None:
    """Report the gradients that could not be written as gradients.

    Both of these are already worked out where the fill is read; without
    them said out loud, a ramp that arrived as one stop is the one loss in
    the conversion that looks exactly like a deliberate flat fill.
    """
    flattened = uneven = 0
    for item in document.all_items():
        if item.style.approximated_fill:
            flattened += 1
        if item.style.uneven_stop_opacity:
            uneven += 1

    if flattened:
        document.warnings.append(
            f"{flattened} gradient fill(s) flattened to one colour: libmspub "
            f"reported a single stop for them, which leaves nothing to ramp "
            f"between, so the shape is filled with that stop and any shading "
            f"Publisher drew needs putting back"
        )
    if uneven:
        document.warnings.append(
            f"{uneven} gradient fill(s) had stops of differing opacity, which "
            f"IDML cannot state: its only transparency for a fill is one value "
            f"for the whole object, so the ramp is written opaque and the "
            f"fading needs redoing"
        )


# Publisher has no bold Calibri Light to set a bold run in, so it draws
# one: the reference PDF strokes the glyph outline as well as filling it
# (text render mode 2), at 0.22971 pt on 8.04 pt text and 0.27086 pt on
# 9.48 pt. Those are 1/35 of the em to within a ten-millionth, both of
# them, which is what makes it a rule rather than two measurements -- and
# IDML says exactly the same thing as a stroke on the run. Every stroked
# run in `1336 kerkbode.pdf` is one of these, and every one of them is
# Calibri Light, which is the other half of the reading: Publisher does
# this where the weight has no bold, not wherever a bold is missing.
_FAUX_BOLD_EM = 1.0 / 35.0

# The same trick for the italic a family does not have: rather than leave
# the run upright, Publisher shears it. Every sheared text matrix in
# `1336 kerkbode.pdf` states the same third of a unit -- 14 runs of
# Mystical Woods Rough Script, 3 of Blackadder ITC and 1 of Segoe Script
# Bold, which between them are every italic the file asks of a family that
# has none. IDML says a shear as an angle, and a third is 18.43 degrees.
#
# The sign is the one part read off the shape of the thing rather than
# probed: the PDF's shear sits in the matrix term that carries x with y, so
# a glyph's top goes right and the slant is forward, and forward is what
# IDML's positive skew is documented to be. Worth confirming in Affinity
# against the pull quote on page 17 of `1336 kerkbode.pub`, which is the
# spread on page 12 of `files/experiments/1336 kerkbode.pdf`.
_FAUX_ITALIC_DEGREES = math.degrees(math.atan(1.0 / 3.0))


def _name_fonts_as_installed(document: model.Document) -> None:
    """Name each run's font, and its style, the way its reader indexes it.

    libmspub reports the legacy family name, which is the one Publisher
    wrote and the only one Publisher can use: in the legacy naming a family
    holds four styles at most, so a fifth weight has to become a family of
    its own. 'Calibri Light' is that -- a family on Windows, and on every
    other reader the 'Light' style of 'Calibri'. Passing the name through
    is what makes Affinity report a font as missing while the file sits
    installed in the fonts folder, and it takes the text's metrics with it.

    The style needs naming for the same reason, and in two more cases than
    the family does. Publisher states a run's face as the family plus two
    booleans, so where the face it wants is neither the regular nor a bold
    or italic of it -- a 'Light', or the one upright face of a family with
    no italic -- the booleans reach nothing and the style has to be said
    outright. Publisher itself draws what the booleans then have no face
    for: a stroke where the weight has no bold, a shear where the family
    has no italic. Both are written here, from the fractions its own PDF
    uses.

    Only a name the installed face contradicts is touched. A font this
    machine does not have is left exactly as the file states it, because
    unverifiable is not the same as wrong, and on the machine that has the
    font the file's own name is very likely right.
    """
    folded: Dict[str, str] = {}
    stroked = sheared = 0
    for span in document.spans():
        if not span.font:
            continue
        naming = fontmetrics.naming(span.font, span.bold, span.italic)
        if naming is None:
            continue
        if not (naming.folded or naming.faux_bold or naming.faux_italic):
            continue
        if naming.folded:
            # Said as family and style rather than joined up, because joined
            # up they spell the name being replaced and the note then reads
            # as though nothing had happened.
            folded[span.font] = f"the {naming.style!r} style of {naming.family!r}"
            span.font = naming.family
        # Named outright, because bold and italic between them have no way
        # of asking for it: neither 'Light' nor the sole upright face of a
        # family whose italic is a shear is a style those two can reach.
        span.font_style = naming.style
        # Neither the stroke nor the shear displaces one the run already
        # carries: a value the document states outright is not this pass's
        # to replace, and nothing here is worth losing one for.
        if naming.faux_bold and span.size_pt and span.stroke is None:
            span.stroke = span.color or (0, 0, 0)
            span.stroke_width = span.size_pt * _FAUX_BOLD_EM
            stroked += 1
        if naming.faux_italic and span.skew is None:
            span.skew = _FAUX_ITALIC_DEGREES
            sheared += 1

    if folded:
        named = ", ".join(
            f"{stated!r} as {installed}" for stated, installed in sorted(folded.items())
        )
        document.warnings.append(
            f"{len(folded)} font(s) renamed to the family the reader lists "
            f"them under — {named}: Publisher names a font by the four-style "
            f"legacy family, where an extra weight has to be a family of its "
            f"own, and Affinity and macOS name the same file by its "
            f"typographic family and style. The file's own name reaches no "
            f"font at all on this machine, so it is the installed one that is "
            f"written — check the runs are in the weight the original sets them in"
        )
    if stroked:
        document.warnings.append(
            f"{stroked} bold run(s) set in a weight that has no bold: Publisher "
            f"draws these by stroking the glyphs rather than in a bold face, "
            f"because none exists — Calibri Light Bold is in no Calibri release "
            f"— and that stroke is what is written, at a 35th of the em, "
            f"the fraction Publisher's own PDF strokes. Affinity does not "
            f"synthesise a bold, so a real one would have to be chosen by hand"
        )
    if sheared:
        document.warnings.append(
            f"{sheared} italic run(s) set in a family that has no italic: "
            f"Publisher slants these by shearing the glyphs rather than in an "
            f"italic face, because the family ships none, and that shear is "
            f"what is written — {_FAUX_ITALIC_DEGREES:.2f}°, the third of a "
            f"unit Publisher's own PDF shears. Check the slant leans the way "
            f"the original does"
        )


def _check_unrenderable_paths(document: model.Document) -> None:
    """Report filled paths whose outlines enclose nothing.

    What is left here after `_recover_wordart` is a path whose guides
    matched no WordArt shape, so there is nothing to say about what it
    outlined. Joining the edges instead would invent geometry, and used to
    draw a filled bowtie across the page, so they are kept apart and the
    loss is named.
    """
    # Masters included: a path lifted onto one draws nothing just the same,
    # and there it draws nothing on every page that applies it.
    unrenderable = sum(
        1 for item in document.all_items() if _is_edge_only_fill(item)
    )

    if unrenderable:
        document.warnings.append(
            f"{unrenderable} filled path(s) enclose no area and draw nothing: "
            f"libmspub reported them as disconnected two-point edges, so "
            f"whatever they outlined needs redrawing"
        )


def _check_overset_text(document: model.Document) -> None:
    """Flag text frames far too small to show the text they contain.

    One file in the sample set has a 5.5 x 5.7 pt frame carrying 3,911
    characters, which Affinity renders as an empty box. Guessing the
    intended geometry would be inventing layout, so the frame is left
    alone and the operator is told exactly which file needs a human.

    This was written up as libmspub reporting a degenerate size, and that
    was wrong: the .pub's own Escher anchor for that shape states the same
    box to two decimals, so the document really does contain a text box
    collapsed to nothing and Publisher showed it empty too. The message
    says so, because the difference decides what the operator does —
    a parser artefact is worth reporting upstream, whereas a collapsed box
    is a judgement about what the hidden copy was for.
    (`test_the_file_states_the_collapsed_frame_libmspub_reports`.)

    A threaded story is measured against the whole chain, since that is
    what has to hold it. Checking each link on its own reported a normal
    linked article as a degenerate frame.

    Master frames are measured too. Nothing is ever threaded onto a master
    -- lifting runs before threading does -- so a master frame is always a
    frame on its own, and a collapsed one there costs more than a collapsed
    one on a page: it shows empty on every page applying the master.
    """
    for item in document.all_items():
        if not isinstance(item, model.TextFrame):
            continue
        characters = len(_frame_text(item))
        if characters < 20:
            continue

        if item.chain_id:
            # Only the head of a chain holds text, so this runs once per
            # chain — measured against every link's room, at the head's
            # type size since the others have no text left to measure.
            size = _point_size(item)
            capacity = sum(
                _frame_capacity(frame, size)
                for frame in document.text_chains[item.chain_id]
            )
        else:
            capacity = _frame_capacity(item)

        if characters > 10 * max(capacity, 1.0):
            document.warnings.append(
                f"text frame too small for its content: {characters} characters "
                f"in a {item.width:.1f}x{item.height:.1f}pt frame "
                f"(the .pub states that size itself, so Publisher showed "
                f"it empty too; resize it in Affinity to read the copy)"
            )


def convert(
    source: Path,
    destination: Path,
    pubdump: Path = PUBDUMP,
    codepage: Optional[str] = "auto",
    wrap_images: bool = True,
    facing_pages: Optional[bool] = None,
) -> Result:
    """Convert one .pub file to an .idml package.

    The package carries its own pictures, so the .idml is the whole
    deliverable and can be moved anywhere on its own.

    `facing_pages` None reads the layout off the file, which is what the
    command line does when neither flag is given; True and False are the
    operator overriding that either way.
    """
    source = Path(source)
    result = Result(source=source)
    try:
        _convert(
            result, source, Path(destination), pubdump, codepage,
            wrap_images, facing_pages,
        )
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


def _count_content(document: model.Document, result: Result) -> None:
    """Tally what the package will carry, for the report.

    A table counts as a frame because that is what it becomes in IDML -- a
    text frame whose story holds the grid -- and its cell text counts as
    text. Leaving it out understated one newsletter by 5,635 characters
    that were present in the output.
    """
    def characters(story: model.Story) -> int:
        return sum(
            len(span.text)
            for paragraph in story.paragraphs
            for span in paragraph.spans
        )

    for item in document.all_items():
        if isinstance(item, model.Table):
            result.text_frames += 1
            for cell in item.cells:
                result.characters += characters(cell.story)
        elif isinstance(item, model.TextFrame):
            result.text_frames += 1
            result.characters += characters(item.story)
        elif isinstance(item, model.Image):
            result.images += 1
        elif isinstance(item, (model.Rectangle, model.Ellipse, model.Polygon, model.Path)):
            result.shapes += 1
    result.wordart = document.wordart


def _convert(
    result: Result,
    source: Path,
    destination: Path,
    pubdump: Path,
    codepage: Optional[str],
    wrap_images: bool,
    facing_pages: Optional[bool],
) -> None:
    started = time.monotonic()
    log.info("converting %s -> %s", source, destination)

    document = parse_document(source, pubdump)

    if not document.pages:
        raise ConversionError("document contains no pages")

    textrepair.repair_document(document, codepage)
    structure = pubfile.read_structure(source)
    _note_unreadable_structure(document, structure)
    # Before everything that reads the text: threading compares the frames
    # of a chain and only collapses them where they agree, overset is
    # measured against what the frame actually holds, and a tab stop is
    # matched to its paragraph by that paragraph's words. All three want
    # the story whole. After `textrepair`, whose repair is part of the text
    # this matches against the file.
    _note_restored_text(document, _restore_truncated_stories(document, structure))
    # After it, and for the frames it cannot reach: a story delivered as
    # nothing at all has no prefix to match on, and is found through the id
    # the file gives it instead.
    _note_filled_text(
        document,
        _fill_undelivered_stories(document, structure)
        + _fill_undelivered_tables(document, structure),
    )
    # Once both have had their say, so that a frame is only blank if the
    # file agrees it is. `model` places every frame it is handed, because
    # up to here an empty frame and an unfilled one look the same.
    _drop_blank_frames(document)
    # Before every pass that reads a page by its index, and before the
    # page-number substitution in particular: a page put back afterwards
    # would leave the numbering it was restored to fix untouched.
    _note_restored_blanks(document, _restore_blank_pages(document, structure))
    # After the blanks: the page count is half the evidence, and libmspub's
    # count is not the document's.
    detected = facing_pages is None
    if detected:
        facing_pages = _detect_facing_pages(document, structure)
        _note_detected_facing(document, facing_pages, structure)
    _apply_master_pages(document, structure)
    _apply_cell_insets(document, structure)
    # After the insets pass, which is where a table's rules and shades
    # arrive: a grid with no text is only blank if it draws nothing too.
    _drop_blank_tables(document)
    _apply_page_margins(document, structure)
    # Before the WordArt pass, which takes a shape's paint as it finds it.
    _restore_gradient_ramps(document, structure)
    # After it, so that the ramps are matched against the box libmspub
    # drew rather than the narrower one a straightened band leaves.
    _restore_floored_turns(document, structure)
    # After every pass that puts text into the document -- the restored
    # stories, the filled frames, the tables -- so that each run is named
    # once. Before the WordArt pass, and deliberately: a headline's type
    # comes off the shape rather than the text, its glyphs are fitted to
    # the band the shape states, and a slant would push that ink wider than
    # the band it was fitted to. What the shear is worth on a headline is
    # not measured either -- Publisher draws WordArt as outlines, so there
    # is no text matrix in the PDF to read it off -- and the two faces that
    # set nearly every headline in the corpus have no italic. So the
    # headlines keep the older behaviour until a probe says otherwise, and
    # running this first is what leaves them out.
    _name_fonts_as_installed(document)
    _recover_wordart(document, structure)
    # After the WordArt pass, which places bands of its own that wrap, and
    # before the writer, which is the only thing that reads the offsets.
    _apply_wrap_offsets(document, structure)
    _rasterise_metafiles(document)
    # After the master pass: threading empties the continuation frames, and
    # a run of identical empty frames is exactly what master lifting looks
    # for, so doing this first would sweep the chain onto a master spread.
    _thread_duplicate_stories(document, structure)
    # After threading, which empties the continuation frames of a chain, so
    # that this reads the one frame still holding the paragraphs. Before the
    # two passes below, both of which count paragraphs: a tab stop is not
    # wanted for a line of tabs nobody sees, and text that only oversets
    # because of them is not overset.
    _trim_trailing_blank_paragraphs(document)
    # After threading, so that a paragraph is counted once rather than once
    # per frame the story was copied into before the links were made.
    _apply_tab_stops(document, structure)
    _check_unrenderable_paths(document)
    _check_gradient_losses(document)
    _check_unnamed_languages(document)
    _check_overset_text(document)

    # No image_dir_name, so the pictures ride inside the package instead
    # of in a folder beside it that anyone could move away from it.
    writer = idml.IdmlWriter(
        document,
        wrap_images=wrap_images,
        facing_pages=facing_pages,
    )
    try:
        writer.write(destination)
    except idml.MalformedPartError as exc:
        raise ConversionError(str(exc)) from exc
    except OSError as exc:
        raise ConversionError(f"IDML write failed: {exc}") from exc

    result.output = destination
    result.facing_pages = bool(facing_pages)
    result.facing_detected = detected and bool(facing_pages)
    result.pages = len(document.pages)
    result.fonts = document.fonts
    result.warnings = list(document.warnings)

    _count_content(document, result)

    log.info(
        "%s: ok in %.2fs - %d pages, %d frames, %d images, %d shapes, %d chars, fonts=%s",
        source.name, time.monotonic() - started, result.pages, result.text_frames,
        result.images, result.shapes, result.characters, ", ".join(result.fonts) or "none",
    )
    for warning in result.warnings:
        log.warning("%s: %s", source.name, warning)
