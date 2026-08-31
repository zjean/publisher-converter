"""Single-file conversion: .pub in, .idml (plus linked images) out."""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import threading
import time
import subprocess
import sys
from dataclasses import dataclass, field
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
    names = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
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
                name=names[len(masters) % len(names)],
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

    tables = cells = 0
    for item in document.all_items():
        if not isinstance(item, model.Table):
            continue
        insets = structure.cell_insets(item.column_widths, item.row_heights)
        if insets is None:
            continue
        alignments = (
            structure.cell_alignments(item.column_widths, item.row_heights) or {}
        )
        tables += 1
        for cell in item.cells:
            found = insets.get((cell.row, cell.column))
            if found is None:
                continue
            cell.insets = model.CellInsets(*found)
            cell.unruled = True
            cell.vertical_align = alignments.get((cell.row, cell.column))
            cells += 1

    if tables:
        log.info("cell insets read for %d table(s), %d cell(s)", tables, cells)
        document.warnings.append(
            f"{tables} table(s) written with every cell rule off: their "
            f"{cells} cell record(s) state padding, alignment and cached "
            f"extents but no rule anywhere in the corpus, and a cell edge "
            f"left unstated is one the reader rules "
            f"itself. Publisher keeps cell rules and shading outside those "
            f"records, in the Escher stream (actions.md §11), and they are "
            f"not read yet — re-add any lines and fills by hand"
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
            if found is None or len(found.stops) < 2:
                continue
            item.style.gradient = model.Gradient(
                stops=tuple(
                    model.GradientStop(location=position * 100.0, color=colour)
                    for position, colour in found.stops
                ),
                # A flattened fill never became a Gradient at all, so its
                # angle went with the ramp; the file states it, and
                # `pubfile` hands it over the way libmspub would have.
                #
                # Plus the shape's own turn, because Publisher turns a shape
                # and its shade together and libmspub reports the two
                # apart: a polygon's turn goes into the order of its points,
                # where the ramp cannot see it, which leaves every band in a
                # newsletter shading the wrong way up. Where the reader
                # turns the object itself the ramp goes round with it, so
                # what the angle owes is the turn the item is *not* already
                # carrying.
                angle=model._fold_angle(
                    found.angle + found.rotation - item.rotation
                ),
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

    # The frame is the band, exactly. Making it taller so a headline
    # wrapped by a substituted font still had somewhere to go was tried
    # and taken back out: it only holds the words in place if the reader
    # centres them vertically, and if it does not, the headline hangs
    # half a band high instead -- a certain error traded for a possible
    # one. Centring inside the band is safe either way, because a reader
    # that ignores it lands on the top of the band, which is where the
    # words used to be put anyway.
    frame = model.TextFrame(
        x=page_width / 2.0 + art.centre_x - art.width / 2.0,
        y=page_height / 2.0 + art.centre_y - art.height / 2.0,
        width=art.width,
        height=art.height,
        rotation=art.rotation,
        # WordArt fits its glyphs to the shape, so the band is not a box
        # the words sit somewhere inside -- it *is* the words, and their
        # centre is its centre. Straight text is the most that can come
        # across, and centred in the band is where that lands closest;
        # left and top would hang the headline off one corner with the
        # space the stretch used to fill left empty beside it.
        vertical_align="center",
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
    for line in lines:
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
            line_spacing_pt=art.height / max(len(lines), 1),
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


def _check_unrenderable_paths(document: model.Document) -> None:
    """Report filled paths whose outlines enclose nothing.

    What is left here after `_recover_wordart` is a path whose guides
    matched no WordArt shape, so there is nothing to say about what it
    outlined. Joining the edges instead would invent geometry, and used to
    draw a filled bowtie across the page, so they are kept apart and the
    loss is named.
    """
    unrenderable = 0
    for page in document.pages:
        for item in model._walk(page.items):
            if _is_edge_only_fill(item):
                unrenderable += 1

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
    """
    for page in document.pages:
        for item in model._walk(page.items):
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
    facing_pages: bool = False,
) -> Result:
    """Convert one .pub file to an .idml package.

    Linked images are written to a sibling folder named after the output
    file, so `report.idml` is accompanied by `report_images/`.
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
    facing_pages: bool,
) -> None:
    started = time.monotonic()
    log.info("converting %s -> %s", source, destination)

    document = parse_document(source, pubdump)

    if not document.pages:
        raise ConversionError("document contains no pages")

    textrepair.repair_document(document, codepage)
    structure = pubfile.read_structure(source)
    _note_unreadable_structure(document, structure)
    _apply_master_pages(document, structure)
    _apply_cell_insets(document, structure)
    _apply_page_margins(document, structure)
    # Before the WordArt pass, which takes a shape's paint as it finds it.
    _restore_gradient_ramps(document, structure)
    _recover_wordart(document, structure)
    _rasterise_metafiles(document)
    # After the master pass: threading empties the continuation frames, and
    # a run of identical empty frames is exactly what master lifting looks
    # for, so doing this first would sweep the chain onto a master spread.
    _thread_duplicate_stories(document, structure)
    # After threading, so that a paragraph is counted once rather than once
    # per frame the story was copied into before the links were made.
    _apply_tab_stops(document, structure)
    _check_unrenderable_paths(document)
    _check_gradient_losses(document)
    _check_unnamed_languages(document)
    _check_overset_text(document)

    writer = idml.IdmlWriter(
        document,
        image_dir_name=f"{destination.stem}_images",
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
