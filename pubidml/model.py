"""Document model reconstructed from a pubdump event stream.

libmspub reports a Publisher document as a flat sequence of drawing
callbacks. This module replays that sequence into an explicit tree of
pages and page items, resolving the implicit state librevenge carries
between calls (the pending graphic style, the open text object, the open
paragraph and span).

The model is deliberately format-neutral: it knows about frames, shapes
and runs of styled text, not about IDML.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import metafile, units

Color = Tuple[int, int, int]

# Publisher files routinely carry frames a little outside the page, but a
# frame thousands of points away is a parse artefact rather than design
# intent. Items beyond this margin are dropped and counted as warnings.
_SANITY_MARGIN_PT = 20000.0


def parse_color(value: Optional[str]) -> Optional[Color]:
    """Parse librevenge's '#rrggbb' colour notation."""
    if not value:
        return None
    text = str(value).strip()
    if not text.startswith("#") or len(text) != 7:
        return None
    try:
        return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
    except ValueError:
        return None


@dataclass
class GraphicStyle:
    """The fill/stroke state librevenge sets before each draw call."""

    fill: Optional[Color] = None
    fill_opacity: float = 1.0
    stroke: Optional[Color] = None
    stroke_width: float = 0.0
    stroke_opacity: float = 1.0
    # True when the source declared a gradient we cannot represent; the
    # writer substitutes the first stop and the converter reports it.
    approximated_fill: bool = False
    # Publisher pictures never arrive as drawGraphicObject. libmspub
    # reports them as a bitmap fill on the shape that follows, so the
    # image payload has to be carried on the graphic style.
    fill_image: Optional[bytes] = None
    fill_image_mime: str = "image/png"
    fill_image_rotation: float = 0.0
    fill_image_luminance: Optional[float] = None

    @classmethod
    def from_props(cls, props: dict) -> "GraphicStyle":
        style = cls()

        fill_kind = props.get("draw:fill", "none")
        if fill_kind == "solid":
            style.fill = parse_color(props.get("draw:fill-color"))
        elif fill_kind == "gradient":
            stops = props.get("svg:linearGradient") or props.get("svg:radialGradient") or []
            if stops:
                style.fill = parse_color(stops[0].get("svg:stop-color"))
            else:
                style.fill = parse_color(props.get("draw:fill-color"))
            style.approximated_fill = True
        elif fill_kind == "bitmap":
            payload = props.get("draw:fill-image")
            if payload:
                try:
                    style.fill_image = base64.b64decode(payload)
                except Exception:
                    style.fill_image = None
            style.fill_image_mime = props.get("librevenge:mime-type", "image/png")
            style.fill_image_rotation = _rotation(props)
            style.fill_image_luminance = units.percent(props.get("draw:luminance"))
            style.fill = parse_color(props.get("draw:fill-color"))

        style.fill_opacity = units.percent(props.get("draw:opacity"), 1.0) or 1.0

        if props.get("draw:stroke", "none") != "none":
            style.stroke = parse_color(props.get("svg:stroke-color"))
            style.stroke_width = units.to_points(props.get("svg:stroke-width"), 0.0) or 0.0
            style.stroke_opacity = units.percent(props.get("svg:stroke-opacity"), 1.0) or 1.0

        return style


@dataclass
class Span:
    """A run of text sharing one character format."""

    text: str = ""
    font: Optional[str] = None
    size_pt: Optional[float] = None
    color: Optional[Color] = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    superscript: bool = False
    subscript: bool = False

    def format_key(self) -> tuple:
        return (
            self.font,
            self.size_pt,
            self.color,
            self.bold,
            self.italic,
            self.underline,
            self.strikethrough,
            self.superscript,
            self.subscript,
        )


@dataclass
class Paragraph:
    align: str = "left"
    space_before: float = 0.0
    space_after: float = 0.0
    margin_left: float = 0.0
    margin_right: float = 0.0
    first_line_indent: float = 0.0
    line_height: Optional[str] = None
    list_level: int = 0
    list_ordered: bool = False
    spans: List[Span] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(span.text for span in self.spans)


@dataclass
class Story:
    paragraphs: List[Paragraph] = field(default_factory=list)

    def is_empty(self) -> bool:
        return all(paragraph.is_empty() for paragraph in self.paragraphs)


@dataclass
class Item:
    """Base page item. Geometry is in points, origin at the page's top-left."""

    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    rotation: float = 0.0
    style: GraphicStyle = field(default_factory=GraphicStyle)


@dataclass
class TextFrame(Item):
    story: Story = field(default_factory=Story)
    padding: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # t r b l
    vertical_align: str = "top"
    # Set when this frame is one link in a threaded story; every frame
    # sharing the id shows one story between them, in the order recorded in
    # Document.text_chains. Only the first link carries the text.
    chain_id: Optional[str] = None


@dataclass
class Rectangle(Item):
    corner_radius: float = 0.0


@dataclass
class Ellipse(Item):
    pass


@dataclass
class Polygon(Item):
    points: List[Tuple[float, float]] = field(default_factory=list)
    closed: bool = True


@dataclass
class Path(Item):
    # Normalised path operations: ("M"|"L"|"C"|"Q"|"Z", *coordinates)
    ops: List[tuple] = field(default_factory=list)


@dataclass
class Image(Item):
    data: bytes = b""
    mime_type: str = "image/png"
    # Publisher can rotate a picture inside its frame independently of the
    # frame itself; this is that inner rotation, in degrees.
    content_rotation: float = 0.0
    luminance: Optional[float] = None


# Formats IDML can carry and Affinity can place. Publisher also embeds
# EMF/WMF vector metafiles, which have no IDML representation.
RASTER_MIME_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/tiff",
    "image/gif",
    "image/bmp",
}

_MIME_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/emf": ".emf",
    "image/wmf": ".wmf",
    "image/x-emf": ".emf",
    "image/x-wmf": ".wmf",
}


def extension_for(mime_type: str) -> str:
    return _MIME_EXTENSION.get(mime_type, ".bin")


@dataclass
class Group(Item):
    children: List[Item] = field(default_factory=list)


@dataclass
class Page:
    width: float = 612.0
    height: float = 792.0
    items: List[Item] = field(default_factory=list)
    #: Name of the master this page applies, once the .pub's own structure
    #: has been read. libmspub replays master content onto every page, so
    #: this stays None unless that content could be lifted back out.
    master: Optional[str] = None


@dataclass
class Master:
    """Content Publisher held once and repeated on every page applying it."""

    name: str = "A"
    width: float = 612.0
    height: float = 792.0
    items: List[Item] = field(default_factory=list)


@dataclass
class Document:
    pages: List[Page] = field(default_factory=list)
    masters: List["Master"] = field(default_factory=list)
    title: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    # chain id -> the frames of one threaded story, in reading order.
    text_chains: Dict[str, List[TextFrame]] = field(default_factory=dict)
    # True once the parser's endDocument event has been seen. A stream that
    # is cut on a line boundary otherwise replays as a syntactically perfect
    # but silently short document, which would be reported as a success.
    complete: bool = False

    def all_items(self):
        """Every item in the document, masters included.

        Resources are collected document-wide, so anything reached only
        through a master -- a font used solely in a footer, say -- has to
        be visible here or it goes missing from the package.
        """
        for page in self.pages:
            yield from _walk(page.items)
        for master in self.masters:
            yield from _walk(master.items)

    @property
    def fonts(self) -> List[str]:
        found = set()
        for item in self.all_items():
            if isinstance(item, TextFrame):
                for paragraph in item.story.paragraphs:
                    for span in paragraph.spans:
                        if span.font:
                            found.add(span.font)
        return sorted(found)

    @property
    def colors(self) -> List[Color]:
        found = set()
        for item in self.all_items():
            if item.style.fill:
                found.add(item.style.fill)
            if item.style.stroke:
                found.add(item.style.stroke)
            if isinstance(item, TextFrame):
                for paragraph in item.story.paragraphs:
                    for span in paragraph.spans:
                        if span.color:
                            found.add(span.color)
        return sorted(found)


def _walk(items: List[Item]):
    """Yield every item, descending into groups."""
    for item in items:
        yield item
        if isinstance(item, Group):
            yield from _walk(item.children)


# libmspub hands back Publisher's raw control characters inside the text
# runs, and XML 1.0 permits almost none of them. Publisher uses several
# structurally — 0x0C page break, 0x0E column break, 0x13-0x15 field
# delimiters, 0x1F optional hyphen — and any one of them reaching <Content>
# produces a Story part no XML parser will open, i.e. a silently unusable
# package. Enumerating only the codes we have seen would leave the rest to
# escape, so the table strips everything XML forbids and adds back the two
# that carry meaning.
#
# The carriage return is Publisher's paragraph terminator, but the event
# stream already delimits paragraphs with openParagraph/closeParagraph, so
# leaving it in makes IDML see an explicit break mid-paragraph — which
# fully justifies the line before it, spreading the last line of every
# justified paragraph across the measure. Vertical tab is Publisher's
# forced line break and maps to the Unicode line separator.
def _build_text_translation() -> dict:
    table = {code: None for code in range(0x20)}
    table[0x09] = 0x09    # tab is structural in IDML; idml.py splits runs on it
    table[0x0B] = 0x2028  # forced line break -> Unicode line separator
    table[0x7F] = None    # DEL: legal XML, but never meaningful in body copy
    # Lone surrogates and the two non-characters are illegal in XML 1.0 and
    # can reach here through a \u escape in the event stream.
    table.update({code: None for code in range(0xD800, 0xE000)})
    table[0xFFFE] = None
    table[0xFFFF] = None
    return table


_TEXT_TRANSLATION = _build_text_translation()


def clean_text(text: str) -> str:
    return text.translate(_TEXT_TRANSLATION)


_ALIGN_MAP = {
    "left": "left",
    "start": "left",
    "right": "right",
    "end": "right",
    "center": "center",
    "justify": "justify",
}


class ModelBuilder:
    """Replays a pubdump event stream into a Document."""

    def __init__(self) -> None:
        self.doc = Document()
        self._page: Optional[Page] = None
        self._style = GraphicStyle()
        self._frame: Optional[TextFrame] = None
        self._paragraph: Optional[Paragraph] = None
        self._span: Optional[Span] = None
        self._group_stack: List[Group] = []
        self._list_stack: List[bool] = []  # True where the level is ordered
        self._in_master = False
        self._dropped = 0

    # -- event dispatch ---------------------------------------------------

    def feed(self, event: str, props: dict, text: Optional[str]) -> None:
        handler = getattr(self, f"_on_{event}", None)
        if handler is None:
            return
        if text is not None:
            handler(text)
        elif props is not None:
            handler(props)
        else:
            handler()

    def finish(self) -> Document:
        if self._dropped:
            self.doc.warnings.append(
                f"{self._dropped} item(s) dropped: geometry outside sane bounds"
            )
        return self.doc

    # -- document / page --------------------------------------------------

    def _on_endDocument(self) -> None:
        self.doc.complete = True

    def _on_setDocumentMetaData(self, props: dict) -> None:
        self.doc.title = props.get("dc:title") or props.get("dc:subject")

    def _on_startPage(self, props: dict) -> None:
        self._page = Page(
            width=units.to_points(props.get("svg:width"), 612.0) or 612.0,
            height=units.to_points(props.get("svg:height"), 792.0) or 792.0,
        )
        self.doc.pages.append(self._page)

    def _on_endPage(self) -> None:
        self._page = None

    def _on_startMasterPage(self, props: dict) -> None:
        # Kept as a guard, not because it fires: libmspub 0.1.5 never calls
        # librevenge's master-page callbacks at all. It resolves masters
        # internally instead and replays their shapes onto each page ahead
        # of that page's own -- MSPUBCollector::writePage does
        # writePageShapes(master) then writePageShapes(page) -- so header
        # and footer frames arrive as ordinary items and are preserved.
        # Verified against the 0.1.5 source and against a 15-page sample
        # whose footer frame is identical on every page.
        #
        # If a future libmspub starts announcing masters, this stops the
        # content being captured twice. Until then it is unreachable.
        self._in_master = True

    def _on_endMasterPage(self) -> None:
        self._in_master = False

    # -- graphic state ----------------------------------------------------

    def _on_setStyle(self, props: dict) -> None:
        self._style = GraphicStyle.from_props(props)

    def _on_openGroup(self, props: dict) -> None:
        group = Group(style=self._style)
        self._group_stack.append(group)

    def _on_closeGroup(self) -> None:
        if not self._group_stack:
            return
        group = self._group_stack.pop()
        if group.children:
            _fit_group_bounds(group)
            self._place(group)

    # -- shapes -----------------------------------------------------------

    def _on_drawRectangle(self, props: dict) -> None:
        item = Rectangle(
            corner_radius=units.to_points(props.get("draw:corner-radius"), 0.0) or 0.0
        )
        self._apply_box(item, props)
        self._place(item)

    def _on_drawEllipse(self, props: dict) -> None:
        cx = units.to_points(props.get("svg:cx"), 0.0) or 0.0
        cy = units.to_points(props.get("svg:cy"), 0.0) or 0.0
        rx = units.to_points(props.get("svg:rx"), 0.0) or 0.0
        ry = units.to_points(props.get("svg:ry"), 0.0) or 0.0
        item = Ellipse(
            x=cx - rx,
            y=cy - ry,
            width=rx * 2,
            height=ry * 2,
            rotation=_rotation(props),
            style=self._style,
        )
        self._place(item)

    def _on_drawPolygon(self, props: dict) -> None:
        self._polyline(props, closed=True)

    def _on_drawPolyline(self, props: dict) -> None:
        self._polyline(props, closed=False)

    def _polyline(self, props: dict, closed: bool) -> None:
        raw = props.get("svg:points") or []
        points = [
            (
                units.to_points(entry.get("svg:x"), 0.0) or 0.0,
                units.to_points(entry.get("svg:y"), 0.0) or 0.0,
            )
            for entry in raw
        ]
        if len(points) < 2:
            return
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        item = Polygon(
            x=min(xs),
            y=min(ys),
            width=max(xs) - min(xs),
            height=max(ys) - min(ys),
            rotation=_rotation(props),
            style=self._style,
            points=points,
            closed=closed,
        )
        self._place(item)

    def _on_drawPath(self, props: dict) -> None:
        ops, xs, ys = _normalise_path(props.get("svg:d") or [])
        if not ops or not xs:
            return
        item = Path(
            x=min(xs),
            y=min(ys),
            width=max(xs) - min(xs),
            height=max(ys) - min(ys),
            rotation=_rotation(props),
            style=self._style,
            ops=ops,
        )
        self._place(item)

    def _on_drawConnector(self, props: dict) -> None:
        self._on_drawPath(props)

    def _on_drawGraphicObject(self, props: dict) -> None:
        payload = props.get("office:binary-data")
        if not payload:
            return
        try:
            data = base64.b64decode(payload)
        except Exception:
            self.doc.warnings.append("image dropped: undecodable binary payload")
            return
        item = Image(
            data=data,
            mime_type=props.get("librevenge:mime-type", "image/png"),
            rotation=_rotation(props),
        )
        self._apply_box(item, props)
        self._place(item)

    # -- text -------------------------------------------------------------

    def _on_startTextObject(self, props: dict) -> None:
        frame = TextFrame(
            padding=(
                units.to_points(props.get("fo:padding-top"), 0.0) or 0.0,
                units.to_points(props.get("fo:padding-right"), 0.0) or 0.0,
                units.to_points(props.get("fo:padding-bottom"), 0.0) or 0.0,
                units.to_points(props.get("fo:padding-left"), 0.0) or 0.0,
            ),
            vertical_align=props.get("draw:textarea-vertical-align", "top"),
            rotation=_rotation(props),
        )
        self._apply_box(frame, props)
        self._frame = frame

    def _on_endTextObject(self) -> None:
        frame, self._frame = self._frame, None
        self._paragraph = None
        self._span = None
        if frame is None:
            return
        # An empty frame with no fill or stroke contributes nothing.
        if frame.story.is_empty() and not frame.style.fill and not frame.style.stroke:
            return
        self._place(frame)

    # Tables reuse the text-frame machinery: IDML tables are a large
    # feature and Publisher uses them rarely, so the cells are flowed into
    # a single frame as paragraphs. That keeps the copy rather than losing
    # it, and the warning tells the operator the grid needs rebuilding.
    def _on_startTableObject(self, props: dict) -> None:
        self.doc.warnings.append(
            "table flattened to paragraphs: cell structure not preserved"
        )
        self._on_startTextObject(props)

    def _on_endTableObject(self) -> None:
        self._on_endTextObject()

    def _on_openTableCell(self, props: dict) -> None:
        self._paragraph = None
        self._span = None

    def _on_closeTableCell(self) -> None:
        self._paragraph = None
        self._span = None

    def _on_openParagraph(self, props: dict) -> None:
        if self._frame is None:
            return
        paragraph = Paragraph(
            align=_ALIGN_MAP.get(props.get("fo:text-align", "left"), "left"),
            space_before=units.to_points(props.get("fo:margin-top"), 0.0) or 0.0,
            space_after=units.to_points(props.get("fo:margin-bottom"), 0.0) or 0.0,
            margin_left=units.to_points(props.get("fo:margin-left"), 0.0) or 0.0,
            margin_right=units.to_points(props.get("fo:margin-right"), 0.0) or 0.0,
            first_line_indent=units.to_points(props.get("fo:text-indent"), 0.0) or 0.0,
            line_height=props.get("fo:line-height"),
            list_level=len(self._list_stack),
            list_ordered=bool(self._list_stack and self._list_stack[-1]),
        )
        self._paragraph = paragraph
        self._frame.story.paragraphs.append(paragraph)

    def _on_closeParagraph(self) -> None:
        self._paragraph = None

    def _on_openSpan(self, props: dict) -> None:
        if self._paragraph is None:
            # Some documents emit spans outside an explicit paragraph.
            self._on_openParagraph({})
            if self._paragraph is None:
                return
        self._span = Span(
            font=props.get("style:font-name"),
            size_pt=units.to_points(props.get("fo:font-size")),
            color=parse_color(props.get("fo:color")),
            bold=props.get("fo:font-weight") == "bold",
            italic=props.get("fo:font-style") == "italic",
            underline=props.get("style:text-underline-type", "none") != "none",
            strikethrough=props.get("style:text-line-through-type", "none") != "none",
            superscript=str(props.get("style:text-position", "")).startswith("super"),
            subscript=str(props.get("style:text-position", "")).startswith("sub"),
        )
        self._paragraph.spans.append(self._span)

    def _on_closeSpan(self) -> None:
        self._span = None

    def _on_insertText(self, text: str) -> None:
        text = clean_text(text)
        if not text:
            return
        if self._span is None:
            if self._paragraph is None:
                return
            self._span = Span()
            self._paragraph.spans.append(self._span)
        self._span.text += text

    def _on_insertTab(self) -> None:
        self._on_insertText("\t")

    def _on_insertSpace(self) -> None:
        self._on_insertText(" ")

    def _on_insertLineBreak(self) -> None:
        # U+2028 is IDML's forced line break within a paragraph.
        self._on_insertText(" ")

    def _on_insertField(self, props: dict) -> None:
        # Page numbers and dates arrive as fields; libmspub does not supply
        # a rendered value, so emit nothing rather than a placeholder.
        return

    def _on_openOrderedListLevel(self, props: dict) -> None:
        self._list_stack.append(True)

    def _on_openUnorderedListLevel(self, props: dict) -> None:
        self._list_stack.append(False)

    def _on_closeOrderedListLevel(self) -> None:
        if self._list_stack:
            self._list_stack.pop()

    def _on_closeUnorderedListLevel(self) -> None:
        self._on_closeOrderedListLevel()

    # -- placement --------------------------------------------------------

    def _apply_box(self, item: Item, props: dict) -> None:
        item.x = units.to_points(props.get("svg:x"), 0.0) or 0.0
        item.y = units.to_points(props.get("svg:y"), 0.0) or 0.0
        item.width = units.to_points(props.get("svg:width"), 0.0) or 0.0
        item.height = units.to_points(props.get("svg:height"), 0.0) or 0.0
        if item.rotation == 0.0:
            item.rotation = _rotation(props)
        item.style = self._style

    def _promote_bitmap_fill(self, item: Item) -> Item:
        """Turn a shape carrying a bitmap fill into a placed Image.

        libmspub represents every Publisher picture this way, so this is
        the only path by which images enter the document.
        """
        style = item.style
        if style.fill_image is None or isinstance(item, (TextFrame, Group, Image)):
            return item

        if style.fill_image_mime not in RASTER_MIME_TYPES:
            if style.fill_image_mime in metafile.METAFILE_MIME_TYPES:
                # Publisher leaves an empty metafile stub wherever a picture
                # placeholder used to be. Those carry no artwork, so dropping
                # them silently is correct — warning would be noise.
                info = metafile.inspect(style.fill_image)
                if info.is_empty:
                    return item
                # Real artwork: keep it and let the rasterisation pass in
                # convert.py deal with it, where subprocesses are allowed.
                return Image(
                    x=item.x,
                    y=item.y,
                    width=item.width,
                    height=item.height,
                    rotation=item.rotation,
                    style=style,
                    data=style.fill_image,
                    mime_type=style.fill_image_mime,
                    content_rotation=style.fill_image_rotation,
                    luminance=style.fill_image_luminance,
                )
            self.doc.warnings.append(
                f"image not converted: {style.fill_image_mime} has no IDML equivalent"
            )
            return item

        return Image(
            x=item.x,
            y=item.y,
            width=item.width,
            height=item.height,
            rotation=item.rotation,
            style=style,
            data=style.fill_image,
            mime_type=style.fill_image_mime,
            content_rotation=style.fill_image_rotation,
            luminance=style.fill_image_luminance,
        )

    def _place(self, item: Item) -> None:
        if self._in_master:
            return
        item = self._promote_bitmap_fill(item)
        if not _within_sane_bounds(item):
            self._dropped += 1
            return
        if self._group_stack:
            self._group_stack[-1].children.append(item)
        elif self._page is not None:
            self._page.items.append(item)


def _rotation(props: dict) -> float:
    """Read librevenge's rotation property, in degrees clockwise.

    libmspub inserts rotation as a plain double, so librevenge stamps it
    with its default RVNG_INCH unit and the value arrives looking like
    "90.0000in". The unit is meaningless here: observed values across
    real documents are 90, -90, 270 and -46, i.e. degrees. units.to_float
    discards the bogus suffix.
    """
    return units.to_float(props.get("librevenge:rotate"), 0.0) or 0.0


def _within_sane_bounds(item: Item) -> bool:
    for value in (item.x, item.y, item.width, item.height):
        if value != value or abs(value) > _SANITY_MARGIN_PT:  # NaN or absurd
            return False
    return True


def _fit_group_bounds(group: Group) -> None:
    xs, ys, x2s, y2s = [], [], [], []
    for child in group.children:
        xs.append(child.x)
        ys.append(child.y)
        x2s.append(child.x + child.width)
        y2s.append(child.y + child.height)
    group.x, group.y = min(xs), min(ys)
    group.width = max(x2s) - group.x
    group.height = max(y2s) - group.y


def _normalise_path(raw: list):
    """Flatten librevenge path actions into (op, *coords) tuples."""
    ops, xs, ys = [], [], []

    def record(*pairs):
        for px, py in pairs:
            xs.append(px)
            ys.append(py)

    for entry in raw:
        action = entry.get("librevenge:path-action", "").upper()
        x = units.to_points(entry.get("svg:x"), 0.0) or 0.0
        y = units.to_points(entry.get("svg:y"), 0.0) or 0.0
        if action == "M":
            ops.append(("M", x, y))
            record((x, y))
        elif action == "L":
            ops.append(("L", x, y))
            record((x, y))
        elif action == "C":
            x1 = units.to_points(entry.get("svg:x1"), 0.0) or 0.0
            y1 = units.to_points(entry.get("svg:y1"), 0.0) or 0.0
            x2 = units.to_points(entry.get("svg:x2"), 0.0) or 0.0
            y2 = units.to_points(entry.get("svg:y2"), 0.0) or 0.0
            ops.append(("C", x1, y1, x2, y2, x, y))
            record((x1, y1), (x2, y2), (x, y))
        elif action == "Q":
            x1 = units.to_points(entry.get("svg:x1"), 0.0) or 0.0
            y1 = units.to_points(entry.get("svg:y1"), 0.0) or 0.0
            ops.append(("Q", x1, y1, x, y))
            record((x1, y1), (x, y))
        elif action == "A":
            # Approximate elliptical arcs with a line to the endpoint;
            # IDML has no arc primitive and Publisher uses these rarely.
            ops.append(("L", x, y))
            record((x, y))
        elif action == "Z":
            ops.append(("Z",))

    return ops, xs, ys


def build(stream) -> Document:
    """Build a Document from an iterable of pubdump JSON lines."""
    builder = ModelBuilder()
    for line in stream:
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        builder.feed(event["e"], event.get("p"), event.get("t"))
    return builder.finish()
