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
from typing import Dict, List, Optional

from . import idml, logsetup, metafile, model, pubfile, textrepair, wmf

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


def _attribute_masters(
    document: model.Document, structure: "pubfile.FileStructure"
) -> Optional[List[List[model.Item]]]:
    """Which leading items on each page came from that page's master.

    Returns None when the attribution cannot be trusted, which is the
    common case for anything unusual: the two halves must line up page for
    page, and pages sharing a master must have been given the same shapes.
    """
    if len(structure.pages) != len(document.pages):
        log.info("page structure did not align with the event stream")
        return None

    attributed: List[tuple] = []
    for index, page in enumerate(document.pages):
        master = structure.master_for(index)
        if master is None or not master.shape_count:
            attributed.append(([], None))
            continue
        items = _master_items(page, master.shape_count)
        # The sheet is part of the identity as well as the shapes: content
        # can only be shared by pages of one size, or lifting it would put
        # it on a page of the wrong dimensions.
        signature = (
            tuple(_shape_signature(i) for i in items),
            round(page.width, 3),
            round(page.height, 3),
        )
        attributed.append((items, (master.seq, signature)))

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
    lifted = numbered = 0

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
        tables += 1
        for cell in item.cells:
            found = insets.get((cell.row, cell.column))
            if found is None:
                continue
            cell.insets = model.CellInsets(*found)
            cells += 1

    if tables:
        log.info("cell insets read for %d table(s), %d cell(s)", tables, cells)


def _tabbed_paragraphs(document: model.Document):
    """Every paragraph in the document that contains a tab."""
    for item in document.all_items():
        stories = []
        if isinstance(item, model.TextFrame):
            stories.append(item.story)
        elif isinstance(item, model.Table):
            stories += [cell.story for cell in item.cells]
        for story in stories:
            for paragraph in story.paragraphs:
                if "\t" in paragraph.text():
                    yield paragraph


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

    What is left over is named rather than passed over in silence: in this
    corpus most tabs belong to paragraphs the file records no stop for at
    all, and those still land on the reader's grid.
    """
    if structure is None or not structure.paragraph_stops:
        return

    stated: Dict[str, Optional[tuple]] = {}
    for text, stops in structure.paragraph_stops:
        key = model.clean_text(text)
        # A text that turns up twice is only usable while both agree, and a
        # paragraph stating no stops disagrees with one that states some.
        if stated.setdefault(key, stops) != stops:
            stated[key] = None

    placed = unplaced = 0
    for paragraph in _tabbed_paragraphs(document):
        stops = stated.get(paragraph.text())
        if not stops:
            unplaced += 1
            continue
        paragraph.tab_stops = [
            model.TabStop(position=position, alignment=alignment)
            for position, alignment in stops
        ]
        placed += 1

    if placed:
        log.info("tab stops read for %d paragraph(s)", placed)
    if unplaced:
        document.warnings.append(
            f"{unplaced} paragraph(s) use tabs the file states no stop for: "
            f"Publisher lined them up on its own default grid, which is not "
            f"recorded anywhere in the file, so they fall on the reader's "
            f"instead and anything tabbed into columns needs checking"
        )


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


def _thread_duplicate_stories(document: model.Document) -> None:
    """Collapse a story duplicated across linked frames into one chain.

    Publisher lets an article flow through a row of linked text boxes.
    librevenge's drawing interface has no way to say "this frame continues
    that one", so libmspub hands the *complete* story to every frame in the
    chain -- one sample carries an 11,121-character article eight times, 73%
    of that file's apparent text. Written through verbatim, the reader gets
    the article once per frame, each one overset.

    The frames are threaded instead: the first keeps the text and the rest
    continue it, which is what IDML models natively.

    Identical text alone is not enough to infer a chain -- a page-number
    field repeats a single "#" across every page, and a running header
    repeats its title. Those are genuine copies, and blanking all but the
    first would erase them. What distinguishes a chain is that the story
    cannot fit the frame holding it: that is *why* the boxes were linked.
    So a group is threaded only when the text oversets even the roomiest
    frame in it, which leaves repeated labels alone.
    """
    groups: dict = {}
    for page in document.pages:
        for item in model._walk(page.items):
            if not isinstance(item, model.TextFrame):
                continue
            text = _frame_text(item)
            if text.strip():
                groups.setdefault(text, []).append(item)

    for text, frames in groups.items():
        if len(frames) < 2:
            continue
        if len(text) <= max(_frame_capacity(frame) for frame in frames):
            continue

        chain_id = f"chain{len(document.text_chains) + 1}"
        document.text_chains[chain_id] = frames
        for position, frame in enumerate(frames):
            frame.chain_id = chain_id
            if position:
                # The text lives on the first link; the others continue it.
                frame.story = model.Story()

    if document.text_chains:
        threaded = sum(len(c) for c in document.text_chains.values())
        document.warnings.append(
            f"{threaded} linked text frame(s) threaded into "
            f"{len(document.text_chains)} story/stories: check where the text "
            f"breaks between frames"
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

    What cannot come across is WordArt itself -- IDML has no warped or
    stretched type -- so a headline that was arched or shadowed into a
    shape arrives as straight text in a box and may need restyling.
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
    fitted = sum(1 for art_id in groups if by_id[art_id].fitted)
    repainted = len(superseded)
    placed = set(groups)

    if recovered:
        detail = (
            f", {fitted} of them sized from that band because the file states none"
            if fitted else ""
        )
        if repainted:
            detail += (
                f", and {repainted} repeated paint(s) of the same guides dropped "
                f"rather than left drawing rules across the words"
            )
        document.warnings.append(
            f"{recovered} WordArt headline(s) recovered as ordinary text: "
            f"Publisher stores the words in the Escher stream and libmspub "
            f"reports only the band they were stretched into, so they arrive "
            f"as straight text in that band and may need restyling{detail}"
        )

    # A WordArt shape libmspub reported nothing at all for. Its words are in
    # the file and its band says where they went, but with nothing in the
    # event stream to confirm either, placing it would be placing content on
    # the strength of the .pub alone. Naming it is what stops it being a
    # silent loss.
    missing = [art for art in structure.wordart if id(art) not in placed]
    if missing:
        words = ", ".join(
            repr(" ".join(art.text.split())[:40]) for art in missing[:3]
        )
        if len(missing) > 3:
            words += f" and {len(missing) - 3} more"
        document.warnings.append(
            f"{len(missing)} WordArt shape(s) not placed: libmspub reports no "
            f"shape where the file puts them, so {words} need retyping"
        )


def _wordart_frame(
    paths: List[model.Path],
    art: "pubfile.WordArt",
    page_width: float,
    page_height: float,
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

    lines = re.split(r"\r\n|\r|\n", art.text)

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
        # Only the shadow carries over: a fill here would paint a solid
        # block of the text colour across the band.
        style=model.GraphicStyle(shadow=first(lambda s: s.shadow)),
    )
    # A WordArt headline can be set on more than one line, and states the
    # break as the same CR LF Publisher uses in body text; one paragraph
    # per line is what that means.
    for line in lines:
        paragraph = model.Paragraph(align="center")
        paragraph.spans.append(
            model.Span(
                text=model.clean_text(line),
                font=art.font,
                size_pt=art.size,
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
            )
        )
        frame.story.paragraphs.append(paragraph)
    return frame


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

    libmspub sometimes reports a degenerate size for a text object — one
    file in the sample set has a 5.5 x 5.7 pt frame carrying 3,869
    characters, which Affinity renders as an empty box. Guessing the
    intended geometry would be inventing layout, so the frame is left
    alone and the operator is told exactly which file needs a human.

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
                    f"(libmspub reported a degenerate size; resize it in Affinity)"
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
    _apply_master_pages(document, structure)
    _apply_cell_insets(document, structure)
    _recover_wordart(document, structure)
    _rasterise_metafiles(document)
    # After the master pass: threading empties the continuation frames, and
    # a run of identical empty frames is exactly what master lifting looks
    # for, so doing this first would sweep the chain onto a master spread.
    _thread_duplicate_stories(document)
    # After threading, so that a paragraph is counted once rather than once
    # per frame the story was copied into before the links were made.
    _apply_tab_stops(document, structure)
    _check_unrenderable_paths(document)
    _check_gradient_losses(document)
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
