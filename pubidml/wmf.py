"""Windows metafile drawing records, translated into model shapes.

Publisher stores clip-art as WMF, which has no IDML representation and no
converter worth depending on: emf2svg-conv reads EMF only, ImageMagick
delegates WMF to LibreOffice, and the shipped artefact is a Windows
executable where none of that is present. Rasterising it here would mean
writing a scanline renderer.

Translating is both cheaper and better. The writer already turns
`model.Polygon`, `model.Path`, `model.Rectangle` and `model.Ellipse` into
IDML `PathGeometry`, so the artwork can arrive as editable vectors rather
than as a picture.

That is only tractable because the vocabulary is small. Every WMF record
in the sample corpus is one of 22 types, and only six of them draw:

    META_POLYGON  META_POLYLINE  META_POLYPOLYGON
    META_RECTANGLE  META_ELLIPSE  META_LINETO

The rest set up the object table (pens and brushes), the coordinate
window, or device state. Anything outside that -- text, arcs, regions,
bitmap blits -- is counted and reported rather than silently skipped, so
the conversion never claims artwork it did not carry.

Two deliberate simplifications, both visible in the output rather than
hidden:

  * A META_POLYPOLYGON becomes one polygon per ring, in paint order,
    because the writer emits a single subpath per shape. WMF clip-art
    composites by overpainting rather than by even-odd holes, so this
    reproduces the usual case; a genuine knockout hole would fill.
  * Only the anisotropic window mapping is implemented, which is what the
    corpus uses. A metafile that sets no window extent is fitted to its
    frame from its own bounds.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import metafile, model

META_EOF = 0x0000
META_SAVEDC = 0x001E
META_RESTOREDC = 0x0127
META_SELECTOBJECT = 0x012D
META_DELETEOBJECT = 0x01F0
META_SETWINDOWORG = 0x020B
META_SETWINDOWEXT = 0x020C
META_LINETO = 0x0213
META_MOVETO = 0x0214
META_CREATEPENINDIRECT = 0x02FA
META_CREATEBRUSHINDIRECT = 0x02FC
META_POLYGON = 0x0324
META_POLYLINE = 0x0325
META_ELLIPSE = 0x0418
META_RECTANGLE = 0x041B
META_POLYPOLYGON = 0x0538

PS_NULL = 5
BS_NULL = 1

_MAX_POINTS = 20000  # a ring larger than this is a corrupt count, not artwork


@dataclass
class Artwork:
    """Shapes recovered from a metafile, with an honest record count."""

    items: List[model.Item] = field(default_factory=list)
    converted: int = 0
    unsupported: int = 0


@dataclass
class _Pen:
    colour: Optional[model.Color] = None
    width: float = 0.0


@dataclass
class _Brush:
    colour: Optional[model.Color] = None


def _colour(value: int) -> model.Color:
    """A WMF ColorRef is 0x00bbggrr."""
    return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def _records(data: bytes, start: int):
    """Yield (function, payload) for each record, stopping at EOF.

    A record must be at least the three-word header; anything shorter is
    corrupt and would loop forever, so it ends the walk.
    """
    offset = start
    while offset + 6 <= len(data):
        size_words, function = struct.unpack_from("<IH", data, offset)
        if size_words < 3:
            return
        end = offset + size_words * 2
        if end > len(data):
            end = len(data)
        if function == META_EOF:
            return
        yield function, data[offset + 6:end]
        offset += size_words * 2


def _points(payload: bytes, offset: int, count: int) -> List[Tuple[int, int]]:
    """Read `count` 16-bit signed pairs, stopping short if truncated."""
    points = []
    for index in range(min(count, _MAX_POINTS)):
        at = offset + index * 4
        if at + 4 > len(payload):
            break
        points.append(struct.unpack_from("<hh", payload, at))
    return points


class _Interpreter:
    """Replays the records, accumulating shapes.

    Publisher's own output is the reference: window mapping first, then an
    object table of pens and brushes selected by index, then the drawing
    records that consume whatever is currently selected.
    """

    def __init__(self, box: Tuple[float, float, float, float]):
        self.x, self.y, self.width, self.height = box
        self.origin = (0, 0)
        self.extent: Optional[Tuple[int, int]] = None
        # Slot-based, not a stack: DeleteObject frees a slot and the next
        # create takes the lowest free one, so indices stay stable.
        self.objects: List[Optional[object]] = []
        self.pen = _Pen()
        self.brush = _Brush()
        self.saved: List[Tuple[_Pen, _Brush]] = []
        self.position: Optional[Tuple[int, int]] = None
        self.run: List[Tuple[int, int]] = []
        # (logical points, closed) in the order they were painted
        self.shapes: List[Tuple[str, List[Tuple[int, int]], bool, _Pen, _Brush]] = []
        self.converted = 0
        self.unsupported = 0

    # -- object table -----------------------------------------------------

    def _add(self, obj: object) -> None:
        for index, slot in enumerate(self.objects):
            if slot is None:
                self.objects[index] = obj
                return
        self.objects.append(obj)

    def _select(self, index: int) -> None:
        if 0 <= index < len(self.objects):
            obj = self.objects[index]
            if isinstance(obj, _Pen):
                self.pen = obj
            elif isinstance(obj, _Brush):
                self.brush = obj

    # -- shape accumulation -----------------------------------------------

    def _emit(self, kind: str, points: List[Tuple[int, int]], closed: bool) -> None:
        if len(points) < 2:
            return
        self.shapes.append((kind, points, closed, self.pen, self.brush))
        self.converted += 1

    def _flush_run(self) -> None:
        if len(self.run) >= 2:
            self.shapes.append(("poly", list(self.run), False, self.pen, self.brush))
            self.converted += 1
        self.run = []

    # -- replay -----------------------------------------------------------

    def run_records(self, data: bytes, start: int) -> None:
        for function, payload in _records(data, start):
            # A LineTo run is only complete once something else happens.
            if function not in (META_LINETO, META_MOVETO) and self.run:
                self._flush_run()
            self._dispatch(function, payload)
        self._flush_run()

    def _dispatch(self, function: int, payload: bytes) -> None:
        if function == META_SETWINDOWORG and len(payload) >= 4:
            y, x = struct.unpack_from("<hh", payload, 0)
            self.origin = (x, y)
        elif function == META_SETWINDOWEXT and len(payload) >= 4:
            height, width = struct.unpack_from("<hh", payload, 0)
            self.extent = (width, height)
        elif function == META_CREATEPENINDIRECT and len(payload) >= 10:
            style, width, _wy, colour = struct.unpack_from("<HhhI", payload, 0)
            self._add(
                _Pen(None if style == PS_NULL else _colour(colour), abs(width))
            )
        elif function == META_CREATEBRUSHINDIRECT and len(payload) >= 8:
            style, colour, _hatch = struct.unpack_from("<HIH", payload, 0)
            self._add(_Brush(None if style == BS_NULL else _colour(colour)))
        elif function == META_SELECTOBJECT and len(payload) >= 2:
            self._select(struct.unpack_from("<H", payload, 0)[0])
        elif function == META_DELETEOBJECT and len(payload) >= 2:
            index = struct.unpack_from("<H", payload, 0)[0]
            if 0 <= index < len(self.objects):
                self.objects[index] = None
        elif function == META_SAVEDC:
            self.saved.append((self.pen, self.brush))
        elif function == META_RESTOREDC:
            if self.saved:
                self.pen, self.brush = self.saved.pop()
        elif function == META_MOVETO and len(payload) >= 4:
            y, x = struct.unpack_from("<hh", payload, 0)
            self._flush_run()
            self.run = [(x, y)]
        elif function == META_LINETO and len(payload) >= 4:
            y, x = struct.unpack_from("<hh", payload, 0)
            self.run.append((x, y))
        elif function in (META_POLYGON, META_POLYLINE) and len(payload) >= 2:
            count = struct.unpack_from("<H", payload, 0)[0]
            self._emit("poly", _points(payload, 2, count), function == META_POLYGON)
        elif function == META_POLYPOLYGON and len(payload) >= 2:
            self._polypolygon(payload)
        elif function in (META_RECTANGLE, META_ELLIPSE) and len(payload) >= 8:
            bottom, right, top, left = struct.unpack_from("<4h", payload, 0)
            kind = "rect" if function == META_RECTANGLE else "ellipse"
            self._emit(kind, [(left, top), (right, bottom)], True)
        elif function in _STATE_RECORDS:
            pass
        else:
            self.unsupported += 1

    def _polypolygon(self, payload: bytes) -> None:
        """Every ring's point count comes first, then every ring's points.

        Reading those interleaved still yields plausible-looking geometry,
        which is exactly why it is worth being explicit about.
        """
        rings = struct.unpack_from("<H", payload, 0)[0]
        if rings > _MAX_POINTS:
            self.unsupported += 1
            return
        counts = []
        for index in range(rings):
            at = 2 + index * 2
            if at + 2 > len(payload):
                break
            counts.append(struct.unpack_from("<H", payload, at)[0])

        offset = 2 + len(counts) * 2
        for count in counts:
            points = _points(payload, offset, count)
            self._emit("poly", points, True)
            offset += count * 4

    # -- output -----------------------------------------------------------

    def _mapper(self):
        """Logical coordinates to points on the page."""
        if self.extent and self.extent[0] and self.extent[1]:
            scale_x = self.width / self.extent[0]
            scale_y = self.height / self.extent[1]
            origin_x, origin_y = self.origin
        else:
            # No window declared: fit the drawing's own bounds to the frame
            # so it lands inside it rather than at an arbitrary scale.
            xs = [p[0] for _, points, _, _, _ in self.shapes for p in points]
            ys = [p[1] for _, points, _, _, _ in self.shapes for p in points]
            if not xs:
                return None
            origin_x, origin_y = min(xs), min(ys)
            span_x = (max(xs) - origin_x) or 1
            span_y = (max(ys) - origin_y) or 1
            scale_x = self.width / span_x
            scale_y = self.height / span_y

        def project(point):
            return (
                self.x + (point[0] - origin_x) * scale_x,
                self.y + (point[1] - origin_y) * scale_y,
            )

        return project

    def artwork(self) -> Optional[Artwork]:
        if not self.shapes:
            return None
        project = self._mapper()
        if project is None:
            return None

        art = Artwork(converted=self.converted, unsupported=self.unsupported)
        for kind, points, closed, pen, brush in self.shapes:
            mapped = [project(point) for point in points]
            xs = [p[0] for p in mapped]
            ys = [p[1] for p in mapped]
            style = model.GraphicStyle(
                fill=brush.colour,
                stroke=pen.colour,
                stroke_width=max(0.25, pen.width) if pen.colour else 0.0,
            )
            box = dict(
                x=min(xs), y=min(ys),
                width=max(xs) - min(xs), height=max(ys) - min(ys),
                style=style,
            )
            if kind == "rect":
                art.items.append(model.Rectangle(**box))
            elif kind == "ellipse":
                art.items.append(model.Ellipse(**box))
            else:
                art.items.append(model.Polygon(points=mapped, closed=closed, **box))
        return art


# Records that only set device state; they paint nothing and are expected.
_STATE_RECORDS = frozenset(
    {
        0x0035,  # META_REALIZEPALETTE
        0x00F7,  # META_CREATEPALETTE
        0x0102,  # META_SETBKMODE
        0x0103,  # META_SETMAPMODE
        0x0104,  # META_SETROP2
        0x0106,  # META_SETPOLYFILLMODE
        0x0107,  # META_SETSTRETCHBLTMODE
        0x0108,  # META_SETTEXTCHAREXTRA
        0x012C,  # META_SELECTCLIPREGION
        0x012E,  # META_SETTEXTALIGN
        0x0201,  # META_SETBKCOLOR
        0x0209,  # META_SETTEXTCOLOR
        0x020D,  # META_SETVIEWPORTORG
        0x020E,  # META_SETVIEWPORTEXT
        0x020F,  # META_OFFSETWINDOWORG
        0x0211,  # META_OFFSETVIEWPORTORG
        0x0220,  # META_OFFSETCLIPRGN
        0x0231,  # META_SETMAPPERFLAGS
        0x0234,  # META_SELECTPALETTE
        0x02FB,  # META_CREATEFONTINDIRECT
        0x0410,  # META_SCALEWINDOWEXT
        0x0412,  # META_SCALEVIEWPORTEXT
        0x0416,  # META_INTERSECTCLIPRECT
        0x0626,  # META_ESCAPE
        0x06FF,  # META_CREATEREGION
    }
)


def to_items(
    data: bytes, x: float, y: float, width: float, height: float
) -> Optional[Artwork]:
    """Translate a WMF into page items, or None if there is nothing to draw.

    `x`, `y`, `width` and `height` are the frame the picture was placed in;
    the metafile's logical window is mapped onto it.
    """
    info = metafile.inspect(data)
    if info.kind != "wmf" or not info.valid or info.is_empty:
        return None
    if width <= 0 or height <= 0:
        # A degenerate frame would scale everything to a point. Keep the
        # artwork at its logical size and let the operator resize it.
        width = width or 72.0
        height = height or 72.0

    start = metafile.WMF_HEADER
    if len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == metafile.WMF_PLACEABLE_KEY:
        start += metafile.WMF_ALDUS_HEADER

    interpreter = _Interpreter((x, y, width, height))
    interpreter.run_records(data, start)
    return interpreter.artwork()
