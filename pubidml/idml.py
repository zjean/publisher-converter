"""IDML package generation.

Writes an Adobe InDesign Markup Language package from a `model.Document`.
IDML is a ZIP of XML parts; the layout below is the minimum InDesign and
Affinity both accept:

    mimetype                     (stored uncompressed, must be first)
    META-INF/container.xml
    META-INF/metadata.xml
    designmap.xml                (manifest, references every other part)
    Resources/Fonts.xml
    Resources/Graphic.xml        (colours, swatches, stroke styles)
    Resources/Styles.xml         (paragraph/character/object styles)
    Resources/Preferences.xml
    Spreads/Spread_*.xml         (one per page)
    Stories/Story_*.xml          (one per text frame)
    XML/BackingStory.xml
    XML/Tags.xml

Coordinates
-----------
IDML measures in points with the y-axis pointing down, and each spread
has its origin at the spread centre. Page items carry their outline in
their own local space and are positioned by an affine `ItemTransform`.
Geometry is therefore emitted centred on the item's own origin, which
also makes rotation behave the way Publisher intends: about the item's
centre rather than a corner.
"""

from __future__ import annotations

import base64
import math
import os
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Literal, NamedTuple, Optional
from urllib.parse import quote

from . import fontmetrics, imagemeta, model
from .units import PT_PER_INCH, fmt

IDPKG = "http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"
DOM_VERSION = "8.0"

ET.register_namespace("idPkg", IDPKG)

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_AID_PI = (
    '<?aid style="50" type="document" readerVersion="6.0" '
    'featureSet="257" product="8.0(370)" ?>\n'
)

_JUSTIFICATION = {
    "left": "LeftAlign",
    "right": "RightAlign",
    "center": "CenterAlign",
    "justify": "LeftJustified",
}

_VERTICAL_JUSTIFICATION = {
    "top": "TopAlign",
    "middle": "CenterAlign",
    "center": "CenterAlign",
    "bottom": "BottomAlign",
}

_IMAGE_TYPE_NAME = {
    "image/jpeg": "$ID/JPEG",
    "image/jpg": "$ID/JPEG",
    "image/png": "$ID/PNG",
    "image/tiff": "$ID/TIFF",
    "image/gif": "$ID/GIF",
    "image/bmp": "$ID/BMP",
}

# RFC 3986 path characters that need no escaping: the sub-delims plus ':',
# '@' and the separator. Everything else in a filename gets percent-encoded.
_URI_PATH_SAFE = "/!$&'()*+,;=:@"

# The folder an embedded picture's link names but nobody writes. See the
# LinkResourceURI comment in _emit_image for why the attribute is kept.
_NOTIONAL_IMAGE_DIR = "images"

NO_PARAGRAPH_STYLE = "ParagraphStyle/$ID/[No paragraph style]"
NO_CHARACTER_STYLE = "CharacterStyle/$ID/[No character style]"

# What the ruler is marked in. A view preference and nothing more -- every
# length in the package stays in points either way -- but it is the first
# thing anybody continuing the layout meets, and Points is the wrong answer
# for a document laid out to whole millimetres.
_MEASUREMENT_UNITS = {"mm": "Millimeters", "in": "Inches"}

# The four sides of a table cell, as IDML prefixes them: TopEdgeStroke...,
# LeftEdgeStroke..., and so on.
_CELL_EDGES = ("Top", "Left", "Bottom", "Right")
# Publisher's three vertical alignments in IDML's spelling. It has a
# fourth, JustifyAlign, that Publisher's cell record cannot state.
_VERTICAL_JUSTIFICATION = {
    "top": "TopAlign", "center": "CenterAlign", "bottom": "BottomAlign",
}

# What IDML calls the languages Publisher states, with the quote marks and
# the id each one carries. Copied from the languages a real InDesign
# designmap declares rather than invented: the name is not a locale tag
# but a display string, and a reader matches on it exactly.
#
# Keyed by locale, most specific first: a country IDML names separately
# (English: USA) is looked up whole, and anything else falls back to the
# bare language, which is the same hyphenation dictionary -- fr-CA and
# fr-FR are both French to a reader that names only French. A locale with
# neither is left alone rather than guessed at: there is no plain
# "English" to fall back to, so en-AU keeps the reader's own default.
_LANGUAGES = {
    "da": ("Danish", 267, "’’", "””"),
    "de": ("German: Reformed", 275, "‚‘", "„“"),
    "en-GB": ("English: UK", 525, "‘’", "“”"),
    "en-US": ("English: USA", 269, "‘’", "“”"),
    "es": ("Spanish: Castilian", 294, "‘’", "“”"),
    "fi": ("Finnish", 273, "’’", "””"),
    "fr": ("French", 274, "‘’", "«»"),
    "it": ("Italian", 281, "‘’", "“”"),
    "nl": ("Dutch", 268, "‘’", "“”"),
    "pl": ("Polish", 287, "‚’", "„”"),
    "pt": ("Portuguese", 288, "‘’", "“”"),
    "sv": ("Swedish", 295, "’’", "””"),
}


def _language_name(locale: Optional[str]) -> Optional[str]:
    """IDML's name for a locale, or None where it names nothing for it."""
    if not locale:
        return None
    found = _LANGUAGES.get(locale) or _LANGUAGES.get(locale.split("-")[0])
    return found[0] if found else None

# What one "space" of Publisher line spacing measures, as a multiple of the
# type size, for a font this machine cannot read.
#
# 120% is InDesign's and Affinity's Auto leading, and it used to stand in
# for Publisher's space as well. It is not: Publisher is a GDI application
# and its space is GDI's `tmHeight`, the font's own usWinAscent plus
# usWinDescent, which for Calibri is 1.2207 em. Measured off Publisher's
# own PDFs -- 1336 and 1337 set their columns at 0.9 spaces of 10.0008pt
# Calibri and Publisher puts those baselines 11.0pt apart, against the
# 10.8 this constant gives -- so the font is read where it can be
# (`fontmetrics.line_metrics`) and this is only the fallback.
SINGLE_LINE_SPACING = 1.2

# The point size IDML assumes for a run that does not state one.
DEFAULT_POINT_SIZE = 12.0


def _pkg(tag: str) -> str:
    return f"{{{IDPKG}}}{tag}"


class MalformedPartError(Exception):
    """Raised when a generated part is not well-formed XML."""


def _emit_margins(
    page: ET.Element, margins: "model.PageMargins", page_width: float
) -> None:
    """Publisher's guides as a MarginPreference on one Page element.

    IDML measures a column by where it starts and ends inside the text
    area, so Publisher's guides -- which are single lines, not gutters --
    come out as columns that meet: gutter zero, and each column ending
    where the next begins. That is what the .pub actually draws.
    """
    text_width = page_width - margins.left - margins.right
    edges = [0.0, *margins.columns, text_width]
    # A guide that does not fall inside the text area would describe a
    # column of negative width, which no reader can draw. Then the margins
    # are still worth having and the columns are not.
    if sorted(edges) != edges:
        edges = [0.0, text_width]
    positions: List[str] = []
    for start, end in zip(edges, edges[1:]):
        positions += [fmt(start), fmt(end)]
    ET.SubElement(
        page,
        "MarginPreference",
        {
            "ColumnCount": str(len(edges) - 1),
            "ColumnGutter": "0",
            "ColumnDirection": "Horizontal",
            "ColumnsPositions": " ".join(positions),
            "Top": fmt(margins.top),
            "Bottom": fmt(margins.bottom),
            "Left": fmt(margins.left),
            "Right": fmt(margins.right),
        },
    )


def _serialise(element: ET.Element, processing_instruction: bool = False) -> bytes:
    body = ET.tostring(element, encoding="unicode")
    # ElementTree escapes markup but passes control characters through
    # untouched, and text extracted from a .pub is full of them. A part that
    # is not well-formed produces a package that opens nowhere, so verify
    # here rather than let the run be reported as a success. This is the
    # last point at which the failure can still be attributed to a file.
    try:
        ET.fromstring(body)
    except ET.ParseError as exc:
        raise MalformedPartError(f"generated XML is not well-formed: {exc}") from exc
    head = _XML_DECL + (_AID_PI if processing_instruction else "")
    return (head + body).encode("utf-8")


def _matrix(rotation_deg: float, tx: float, ty: float) -> str:
    """Build an IDML ItemTransform for a rotation about the item origin."""
    if not rotation_deg:
        return f"1 0 0 1 {fmt(tx)} {fmt(ty)}"
    radians = math.radians(rotation_deg)
    cos, sin = math.cos(radians), math.sin(radians)
    return f"{fmt(cos)} {fmt(sin)} {fmt(-sin)} {fmt(cos)} {fmt(tx)} {fmt(ty)}"


def _resolution(data: bytes, placed_w: float, placed_h: float) -> dict:
    """ActualPpi and EffectivePpi for a picture placed at a given size.

    Actual is what the file claims about itself; effective is what that
    works out to once it is scaled onto the page, and it is the number
    that decides whether the artwork holds up in print. Both were
    hard-coded to 72, which made every picture in the document look
    identically fine in the links panel — including one placed at 42 ppi.

    Effective resolution does not depend on the declared value: it is
    simply pixels over placed inches. So a file that stores no density
    still gets a truthful effective figure, and only the actual figure
    falls back to the conventional 72.
    """
    info = imagemeta.inspect(data)
    if not info.known or placed_w <= 0 or placed_h <= 0:
        return {"ActualPpi": "72 72", "EffectivePpi": "72 72"}

    actual_x = info.ppi_x or 72.0
    actual_y = info.ppi_y or 72.0
    effective_x = info.width_px / (placed_w / PT_PER_INCH)
    effective_y = info.height_px / (placed_h / PT_PER_INCH)
    return {
        "ActualPpi": f"{round(actual_x)} {round(actual_y)}",
        "EffectivePpi": f"{round(effective_x)} {round(effective_y)}",
    }


def _is_quarter_turn(rotation_deg: float) -> bool:
    """True when a rotation exchanges the horizontal and vertical axes."""
    return abs((abs(rotation_deg) % 180.0) - 90.0) < 45.0


def _content_bounds(rotation_deg: float, width: float, height: float):
    """The size to place a picture at, before its own rotation is applied.

    A quarter turn exchanges the footprint's axes, so a WxH frame is
    covered by a picture placed at HxW. Placing it at WxH regardless is
    what left rotated photographs both squashed and short of their frame:
    a 4000x3000 landscape photo was placed into portrait bounds and only
    then turned.
    """
    if _is_quarter_turn(rotation_deg):
        return height, width
    return width, height


def _content_matrix(rotation_deg: float, width: float, height: float) -> str:
    """Place a picture's content centred on the frame that holds it.

    IDML applies an ItemTransform as x' = a*x + c*y + tx, so the rotation
    runs before the translation. Offsetting by half the *unrotated* size
    therefore only centres content that is not rotated at all: at 90
    degrees it puts the picture wholly outside its frame, where it is
    clipped and the page simply looks as though the image is missing.

    Negating the rotated centre puts it back on the frame's own centre,
    and reduces to the old offset when the rotation is zero.
    """
    half_w, half_h = width / 2.0, height / 2.0
    if not rotation_deg:
        return f"1 0 0 1 {fmt(-half_w)} {fmt(-half_h)}"
    radians = math.radians(rotation_deg)
    cos, sin = math.cos(radians), math.sin(radians)
    tx = -(cos * half_w - sin * half_h)
    ty = -(sin * half_w + cos * half_h)
    return f"{fmt(cos)} {fmt(sin)} {fmt(-sin)} {fmt(cos)} {fmt(tx)} {fmt(ty)}"


def _ramp_angle(
    angle_deg: float,
    width: float = 0.0,
    height: float = 0.0,
    turn: float = 0.0,
    flipped_h: bool = False,
    flipped_v: bool = False,
) -> float:
    """One ramp's angle, turned from Publisher's convention into IDML's.

    The two sit a quarter turn apart. Publisher's unturned ramp runs up the
    shape -- Office calls that a *horizontal* shade, because the bands of
    colour lie horizontally -- and libmspub reports it as an angle of
    nothing; IDML's nothing runs left to right. Copying the number across
    turned every ramp in the corpus a quarter turn, which is subtle enough
    to read as a colour mistake rather than a rotation: the panel Publisher
    shades cream at the top down to white came out cream at the left.

    A quarter turn is the whole of it only while the ramp lies along an
    edge. **A diagonal one lies along the shape's own corner-to-corner
    line**, so its angle on the page follows the box's proportions and is
    45 degrees only on a square. Publisher's own PDF export settles it:
    across the 82.3 by 62.0 panel on page 11 of `1336 kerkbode.pub` it
    draws the ramp at 53.0 degrees, which is that box's diagonal exactly,
    where a plain quarter turn puts it 82 degrees away.

    So the stated angle is read in the shape's *square* and then stretched
    to the box, which is what laying the bands along the diagonal amounts
    to: a direction stretched by (w, h) has its bands' normal stretched by
    the inverse, leaving `atan2(w sin, h cos)`. On an upright ramp the
    stretch cancels and the answer is the plain quarter turn to the last
    decimal, which is what keeps the corpus's other 22 ramps where they
    were.

    Two things here are measured and one is not. Measured: the diagonal
    follows the box, and which way round the ramp then runs -- the WordArt
    banner on that page states its ramp as a sampled table, dark navy at
    the low end, and the direction below reproduces it. Not measured: a
    ramp's *sense* comes from the angle's magnitude, because the corpus's
    two diagonals lie on one line. The file states them as 135 and -45,
    which is one line half a turn apart, and `_FILL_ANGLE_FIXUPS` splits
    that line in two on the way through libmspub -- so the mirrored
    diagonal, the one no file here states, is still a guess.

    `research/gradient_angle.py` is the measurement, and rerunning it
    against a .pub with the other diagonal in it is what would settle the
    rest.
    """
    # The quarter turn on its own: the right line for an upright ramp, and
    # the right way along it for either.
    plain = model._fold_angle(angle_deg + 90.0)
    if not width or not height:
        # A text run has no box of its own to lay a diagonal across.
        return _placed(plain, turn, flipped_h, flipped_v)
    square = math.radians(abs(angle_deg) - 90.0)
    turned = math.degrees(
        math.atan2(width * math.sin(square), height * math.cos(square))
    )
    # The stretch settles the line, not which end of it the ramp starts
    # from; that stays as the quarter turn states it.
    if abs(model._fold_angle(turned - plain)) > 90.0:
        turned += 180.0
    return _placed(turned, turn, flipped_h, flipped_v)


def _placed(angle: float, turn: float, flipped_h: bool, flipped_v: bool) -> float:
    """Where a ramp laid across the shape ends up once the shape is placed.

    The stretch above is about the ramp *inside* the shape, so the shape's
    own placement comes after it: turn first, then mirror, because that is
    the order Publisher composes them in. Measured against Publisher's own
    PDF export: the masthead ribbon states a turn and no flip, the heading
    bands state both, and only this order and this sign put all of them on
    the axis Publisher draws -- the ribbon to two thousandths of a degree,
    where folding the turn into the angle before the stretch left it 12
    degrees out.

    The turn's sign is the caller's: Publisher states a turn the other way
    about from the way it draws it, which `convert` negates on the way in.
    """
    angle = angle + turn
    # A mirrored shape mirrors its shade: reflected about the horizontal
    # axis for a vertical flip, about the vertical axis for a horizontal
    # one. Only the vertical flip is measured -- no shape in the corpus
    # states a horizontal one.
    if flipped_v:
        angle = -angle
    if flipped_h:
        angle = 180.0 - angle
    return model._fold_angle(angle)


def _ramp_geometry(
    angle_deg: float, width: float, height: float, span: Optional[float] = None
):
    """Where a gradient starts and how far it runs across a box.

    IDML measures both in the box's own coordinates, which are centred on
    its middle with y increasing downwards, and states the angle
    anticlockwise from left-to-right. The ramp has to cover the box's
    whole extent in that direction, so the length is the box projected
    onto it and the start is half of that back from the centre.

    `span` overrides that length where the file states the box the ramp was
    measured across, which is not always the one the item carries. Two
    things pull them apart, and Publisher's own PDF export settles both.
    An outline: the anchor measures the shape with it, libmspub reports the
    path inside it, and on the page-8 panel of the newsletter corpus that
    is 82.9pt against 66.4 -- a ramp squeezed into four fifths of its room,
    which reaches neither end colour. And a turn: a shape turned 12 degrees
    has a page-aligned box half again as tall as itself, and measuring the
    ramp across *that* ran the masthead ribbon 231pt where Publisher runs
    it 92.1.
    """
    radians = math.radians(angle_deg)
    # A quarter turn leaves a cosine of 1e-17 rather than nothing, which
    # would be written as the "-0" no reader should have to interpret.
    cos, sin = (
        value if abs(value) > 1e-9 else 0.0
        for value in (math.cos(radians), math.sin(radians))
    )
    length = span if span else abs(width * cos) + abs(height * sin)
    # Adding zero turns the -0.0 that negating a zero cosine leaves back
    # into 0.0, which is the same number and the only one of the two that
    # formats as "0".
    start_x = -cos * length / 2.0 + 0.0
    start_y = sin * length / 2.0 + 0.0
    return f"{fmt(start_x)} {fmt(start_y)}", length


def _ramp_placement(
    gradient: model.Gradient,
    width: Optional[float] = None,
    height: Optional[float] = None,
):
    """One ramp's angle, start point and distance, all off the same box.

    The whole composition in one place, because the parts have to agree on
    which box they are measured across and they used not to: the stretch
    was taken across the box the *item* carries while the distance came
    from the box the *file* states, which on the page-8 panel of the
    newsletter corpus are 16pt apart and on the masthead ribbon 139pt. A
    ramp measured that way runs along a line it was never laid on.

    `gradient.box` is that file box, and the item's own is the fallback --
    all there is for a ramp libmspub reported and the file did not. A run
    of text has neither, and then there is a direction and nothing else to
    say: `None` for both the start and the distance.

    The distance is measured across the ramp's angle *inside* the shape,
    before the shape's own turn goes on, because the box is in that frame
    too. The start point is on the placed angle, which is where the reader
    draws it. A mirror makes no difference to the distance either way,
    since a reflection leaves both projections' magnitudes alone.
    """
    def usable(box):
        # A box with a zero side measures nothing, and a ramp with no
        # distance to run is not a ramp: it paints two flat halves with a
        # hard edge between them. So it counts as no box rather than as a
        # distance of zero, and the next one along gets its turn.
        return box if box and all(box) else None

    box = usable(gradient.box) or usable((width, height))
    inside = _ramp_angle(gradient.angle, *(box or ()))
    angle = _placed(
        inside, gradient.turn, gradient.flipped_h, gradient.flipped_v
    )
    if box is None:
        return angle, None, None
    radians = math.radians(inside)
    span = (
        abs(box[0] * math.cos(radians)) + abs(box[1] * math.sin(radians))
    )
    start, length = _ramp_geometry(angle, *box, span=span)
    return angle, start, length


def _spanning_stops(stops):
    """A ramp with explicit stops at both ends of the range.

    Publisher's ramps often occupy only part of their range -- 32 to 49,
    or 3 to 69 -- and what happens outside that is the reader's choice:
    hold the end colours, or stretch the ramp to fit. Those look nothing
    alike, and holding is what Publisher does, so the ends are stated
    rather than left to be guessed.

    The reported offsets are not touched. Padding only adds a stop at 0
    and at 100 in the colour already at that end, so the repeated offsets
    Publisher uses to make a hard edge survive intact.
    """
    if not stops:
        return stops
    ordered = sorted(stops, key=lambda stop: stop.location)
    padded = list(ordered)
    first, last = ordered[0], ordered[-1]
    if first.location > 0:
        padded.insert(0, replace(first, location=0.0))
    if last.location < 100:
        padded.append(replace(last, location=100.0))
    return padded


def _rect_path(parent: ET.Element, width: float, height: float) -> None:
    """Attach a closed rectangular PathGeometry centred on the origin."""
    half_w, half_h = width / 2.0, height / 2.0
    corners = [
        (-half_w, -half_h),
        (-half_w, half_h),
        (half_w, half_h),
        (half_w, -half_h),
    ]
    _path_from_anchors(parent, [(x, y, None, None) for x, y in corners], closed=True)


def _path_from_anchors(parent: ET.Element, anchors, closed: bool) -> None:
    """Emit a PathGeometry holding one outline."""
    _path_from_subpaths(parent, [(anchors, closed)])


def _path_from_subpaths(parent: ET.Element, subpaths) -> None:
    """Emit a PathGeometry holding one outline per subpath.

    A compound path is several GeometryPathType entries inside a single
    PathGeometry, which is how IDML represents disjoint outlines.
    """
    geometry = ET.SubElement(parent, "PathGeometry")
    for anchors, closed in subpaths:
        _geometry_path(geometry, anchors, closed)


def _geometry_path(geometry: ET.Element, anchors, closed: bool) -> None:
    path = ET.SubElement(
        geometry, "GeometryPathType", {"PathOpen": "false" if closed else "true"}
    )
    array = ET.SubElement(path, "PathPointArray")
    for x, y, left, right in anchors:
        anchor = f"{fmt(x)} {fmt(y)}"
        left_dir = f"{fmt(left[0])} {fmt(left[1])}" if left else anchor
        right_dir = f"{fmt(right[0])} {fmt(right[1])}" if right else anchor
        ET.SubElement(
            array,
            "PathPointType",
            {"Anchor": anchor, "LeftDirection": left_dir, "RightDirection": right_dir},
        )


class _Ids:
    """Sequential IDML Self identifiers."""

    def __init__(self) -> None:
        self._n = 0

    def next(self, prefix: str = "u") -> str:
        self._n += 1
        return f"{prefix}{self._n}"


class _ChainLink(NamedTuple):
    """One frame's place in a threaded story."""

    story_id: str
    self_id: str
    previous: str  # a frame Self id, or "n" at the head
    following: str  # a frame Self id, or "n" at the tail
    head: bool  # the link that carries the text


class IdmlWriter:
    """Renders a `model.Document` into an IDML package on disk.

    Pictures are carried inside the package, base64 in each image's
    `<Contents>`. That is what `image_dir_name=None` means, and it is the
    default because the alternative -- a `<name>_images/` folder beside
    the .idml, linked by relative URI -- was the one way a conversion
    that reported nothing wrong still lost its artwork: move the .idml
    without the folder and every picture is gone.

    `research/probe_embedded_image.py` asked Affinity whether it reads
    embedded bytes at all, with the link deliberately pointed at a file
    that was not there so a drawn picture could only have come from the
    contents. It draws them, from base64 (hex it ignores).

    Naming a directory instead restores the linked sidecar exactly as it
    was. It is kept for the experiments in `research/` that record it,
    and as the way back should Affinity ever object to the unresolvable
    URI an embedded image carries.
    """

    def __init__(
        self,
        document: model.Document,
        image_dir_name: Optional[str] = None,
        wrap_images: bool = True,
        facing_pages: bool = False,
        measurement_unit: Literal["mm", "in"] = "mm",
        bleed: float = 0.0,
    ):
        self.doc = document
        self.ids = _Ids()
        self.image_dir_name = image_dir_name
        self.wrap_images = wrap_images
        self.facing_pages = facing_pages
        self.measurement_unit = measurement_unit
        #: Document bleed on all four edges, in points. Nothing in a .pub
        #: states one -- Publisher has no document bleed at all, only a
        #: fixed 0.125in print option -- so this is the caller's, and zero
        #: means the document is set up without bleed rather than unknown.
        self.bleed = bleed
        self.color_ids: Dict[model.Color, str] = {}
        # Equal gradients share one resource, which is why model.Gradient is
        # frozen: Publisher repeats the same ramp across a document.
        self.gradient_ids: Dict[model.Gradient, str] = {}
        self.fonts: List[str] = []
        # (relative path, bytes) pairs the caller must write next to the
        # IDML. Empty when images are embedded, which is why write()'s
        # sidecar block needs no mode of its own.
        self.image_files: List[tuple] = []
        # Numbers the pictures. Not len(image_files): embedding appends
        # nothing there, and the notional filenames must still differ.
        self._image_count = 0
        self._parts: Dict[str, bytes] = {}
        # id(frame) -> _ChainLink, for the frames of a threaded story. Keyed
        # by identity, not equality: the continuation links of a chain hold no
        # text and often share their geometry, so as dataclasses they compare
        # equal to one another and a lookup by value would conflate them.
        self._chain_links: Dict[int, _ChainLink] = {}

    # -- public API -------------------------------------------------------

    def write(self, idml_path: Path) -> None:
        """Build the package, then put it where it belongs -- in that order.

        The archive is written beside its destination and moved onto it
        only once it is whole, because the batch driver decides a file is
        already converted by asking whether the .idml exists. A package
        left half-written by a full disk or a killed process would not
        merely be broken: it would be skipped for ever after, and its
        failed row overwritten by the next run's report.

        `os.replace` is atomic within a directory on both Windows and
        POSIX, which is why the temporary sits next to the destination
        rather than in the system temp directory.
        """
        idml_path = Path(idml_path)
        self._build()

        idml_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            dir=idml_path.parent, prefix=f".{idml_path.name}.", suffix=".part"
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                # The mimetype entry must be first and uncompressed.
                archive.writestr(
                    zipfile.ZipInfo("mimetype"),
                    "application/vnd.adobe.indesign-idml-package",
                    compress_type=zipfile.ZIP_STORED,
                )
                for name, payload in self._parts.items():
                    archive.writestr(name, payload)

            # Before the move, not after: a package whose pictures are
            # missing is as unfinished as one whose parts are, and the
            # move is what says the whole thing arrived.
            if self.image_files:
                image_root = idml_path.parent / self.image_dir_name
                image_root.mkdir(parents=True, exist_ok=True)
                for relative_name, payload in self.image_files:
                    (idml_path.parent / relative_name).write_bytes(payload)

            os.replace(temporary, idml_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    # -- assembly ---------------------------------------------------------

    def _build(self) -> None:
        self._collect_resources()
        self._plan_text_chains()

        spread_parts: List[str] = []
        story_parts: List[str] = []
        master_parts: List[str] = []

        # Masters are written first so that a page can name one it applies.
        for master in self.doc.masters:
            part_name = f"MasterSpreads/MasterSpread_{self.master_id(master.name)}.xml"
            master_parts.append(part_name)
            self._parts[part_name] = self._master_spread_part(master, story_parts)

        for index, group in enumerate(self._spread_groups(), start=1):
            spread_name = f"Spreads/Spread_spread{index}.xml"
            spread_parts.append(spread_name)
            self._parts[spread_name] = self._spread_part(group, index, story_parts)

        self._parts["META-INF/container.xml"] = self._container_part()
        self._parts["META-INF/metadata.xml"] = self._metadata_part()
        self._parts["Resources/Fonts.xml"] = self._fonts_part()
        self._parts["Resources/Graphic.xml"] = self._graphic_part()
        self._parts["Resources/Styles.xml"] = self._styles_part()
        self._parts["Resources/Preferences.xml"] = self._preferences_part()
        self._parts["XML/BackingStory.xml"] = self._backing_story_part()
        self._parts["XML/Tags.xml"] = self._tags_part()
        self._parts["designmap.xml"] = self._designmap_part(
            spread_parts, story_parts, master_parts
        )

    @staticmethod
    def master_id(name: str) -> str:
        return f"master{name}"

    def _plan_text_chains(self) -> None:
        """Assign ids for threaded stories before any spread is written.

        A chain runs across pages, so a frame has to name a successor that
        lives in a spread part not yet built. Allocating the whole chain's
        ids up front is what makes those forward references possible.
        """
        for chain_id, frames in self.doc.text_chains.items():
            story_id = self.ids.next("story")
            frame_ids = [self.ids.next() for _ in frames]
            for position, frame in enumerate(frames):
                self._chain_links[id(frame)] = _ChainLink(
                    story_id=story_id,
                    self_id=frame_ids[position],
                    previous=frame_ids[position - 1] if position else "n",
                    following=(
                        frame_ids[position + 1]
                        if position + 1 < len(frame_ids)
                        else "n"
                    ),
                    head=position == 0,
                )

    def _collect_resources(self) -> None:
        self.fonts = self.doc.fonts
        for color in self.doc.colors:
            self.color_ids[color] = "Color/C_%02X%02X%02X" % color
        def claim(gradient: Optional[model.Gradient]) -> None:
            if gradient is not None and gradient not in self.gradient_ids:
                self.gradient_ids[gradient] = f"Gradient/G_{len(self.gradient_ids) + 1}"

        for item in self.doc.all_items():
            claim(item.style.gradient)
        # A recovered WordArt headline carries its ramp on the text rather
        # than on a shape, so the runs have to be reached too or the
        # reference resolves to nothing -- every run, cells included, for
        # the same reason their colours are collected.
        for span in self.doc.spans():
            claim(span.gradient)

    def _languages_used(self) -> List[str]:
        """IDML's name for every language the text is written in.

        In first-appearance order, and only those a run actually names:
        a document says nothing about languages it does not use, which is
        also how InDesign writes it -- a Czech document's designmap
        declares Czech and nothing else.
        """
        found: List[str] = []
        for span in self.doc.spans():
            name = _language_name(span.language)
            if name and name not in found:
                found.append(name)
        return found

    def _fill_ref(self, style: model.GraphicStyle) -> str:
        """A shape's fill: its gradient where it has one, else its colour."""
        gradient = style.gradient
        if gradient is not None:
            reference = self.gradient_ids.get(gradient)
            if reference:
                return reference
        return self._color_ref(style.fill)

    def _color_ref(self, color: Optional[model.Color], fallback: str = "Swatch/None") -> str:
        if color is None:
            return fallback
        return self.color_ids.get(color, fallback)

    # -- package parts ----------------------------------------------------

    def _container_part(self) -> bytes:
        namespace = "urn:oasis:names:tc:opendocument:xmlns:container"
        container = ET.Element("container", {"version": "1.0", "xmlns": namespace})
        rootfiles = ET.SubElement(container, "rootfiles")
        ET.SubElement(
            rootfiles, "rootfile", {"full-path": "designmap.xml", "media-type": "text/xml"}
        )
        return _serialise(container)

    def _metadata_part(self) -> bytes:
        root = ET.Element(
            "{adobe:ns:meta/}xmpmeta", {"{adobe:ns:meta/}xmptk": "pubidml"}
        )
        return _serialise(root)

    def _master_sides(self, master: model.Master) -> List[float]:
        """Where this master's page or pages sit in its own spread.

        A master's items are carried onto a page by `MasterPageTransform`,
        which InDesign writes as the identity -- and only the identity is
        right, because a master page in a real IDML sits at exactly the
        offset of the pages applying it. So the master has to be laid out
        the way those pages are: centred on the spread origin where the
        document is single-page, and on the side of the spine the pages
        using it are on where it is facing.

        A master applied to both sides gets a page on each, which is what
        InDesign's own two-page master spread is. Its content is written
        twice, once per side, since a running head on a facing master
        really is two frames.
        """
        if not self.facing_pages:
            return [-master.width / 2.0]
        sides = {
            self._page_offset_x(index + 1, master.width)
            for index, page in enumerate(self.doc.pages)
            if page.master == master.name
        }
        # A master no page applies has no side to take: it is written
        # centred so the part is still well-formed and openable.
        return sorted(sides) or [-master.width / 2.0]

    def _master_spread_part(self, master: model.Master, story_parts: List[str]) -> bytes:
        """A MasterSpread holding content Publisher kept once, not per page.

        Same shape as a Spread, and its items keep their page coordinates:
        a master serves only pages of its own size, so the offset of the
        page it is laid out on (`_master_sides`) is all they need.
        """
        root = ET.Element(
            "idPkg:MasterSpread", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION}
        )
        identifier = self.master_id(master.name)
        sides = self._master_sides(master)
        spread = ET.SubElement(
            root,
            "MasterSpread",
            {
                "Self": identifier,
                "Name": f"{master.name}-Master",
                "NamePrefix": master.name,
                "BaseName": "Master",
                "ShowMasterItems": "true",
                "PageCount": str(len(sides)),
                "OverriddenPageItemProps": "",
                "ItemTransform": "1 0 0 1 0 0",
            },
        )
        half_h = master.height / 2.0
        for position, offset_x in enumerate(sides, start=1):
            page_element = ET.SubElement(
                spread,
                "Page",
                {
                    "Self": f"{identifier}_page{position}",
                    "Name": master.name,
                    "AppliedMaster": "n",
                    "OverrideList": "",
                    # Identity, and identity is only true because the page
                    # above is on the same side of the spine as every page
                    # applying it. Left out, a reader has no statement of
                    # where master content lands at all.
                    "MasterPageTransform": "1 0 0 1 0 0",
                    "GeometricBounds": f"0 0 {fmt(master.height)} {fmt(master.width)}",
                    "ItemTransform": f"1 0 0 1 {fmt(offset_x)} {fmt(-half_h)}",
                    "AppliedTrapPreset": "TrapPreset/$ID/kDefaultTrapStyleName",
                    "GridStartingPoint": "TopOutside",
                    "UseMasterGrid": "true",
                },
            )
            if master.margins is not None:
                _emit_margins(page_element, master.margins, master.width)
        page = model.Page(width=master.width, height=master.height)
        for offset_x in sides:
            for item in _flatten(master.items):
                self._emit_item(spread, item, page, story_parts, offset_x)
        return _serialise(root)

    def _designmap_part(
        self, spreads: List[str], stories: List[str], masters: List[str] = ()
    ) -> bytes:
        document = ET.Element(
            "Document",
            {
                "xmlns:idPkg": IDPKG,
                "DOMVersion": DOM_VERSION,
                "Self": "pubidml_doc",
                "StoryList": " ".join(
                    Path(name).stem.replace("Story_", "") for name in stories
                ),
                "Name": self.doc.title or "Converted Publisher document",
            },
        )
        # The schema puts the languages ahead of the package references,
        # and a reader resolving AppliedLanguage looks for them here.
        for name in self._languages_used():
            display, identifier, single, double = next(
                entry for entry in _LANGUAGES.values() if entry[0] == name
            )
            primary, _, sub = display.partition(": ")
            ET.SubElement(
                document,
                "Language",
                {
                    # A colon is escaped in the id and left alone in the
                    # name, which is how InDesign writes it.
                    "Self": f"Language/$ID/{display.replace(':', '%3a')}",
                    "Name": f"$ID/{display}",
                    "SingleQuotes": single,
                    "DoubleQuotes": double,
                    "PrimaryLanguageName": f"$ID/{primary}",
                    "SublanguageName": f"$ID/{sub}",
                    "Id": str(identifier),
                },
            )
        for src in (
            "Resources/Graphic.xml",
            "Resources/Fonts.xml",
            "Resources/Styles.xml",
            "Resources/Preferences.xml",
        ):
            tag = {"Graphic.xml": "Graphic", "Fonts.xml": "Fonts",
                   "Styles.xml": "Styles", "Preferences.xml": "Preferences"}[Path(src).name]
            ET.SubElement(document, f"idPkg:{tag}", {"src": src})
        # Masters must be declared before the spreads that apply them.
        for src in masters:
            ET.SubElement(document, "idPkg:MasterSpread", {"src": src})
        for src in spreads:
            ET.SubElement(document, "idPkg:Spread", {"src": src})
        for src in stories:
            ET.SubElement(document, "idPkg:Story", {"src": src})
        ET.SubElement(document, "idPkg:BackingStory", {"src": "XML/BackingStory.xml"})
        ET.SubElement(document, "idPkg:Tags", {"src": "XML/Tags.xml"})
        return _serialise(document, processing_instruction=True)

    def _fonts_part(self) -> bytes:
        root = ET.Element("idPkg:Fonts", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION})
        for index, family in enumerate(self.fonts, start=1):
            group = ET.SubElement(
                root, "FontFamily", {"Self": f"font{index}", "Name": family}
            )
            # Squeezing the spaces out of the family is a guess, and it is
            # wrong for most faces that have a style in the name at all:
            # Calibri's regular is 'Calibri' but its light is 'Calibri-Light'
            # and Times New Roman's is 'TimesNewRomanPSMT'. Where the font
            # is installed, the file states its own name; where it is not,
            # the guess is still the best that can be said.
            naming = fontmetrics.naming(family, False, False)
            ET.SubElement(
                group,
                "Font",
                {
                    "Self": f"font{index}_regular",
                    "FontFamily": family,
                    "Name": f"{family} Regular",
                    "PostScriptName": (
                        naming.postscript if naming else family.replace(" ", "")
                    ),
                    "Status": "Substituted",
                    "FontStyleName": "Regular",
                    "FontType": "OpenTypeTT",
                },
            )
        return _serialise(root)

    def _graphic_part(self) -> bytes:
        root = ET.Element("idPkg:Graphic", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION})

        ET.SubElement(
            root,
            "Color",
            {
                "Self": "Color/Black",
                "Model": "Process",
                "Space": "CMYK",
                "ColorValue": "0 0 0 100",
                "ColorOverride": "Normal",
                "Name": "Black",
                "ColorEditable": "false",
                "ColorRemovable": "false",
                "Visible": "true",
                "SwatchCreatorID": "7937",
            },
        )
        ET.SubElement(
            root,
            "Color",
            {
                "Self": "Color/Paper",
                "Model": "Process",
                "Space": "CMYK",
                "ColorValue": "0 0 0 0",
                "ColorOverride": "Specialpaper",
                "Name": "Paper",
                "ColorEditable": "false",
                "ColorRemovable": "false",
                "Visible": "true",
                "SwatchCreatorID": "7937",
            },
        )
        for gradient, identifier in self.gradient_ids.items():
            element = ET.SubElement(
                root,
                "Gradient",
                {
                    "Self": identifier,
                    "Type": "Radial" if gradient.radial else "Linear",
                },
            )
            for index, stop in enumerate(_spanning_stops(gradient.stops)):
                ET.SubElement(
                    element,
                    "GradientStop",
                    {
                        "Self": f"{identifier}GradientStop{index}",
                        "StopColor": self._color_ref(stop.color, "Color/Black"),
                        "Location": fmt(stop.location),
                        "Midpoint": "50",
                    },
                )

        ET.SubElement(
            root,
            "Swatch",
            {
                "Self": "Swatch/None",
                "Name": "None",
                "ColorEditable": "false",
                "ColorRemovable": "false",
                "Visible": "true",
                "SwatchCreatorID": "7937",
            },
        )

        for color, identifier in self.color_ids.items():
            ET.SubElement(
                root,
                "Color",
                {
                    "Self": identifier,
                    "Model": "Process",
                    "Space": "RGB",
                    "ColorValue": f"{color[0]} {color[1]} {color[2]}",
                    "ColorOverride": "Normal",
                    "Name": "R=%d G=%d B=%d" % color,
                    "ColorEditable": "true",
                    "ColorRemovable": "true",
                    "Visible": "true",
                    "SwatchCreatorID": "7937",
                },
            )

        for name in ("$ID/Solid", "$ID/ThinThin", "$ID/Dashed"):
            ET.SubElement(root, "StrokeStyle", {"Self": f"StrokeStyle/{name}", "Name": name})

        return _serialise(root)

    def _styles_part(self) -> bytes:
        root = ET.Element("idPkg:Styles", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION})

        character_group = ET.SubElement(root, "RootCharacterStyleGroup", {"Self": "cs_root"})
        ET.SubElement(
            character_group,
            "CharacterStyle",
            {
                "Self": NO_CHARACTER_STYLE,
                "Name": "$ID/[No character style]",
                "Imported": "false",
            },
        )

        paragraph_group = ET.SubElement(root, "RootParagraphStyleGroup", {"Self": "ps_root"})
        ET.SubElement(
            paragraph_group,
            "ParagraphStyle",
            {
                "Self": NO_PARAGRAPH_STYLE,
                "Name": "$ID/[No paragraph style]",
                "Imported": "false",
            },
        )

        object_group = ET.SubElement(root, "RootObjectStyleGroup", {"Self": "os_root"})
        for name in ("$ID/[None]", "$ID/[Normal Text Frame]", "$ID/[Normal Graphics Frame]"):
            ET.SubElement(
                object_group,
                "ObjectStyle",
                {
                    "Self": f"ObjectStyle/{name}",
                    "Name": name,
                    "AppliedParagraphStyle": NO_PARAGRAPH_STYLE,
                },
            )

        ET.SubElement(root, "RootCellStyleGroup", {"Self": "cell_root"})
        ET.SubElement(root, "RootTableStyleGroup", {"Self": "table_root"})
        return _serialise(root)

    def _preferences_part(self) -> bytes:
        root = ET.Element(
            "idPkg:Preferences", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION}
        )
        first = self.doc.pages[0] if self.doc.pages else model.Page()
        # Singular, and not a typo to correct: Affinity's importer matches
        # this element as `DocumentPreference`, the way it matches
        # MarginPreference and ViewPreference, and its grammar reads
        # PageHeight, PageWidth, FacingPages and the five bleed attributes
        # off it. Named plural it is not matched at all, and the document
        # opens with no bleed, no stated page size and no facing pages --
        # which is how it opened until the reader's own grammar was read
        # out of liblibidmlimport.dylib. PagesPerDocument and
        # PageOrientation are not in that grammar; they stay because
        # InDesign's own packages carry them.
        ET.SubElement(
            root,
            "DocumentPreference",
            {
                "PageHeight": fmt(first.height),
                "PageWidth": fmt(first.width),
                "PagesPerDocument": str(max(1, len(self.doc.pages))),
                "FacingPages": "true" if self.facing_pages else "false",
                "PageOrientation": "Portrait" if first.height >= first.width else "Landscape",
                # One figure on all four edges, which is what a bleed is
                # asked for as. The uniform flag is what makes the reader
                # show it as one figure rather than four; without it the
                # four offsets are still honoured but arrive as a custom
                # bleed nobody typed.
                "DocumentBleedUniformSize": "true",
                "DocumentBleedTopOffset": fmt(self.bleed),
                "DocumentBleedBottomOffset": fmt(self.bleed),
                "DocumentBleedInsideOrLeftOffset": fmt(self.bleed),
                "DocumentBleedOutsideOrRightOffset": fmt(self.bleed),
            },
        )
        # The fallback agrees with `convert._measurement_unit`'s own, which
        # is the only thing that produces this value and produces nothing
        # else -- so it is unreachable rather than a second opinion. It
        # used to say Millimeters, which made an unrecognised unit read as
        # metric here and imperial there.
        units_name = _MEASUREMENT_UNITS.get(self.measurement_unit, "Inches")
        ET.SubElement(
            root,
            "ViewPreference",
            {
                "HorizontalMeasurementUnits": units_name,
                "VerticalMeasurementUnits": units_name,
            },
        )
        return _serialise(root)

    def _backing_story_part(self) -> bytes:
        root = ET.Element(
            "idPkg:BackingStory", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION}
        )
        story = ET.SubElement(root, "XmlStory", {"Self": "backing_story"})
        ET.SubElement(
            story,
            "StoryPreference",
            {"StoryOrientation": "Horizontal", "StoryDirection": "LeftToRightDirection"},
        )
        return _serialise(root)

    def _tags_part(self) -> bytes:
        root = ET.Element("idPkg:Tags", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION})
        ET.SubElement(
            root, "XMLTag", {"Self": "XMLTag/Root", "Name": "Root"}
        )
        return _serialise(root)

    # -- spreads ----------------------------------------------------------

    def _spread_groups(self) -> List[List[int]]:
        """Page indices per spread.

        One page per spread unless the document is facing, in which case
        the cover stands alone as a recto with nothing opposite it and the
        rest pair up: 1 | 2 3 | 4 5. That is how a booklet reads, and it
        keeps odd page numbers on the right of the spine throughout.
        """
        count = len(self.doc.pages)
        if not self.facing_pages:
            return [[index] for index in range(count)]

        groups: List[List[int]] = []
        if count:
            groups.append([0])
        for index in range(1, count, 2):
            groups.append([i for i in (index, index + 1) if i < count])
        return groups

    def _page_offset_x(self, page_number: int, width: float) -> float:
        """Where a page's left edge sits in its spread's coordinates.

        A single-page spread is centred on the spread origin. Facing pages
        put the spine at the origin instead: odd numbers are rectos and run
        rightwards from it, even numbers are versos and hang to its left.
        """
        if not self.facing_pages:
            return -width / 2.0
        return 0.0 if page_number % 2 else -width

    def _spread_part(
        self, pages: List[int], index: int, story_parts: List[str]
    ) -> bytes:
        root = ET.Element("idPkg:Spread", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION})
        spread = ET.SubElement(
            root,
            "Spread",
            {
                "Self": f"spread{index}",
                "PageCount": str(len(pages)),
                "BindingLocation": "1" if len(pages) > 1 else "0",
                "ShowMasterItems": "true",
                "PageTransitionType": "None",
                "PageTransitionDirection": "NotApplicable",
                "PageTransitionDuration": "Medium",
                "AllowPageShuffle": "true",
                "ItemTransform": "1 0 0 1 0 0",
                "FlattenerOverride": "Default",
            },
        )
        ET.SubElement(spread, "FlattenerPreference", {"LineArtAndTextResolution": "300"})

        for position in pages:
            page = self.doc.pages[position]
            number = position + 1
            offset_x = self._page_offset_x(number, page.width)
            page_element = ET.SubElement(
                spread,
                "Page",
                {
                    "Self": f"page{number}",
                    "Name": str(number),
                    "AppliedMaster": self.master_id(page.master) if page.master else "n",
                    "OverrideList": "",
                    # The matrix that carries the master's items onto this
                    # page. Every Page in an InDesign-written package states
                    # it, and the master spread is laid out so that the
                    # identity is the true one (`_master_sides`).
                    "MasterPageTransform": "1 0 0 1 0 0",
                    "GeometricBounds": f"0 0 {fmt(page.height)} {fmt(page.width)}",
                    "ItemTransform": (
                        f"1 0 0 1 {fmt(offset_x)} {fmt(-page.height / 2.0)}"
                    ),
                    "AppliedTrapPreset": "TrapPreset/$ID/kDefaultTrapStyleName",
                    "GridStartingPoint": "TopOutside",
                    "UseMasterGrid": "true",
                },
            )
            if page.margins is not None:
                _emit_margins(page_element, page.margins, page.width)

        # Page items are children of the spread, not of the page, so each
        # one carries its page's offset itself.
        for position in pages:
            page = self.doc.pages[position]
            offset_x = self._page_offset_x(position + 1, page.width)
            # Groups are flattened: their children already carry absolute
            # page coordinates, and a flat spread avoids nested-transform
            # drift.
            for item in _flatten(page.items):
                self._emit_item(spread, item, page, story_parts, offset_x)

        return _serialise(root)

    def _emit_item(
        self,
        spread: ET.Element,
        item: model.Item,
        page: model.Page,
        story_parts: List[str],
        offset_x: Optional[float] = None,
    ) -> None:
        if isinstance(item, model.Table):
            self._emit_table(spread, item, page, story_parts, offset_x)
        elif isinstance(item, model.TextFrame):
            self._emit_text_frame(spread, item, page, story_parts, offset_x)
        elif isinstance(item, model.Image):
            self._emit_image(spread, item, page, offset_x)
        elif isinstance(item, model.Ellipse):
            self._emit_shape(spread, item, page, "Oval", offset_x)
        elif isinstance(item, (model.Polygon, model.Path)):
            self._emit_shape(spread, item, page, "Polygon", offset_x)
        elif isinstance(item, model.Rectangle):
            self._emit_shape(spread, item, page, "Rectangle", offset_x)

    def _frame_attributes(
        self,
        item: model.Item,
        page: model.Page,
        object_style: str,
        self_id: Optional[str] = None,
        offset_x: Optional[float] = None,
    ) -> dict:
        # None means the page is centred on its spread origin, which is the
        # single-page case and what every master spread is.
        if offset_x is None:
            offset_x = -page.width / 2.0
        centre_x = item.x + item.width / 2.0 + offset_x
        centre_y = item.y + item.height / 2.0 - page.height / 2.0
        attributes = {
            "Self": self_id or self.ids.next(),
            "ItemTransform": _matrix(item.rotation, centre_x, centre_y),
            "AppliedObjectStyle": f"ObjectStyle/{object_style}",
            "FillColor": self._fill_ref(item.style),
            "StrokeColor": self._color_ref(item.style.stroke),
            "StrokeWeight": fmt(item.style.stroke_width),
            "StrokeAlignment": "CenterAlignment",
            "Visible": "true",
        }
        gradient = item.style.gradient
        if gradient is not None and gradient in self.gradient_ids:
            # An angle says which way the ramp runs, not how far, and a
            # ramp with no distance to run is not a ramp: everything before
            # its start point takes the first stop and everything after it
            # the last. Left unstated the distance is nothing, so a shape
            # came out as two flat halves meeting in a hard edge down its
            # middle -- the fade over the whole shape is what the distance
            # is for.
            angle, start, length = _ramp_placement(
                gradient, item.width, item.height
            )
            attributes["GradientFillAngle"] = fmt(angle)
            attributes["GradientFillStart"] = start
            attributes["GradientFillLength"] = fmt(length)
        if item.style.stroke is not None:
            attributes["AppliedStrokeStyle"] = "StrokeStyle/$ID/Solid"
        return attributes

    def _emit_shape(
        self,
        spread: ET.Element,
        item: model.Item,
        page: model.Page,
        tag: str,
        offset_x: Optional[float] = None,
    ) -> None:
        attributes = self._frame_attributes(
            item, page, "$ID/[None]", offset_x=offset_x
        )
        attributes["ContentType"] = "Unassigned"
        element = ET.SubElement(spread, tag, attributes)
        properties = ET.SubElement(element, "Properties")

        if isinstance(item, model.Polygon) and item.points:
            offset_x = item.x + item.width / 2.0
            offset_y = item.y + item.height / 2.0
            anchors = [
                (px - offset_x, py - offset_y, None, None) for px, py in item.points
            ]
            _path_from_anchors(properties, anchors, closed=item.closed)
        elif isinstance(item, model.Path) and item.ops:
            offset_x = item.x + item.width / 2.0
            offset_y = item.y + item.height / 2.0
            subpaths = _subpaths_from_ops(item.ops, offset_x, offset_y)
            if subpaths:
                _path_from_subpaths(properties, subpaths)
            else:
                _rect_path(properties, item.width, item.height)
        else:
            _rect_path(properties, item.width, item.height)

        self._emit_transparency(element, item.style)

    def _emit_transparency(
        self, element: ET.Element, style: model.GraphicStyle
    ) -> None:
        """Opacity and shadow, both of which live in one IDML element.

        Opacity used to be written as `FillTint`, which is not what it
        means: a tint mixes the colour with the paper, so a 78% fill came
        out pale rather than see-through and looked right only over white.
        `BlendingSetting` is the real thing. It applies to the whole object
        rather than to its fill alone, which is exact for every
        transparency in the corpus -- all 46 of them, the 42 see-through
        gradients and the four faded fills, are on shapes with no stroke.
        """
        opacity = style.fill_opacity
        shadow = style.shadow
        if opacity >= 1.0 and shadow is None:
            return

        setting = ET.SubElement(element, "TransparencySetting")
        if opacity < 1.0:
            ET.SubElement(
                setting,
                "BlendingSetting",
                {
                    "BlendMode": "Normal",
                    "Opacity": fmt(max(0.0, opacity) * 100),
                    "KnockoutGroup": "false",
                    "IsolateBlending": "false",
                },
            )
        self._emit_shadow(setting, shadow)

    def _emit_shadow(
        self, setting: ET.Element, shadow: Optional[model.Shadow]
    ) -> None:
        """Publisher's offset shadow, as an IDML drop shadow.

        Publisher's is a flat copy of the shape in one colour: no blur, no
        spread, no noise, so those are written as zero rather than left to
        a default that would soften it.

        IDML states the offset twice, once as X/Y and once as an angle with
        a distance, and InDesign keeps the two in step. Which of them a
        reader believes is its own business, so both are written and they
        agree: the angle is where the light is, which is opposite the way
        the shadow falls, and 135 degrees with both offsets positive is
        InDesign's own default -- light from the top left, shadow to the
        bottom right.
        """
        if shadow is None:
            return
        ET.SubElement(
            setting,
            "DropShadowSetting",
            {
                "Mode": "Drop",
                "BlendMode": "Normal",
                "Opacity": fmt(shadow.opacity * 100),
                "XOffset": fmt(shadow.offset_x),
                "YOffset": fmt(shadow.offset_y),
                "Angle": fmt(
                    math.degrees(math.atan2(shadow.offset_y, -shadow.offset_x)) % 360.0
                ),
                "Distance": fmt(math.hypot(shadow.offset_x, shadow.offset_y)),
                "Size": "0",
                "Spread": "0",
                "Noise": "0",
                "UseGlobalLight": "false",
                "KnockedOut": "false",
                "HonorOtherEffects": "false",
                "EffectColor": self._color_ref(shadow.color, "Color/Black"),
            },
        )

    def _first_baseline_lift(self, frame: model.TextFrame) -> float:
        """How far to lift a frame so its first baseline lands where Publisher puts it.

        Publisher hangs a first baseline `spacing x usWinAscent x size`
        below the top of the text area -- so it moves with the paragraph's
        line spacing, 2.4pt on a quarter-space line and 8.6pt on a
        nine-tenths one. No IDML attribute says that. `FixedHeight` is the
        shape of the thing and is ruled out twice over, here and in
        `_emit_table`: Affinity answers it by lifting the frame off its
        position entirely. `LeadingOffset` is honoured, but it puts the
        baseline at the *whole* leading, which is `1 / 0.78` of what
        Publisher wants for Calibri.

        So the offset is bought with geometry instead. `LeadingOffset` puts
        the baseline a known distance down -- the leading -- and lifting the
        frame's top by the difference lands it where Publisher draws it.
        The height grows by the same amount, so the bottom edge stays and
        the story still runs out where it did.

        Only where all of it is known and nothing else moves: a frame that
        paints something would visibly shift, one others flow around would
        drag its wrap with it, one not aligned to its top does not hang its
        text off the first baseline at all, and a face this machine cannot
        read has no ascent to aim at. Those keep the reader's own rule.
        """
        if frame.first_baseline_from_leading or frame.wrap_text:
            return 0.0
        if _VERTICAL_JUSTIFICATION.get(frame.vertical_align, "TopAlign") != "TopAlign":
            return 0.0
        if frame.style.fill or frame.style.gradient or frame.style.stroke:
            return 0.0
        paragraphs = frame.story.paragraphs
        if not paragraphs and frame.chain_id:
            # A chain keeps its text on the head link alone, so a
            # continuation frame has nothing of its own to read. It takes
            # the head's setting rather than no lift at all: which
            # paragraph lands in it is not knowable without laying the
            # story out, and two columns of one story starting at
            # different heights is the one error that is obvious on sight.
            chain = self.doc.text_chains.get(frame.chain_id) or []
            if chain:
                paragraphs = chain[0].story.paragraphs
        if not paragraphs or not paragraphs[0].spans:
            return 0.0
        paragraph, span = paragraphs[0], paragraphs[0].spans[0]
        metrics = fontmetrics.line_metrics(span.font, span.bold, span.italic)
        if metrics is None or not metrics.height:
            return 0.0

        size = span.size_pt or DEFAULT_POINT_SIZE
        leading = _leading_for(paragraph, span)
        if leading is None:
            # Left on the reader's Auto, which Affinity resolves at 120% of
            # the size -- measured off its own export, 12.00pt on 10.0008pt
            # type -- and that is what `LeadingOffset` will then use.
            leading = SINGLE_LINE_SPACING * size
        if paragraph.line_spacing_pt is not None:
            # An exact spacing is the whole line box, so the baseline sits
            # at the same share of it that the face states.
            baseline = paragraph.line_spacing_pt * metrics.ascent / metrics.height
        else:
            spacing = paragraph.line_spacing_multiple
            baseline = (1.0 if spacing is None else spacing) * metrics.ascent * size
        return max(0.0, leading - baseline)

    def _emit_text_frame(
        self,
        spread: ET.Element,
        frame: model.TextFrame,
        page: model.Page,
        story_parts: List[str],
        offset_x: Optional[float] = None,
    ) -> None:
        link = self._chain_links.get(id(frame))
        if link is None:
            story_id = self.ids.next("story")
            self_id, previous, following = None, "n", "n"
        else:
            story_id = link.story_id
            self_id, previous, following = link.self_id, link.previous, link.following

        # Where the first baseline goes has to be bought with frame height,
        # because IDML has no way to state it -- see `_first_baseline_lift`.
        # The lift is taken off the top and given back as height, so the
        # text sits in exactly the band Publisher gave it and the bottom
        # edge, which is where the story runs out, does not move.
        lift = self._first_baseline_lift(frame)
        placed = (
            replace(frame, y=frame.y - lift, height=frame.height + lift)
            if lift else frame
        )

        attributes = self._frame_attributes(
            placed, page, "$ID/[Normal Text Frame]", self_id, offset_x
        )
        attributes.update(
            {
                "ContentType": "TextType",
                "ParentStory": story_id,
                "PreviousTextFrame": previous,
                "NextTextFrame": following,
            }
        )
        element = ET.SubElement(spread, "TextFrame", attributes)
        properties = ET.SubElement(element, "Properties")
        _rect_path(properties, placed.width, placed.height)

        top, right, bottom, left = frame.padding
        columns = max(1, frame.columns)
        preference = {
            "TextColumnCount": str(columns),
            "VerticalJustification": _VERTICAL_JUSTIFICATION.get(
                frame.vertical_align, "TopAlign"
            ),
            "InsetSpacing": f"{fmt(top)} {fmt(left)} {fmt(bottom)} {fmt(right)}",
            "AutoSizingType": "Off",
        }
        if frame.first_baseline_from_leading or lift:
            # The leading, not the font's ascent, decides where the first
            # baseline goes. Left unstated, Affinity hangs it a full
            # usWinAscent below the frame's top -- 0.86 of an em against
            # the 0.69 a WordArt band is tall for this corpus's dropped
            # initial -- and a first baseline past the bottom of its frame
            # is a line it hides rather than draws.
            #
            # `LeadingOffset` and not `FixedHeight`: the two were measured
            # side by side and Affinity draws the headline above its box
            # for FixedHeight, which is the same lifting-off-position bug
            # `_emit_table` records for a table.
            preference["FirstBaselineOffset"] = "LeadingOffset"
        if columns > 1:
            # Stated explicitly, never left to the reader's default: InDesign's
            # is 12pt against Publisher's 2mm, which would widen every gap and
            # narrow every column. Publisher's column spacing is the space
            # between columns only, the same thing IDML calls the gutter, so
            # it does not overlap InsetSpacing above.
            preference["TextColumnGutter"] = fmt(frame.column_gap)
        ET.SubElement(element, "TextFramePreference", preference)
        # The same switch a placed image's wrap answers to: it is the one
        # question, whether placement or legibility wins where Publisher's
        # objects overlap.
        if frame.wrap_text and self.wrap_images:
            self._emit_text_wrap(element, frame)
        self._emit_transparency(element, frame.style)

        # A chain's text belongs to the story, not to each frame that shows
        # it, so it is written once — at the head, the only link still
        # holding the paragraphs.
        if link is None or link.head:
            part_name = f"Stories/Story_{story_id}.xml"
            story_parts.append(part_name)
            self._parts[part_name] = self._story_part(
                story_id, frame.story, (frame.width, frame.height)
            )

    def _emit_table(
        self,
        spread: ET.Element,
        table: model.Table,
        page: model.Page,
        story_parts: List[str],
        offset_x: Optional[float] = None,
    ) -> None:
        """A table is a frame whose story contains an IDML Table."""
        story_id = self.ids.next("story")
        attributes = self._frame_attributes(
            table, page, "$ID/[Normal Text Frame]", None, offset_x
        )
        attributes.update(
            {
                "ContentType": "TextType",
                "ParentStory": story_id,
                "PreviousTextFrame": "n",
                "NextTextFrame": "n",
            }
        )
        element = ET.SubElement(spread, "TextFrame", attributes)
        properties = ET.SubElement(element, "Properties")
        _rect_path(properties, table.width, table.height)
        ET.SubElement(
            element,
            "TextFramePreference",
            {
                "TextColumnCount": "1",
                "VerticalJustification": "TopAlign",
                "InsetSpacing": "0 0 0 0",
                "AutoSizingType": "Off",
                # A table cannot flow around anything. Ordinary copy meets a
                # wrapping picture by narrowing its lines; a row has no way
                # to do that, so a reader clears the whole table past the
                # obstruction instead -- and a table pushed off Publisher's
                # coordinates is worse than one a picture overlaps, which is
                # what Publisher itself draws. The corpus has 83 of these
                # overlaps, including the agenda on page 7 of both `1337`
                # and `1338`, where a bin standing 18pt into the right edge
                # of the frame is enough to move the whole grid.
                #
                # Only the table frame ignores wraps. The switch that turns
                # image wraps on stays exactly as it was for text, where
                # reflowing is the right answer and the reason it exists.
                "IgnoreWrap": "true",
                # No FirstBaselineOffset here, deliberately. Pinning it to
                # the top of the frame reads like the right thing -- a
                # table has no baseline to offset -- but Affinity answers
                # "FixedHeight" with a minimum of zero by lifting the whole
                # table a full frame height off its position. Measured, not
                # argued: research/probe_table_placement.py.
            },
        )

        part_name = f"Stories/Story_{story_id}.xml"
        story_parts.append(part_name)
        self._parts[part_name] = self._table_story_part(story_id, table)

    def _table_story_part(self, story_id: str, table: model.Table) -> bytes:
        root = ET.Element("idPkg:Story", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION})
        story = ET.SubElement(
            root,
            "Story",
            {
                "Self": story_id,
                "AppliedTOCStyle": "n",
                "TrackChanges": "false",
                "StoryTitle": "",
                "AppliedNamedGrid": "n",
            },
        )
        ET.SubElement(
            story,
            "StoryPreference",
            {
                "OpticalMarginAlignment": "false",
                "OpticalMarginSize": "12",
                "FrameType": "TextFrameType",
                "StoryOrientation": "Horizontal",
                "StoryDirection": "LeftToRightDirection",
            },
        )
        paragraph = ET.SubElement(
            story,
            "ParagraphStyleRange",
            {"AppliedParagraphStyle": NO_PARAGRAPH_STYLE},
        )
        run = ET.SubElement(
            paragraph, "CharacterStyleRange", {"AppliedCharacterStyle": NO_CHARACTER_STYLE}
        )

        table_id = self.ids.next("table")
        element = ET.SubElement(
            run,
            "Table",
            {
                "Self": table_id,
                "AppliedTableStyle": "TableStyle/$ID/[Basic Table]",
                "TableDirection": "LeftToRightDirection",
                "HeaderRowCount": "0",
                "FooterRowCount": "0",
                "BodyRowCount": str(table.row_count),
                "ColumnCount": str(table.column_count),
            },
        )
        # IDML wants every Row, then every Column, then the Cells.
        for index in range(table.row_count):
            height = (
                table.row_heights[index] if index < len(table.row_heights) else 0.0
            )
            ET.SubElement(
                element,
                "Row",
                {
                    "Self": f"{table_id}Row{index}",
                    "Name": str(index),
                    "SingleRowHeight": fmt(height),
                },
            )
        for index in range(table.column_count):
            width = (
                table.column_widths[index]
                if index < len(table.column_widths)
                else 0.0
            )
            ET.SubElement(
                element,
                "Column",
                {
                    "Self": f"{table_id}Column{index}",
                    "Name": str(index),
                    "SingleColumnWidth": fmt(width),
                },
            )
        setting = _table_setting(table)
        for cell in table.cells:
            # A cell is named column first, then row.
            attributes = {
                "Self": f"{table_id}i{cell.column}i{cell.row}",
                "Name": f"{cell.column}:{cell.row}",
                "AppliedCellStyle": "CellStyle/$ID/[None]",
                "RowSpan": str(max(1, cell.row_span)),
                "ColumnSpan": str(max(1, cell.column_span)),
            }
            # Left unwritten where Publisher said nothing, so a cell whose
            # padding we do not know keeps whatever the reader defaults to
            # rather than being flattened to nothing.
            if cell.insets is not None:
                attributes.update(
                    {
                        "LeftInset": fmt(cell.insets.left),
                        "TopInset": fmt(cell.insets.top),
                        "RightInset": fmt(cell.insets.right),
                        "BottomInset": fmt(cell.insets.bottom),
                    }
                )
            # Publisher leaves this field out for a top-aligned cell, so
            # a cell whose record we read always states one, top included
            # -- and saying it is what keeps the reader's own default from
            # standing where Publisher stated something.
            if cell.vertical_align is not None:
                attributes["VerticalJustification"] = _VERTICAL_JUSTIFICATION[
                    cell.vertical_align
                ]
            # Both spellings of "no line", on every edge. We name
            # [Basic Table] without defining it, so an edge we say nothing
            # about is ruled by the reader -- Affinity draws a line around
            # every cell -- and that grid belongs to no .pub. Measured, not
            # argued: research/probe_cell_rules.py shows Affinity honouring
            # a zero weight and a None swatch separately and an override
            # per edge, so stating both says the same thing twice to a
            # reader that reads either.
            if cell.unruled:
                for edge in _CELL_EDGES:
                    rule = cell.rules.get(edge.lower())
                    if rule is None:
                        attributes[f"{edge}EdgeStrokeWeight"] = fmt(0.0)
                        attributes[f"{edge}EdgeStrokeColor"] = "Swatch/None"
                        continue
                    # A side Publisher did draw. Its weight is the one
                    # the drawing states; a rule whose colour did not
                    # resolve keeps the reader's, which is a line of the
                    # right thickness in the wrong colour rather than no
                    # line at all.
                    attributes[f"{edge}EdgeStrokeWeight"] = fmt(rule.weight)
                    if rule.color is not None:
                        attributes[f"{edge}EdgeStrokeColor"] = self._color_ref(
                            rule.color
                        )
            if cell.shade is not None:
                attributes["FillColor"] = self._color_ref(cell.shade)
            node = ET.SubElement(element, "Cell", attributes)
            paragraphs = cell.story.paragraphs or [setting.placeholder()]
            for position, block in enumerate(paragraphs):
                last = position == len(paragraphs) - 1
                self._emit_paragraph(
                    node,
                    block,
                    last=last,
                    first=(position == 0),
                    in_cell=True,
                    fill_size=setting.size,
                )
        return _serialise(root)

    @staticmethod
    def _emit_text_wrap(
        element: ET.Element, item: Optional[model.Item] = None
    ) -> None:
        """Ask the text under an object to flow around it instead.

        libmspub reports no wrap *mode* for anything, so every object
        Publisher floated over its copy arrives with nothing to say it was
        floated. A bounding-box wrap is what those documents almost always
        intend, and the alternative is the object drawn straight over the
        words.

        The room the wrap leaves is not a guess: the file states a distance
        per side and `convert._apply_wrap_offsets` puts it on the item. Left
        at zero -- as this did until it was measured -- the copy runs right
        up against the picture where Publisher keeps 0.04in of air, and the
        wrap ends level with the picture instead of below it, which widens
        the column a line early. Zero stays the answer for an object whose
        distances could not be read, since a gap nothing states is one this
        would be inventing.
        """
        wrap = ET.SubElement(
            element,
            "TextWrapPreference",
            {
                "Inverse": "false",
                "ApplyToMasterPageOnly": "false",
                "TextWrapSide": "BothSides",
                "TextWrapMode": "BoundingBoxTextWrap",
            },
        )
        offsets = getattr(item, "wrap_offsets", None) or (0.0, 0.0, 0.0, 0.0)
        top, left, bottom, right = offsets
        ET.SubElement(
            ET.SubElement(wrap, "Properties"),
            "TextWrapOffset",
            {
                "Top": fmt(top), "Left": fmt(left),
                "Bottom": fmt(bottom), "Right": fmt(right),
            },
        )

    def _emit_image(
        self,
        spread: ET.Element,
        item: model.Image,
        page: model.Page,
        offset_x: Optional[float] = None,
    ) -> None:
        # Anything not in the table has no IDML representation. Emitting it
        # anyway under the old "$ID/JPEG" default wrote, say, an .emf and
        # told Affinity it was a JPEG: the package opens, the artwork is
        # silently absent, and the run still reports ok. Skipping it and
        # saying so is the honest outcome.
        type_name = _IMAGE_TYPE_NAME.get(item.mime_type)
        if type_name is None:
            self.doc.warnings.append(
                f"image dropped: {item.mime_type} has no IDML equivalent"
            )
            return

        attributes = self._frame_attributes(
            item, page, "$ID/[Normal Graphics Frame]", offset_x=offset_x
        )
        attributes["ContentType"] = "GraphicType"
        # A picture frame's own fill would paint over the artwork.
        attributes["FillColor"] = "Swatch/None"
        rectangle = ET.SubElement(spread, "Rectangle", attributes)
        properties = ET.SubElement(rectangle, "Properties")
        _rect_path(properties, item.width, item.height)

        # libmspub reports no text-wrap information, and images are emitted
        # after the text frames, so without this they simply paint over the
        # copy and hide it. A bounding-box wrap reproduces what Publisher
        # documents almost always intend. Page-sized images are excluded:
        # those are backgrounds, and wrapping would push all text off.
        if self.wrap_images and item.width * item.height < 0.6 * page.width * page.height:
            self._emit_text_wrap(rectangle, item)

        # On the frame rather than the Image inside it: Publisher shadows
        # the picture as placed, and a shadow on the content would sit
        # under the frame's own clipping.
        self._emit_transparency(rectangle, item.style)

        self._image_count += 1
        embed = self.image_dir_name is None
        directory = _NOTIONAL_IMAGE_DIR if embed else self.image_dir_name
        filename = (
            f"{directory}/image{self._image_count}"
            f"{model.extension_for(item.mime_type)}"
        )
        if not embed:
            self.image_files.append((filename, item.data))

        placed_w, placed_h = _content_bounds(
            item.content_rotation, item.width, item.height
        )
        image = ET.SubElement(
            rectangle,
            "Image",
            {
                "Self": self.ids.next("img"),
                "ItemTransform": _content_matrix(
                    item.content_rotation, placed_w, placed_h
                ),
                "ImageTypeName": type_name,
                **_resolution(item.data, placed_w, placed_h),
                "Visible": "true",
            },
        )
        image_properties = ET.SubElement(image, "Properties")
        profile = ET.SubElement(image_properties, "Profile", {"type": "string"})
        profile.text = "$ID/Embedded"
        ET.SubElement(
            image_properties,
            "GraphicBounds",
            {
                "Left": "0",
                "Top": "0",
                "Right": fmt(placed_w),
                "Bottom": fmt(placed_h),
            },
        )
        if embed:
            # After Profile and GraphicBounds because that is the order the
            # probe Affinity accepted was built in, and unwrapped because
            # that is how it was written there too. Hex was ignored; base64
            # drew the picture.
            #
            # This holds the encoded text -- a third larger again than the
            # bytes the document model is already holding -- until the part
            # is serialised. parse_document's docstring puts peak memory at
            # roughly three times the file size per worker; embedding adds
            # to that, which matters when sizing the thread pool. In the
            # package itself it costs nothing: deflate takes base64's
            # overhead back out, measured at 1.01x the linked package
            # across 60 MB of corpus artwork.
            contents = ET.SubElement(image_properties, "Contents")
            contents.text = base64.b64encode(item.data).decode("ascii")
        ET.SubElement(
            image,
            "Link",
            {
                "Self": self.ids.next("link"),
                # The sidecar folder is named after the source file, so the
                # path carries whatever the user's archive contains. A bare
                # '#' truncates the URI at the fragment and a bare '%' is an
                # invalid escape, either of which breaks the link silently.
                # The safe set is RFC 3986's legal path characters, so names
                # that already worked — parentheses in particular — are left
                # byte-for-byte alone and only genuinely illegal characters
                # (space, '#', '%') are escaped.
                #
                # An embedded image keeps the attribute even though nothing
                # is ever written at that path: the probe pointed it at a
                # file it knew to be absent and Affinity drew the picture
                # anyway, so an unresolvable URI is demonstrably fine while
                # Contents is present, and inventing a shape the probe did
                # not test would be guessing. If the Resource Manager ever
                # complains about a missing link, dropping the attribute
                # entirely is the first thing to try.
                "LinkResourceURI": "file:" + quote(filename, safe=_URI_PATH_SAFE),
                "LinkResourceFormat": type_name,
                "StoredState": "Embedded" if embed else "Normal",
                "LinkClassID": "35906",
                "LinkClientID": "257",
                "LinkResourceModified": "false",
                "LinkObjectModified": "false",
                "ShowInUI": "true",
                "CanEmbed": "true",
                "CanUnembed": "true",
                "CanPackage": "true",
                "ImportPolicy": "NoAutoImport",
                "ExportPolicy": "NoAutoExport",
            },
        )

    # -- stories ----------------------------------------------------------

    def _story_part(
        self, story_id: str, story: model.Story, box: Optional[tuple] = None
    ) -> bytes:
        root = ET.Element("idPkg:Story", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION})
        element = ET.SubElement(
            root,
            "Story",
            {
                "Self": story_id,
                "AppliedTOCStyle": "n",
                "TrackChanges": "false",
                "StoryTitle": "",
                "AppliedNamedGrid": "n",
            },
        )
        ET.SubElement(
            element,
            "StoryPreference",
            {
                "OpticalMarginAlignment": "false",
                "OpticalMarginSize": "12",
                "FrameType": "TextFrameType",
                "StoryOrientation": "Horizontal",
                "StoryDirection": "LeftToRightDirection",
            },
        )

        paragraphs = story.paragraphs or [model.Paragraph()]
        for position, paragraph in enumerate(paragraphs):
            self._emit_paragraph(
                element, paragraph, last=(position == len(paragraphs) - 1), box=box
            )
        return _serialise(root)

    def _emit_paragraph(
        self,
        story: ET.Element,
        paragraph: model.Paragraph,
        last: bool,
        box: Optional[tuple] = None,
        first: bool = False,
        in_cell: bool = False,
        fill_size: Optional[float] = None,
    ) -> None:
        attributes = {
            "AppliedParagraphStyle": NO_PARAGRAPH_STYLE,
            "Justification": _JUSTIFICATION.get(paragraph.align, "LeftAlign"),
        }
        # A cell's own edge has nothing to space away from, and Publisher
        # lays out none of it there: the rows of the tables that state it
        # measure the leading and no more. Written out, the reader grows
        # every row that carries it -- downwards, out of the frame the
        # table was placed in. backlog.md 11.
        if paragraph.space_before and not (in_cell and first):
            attributes["SpaceBefore"] = fmt(paragraph.space_before)
        if paragraph.space_after and not (in_cell and last):
            attributes["SpaceAfter"] = fmt(paragraph.space_after)
        if paragraph.margin_left:
            attributes["LeftIndent"] = fmt(paragraph.margin_left)
        if paragraph.margin_right:
            attributes["RightIndent"] = fmt(paragraph.margin_right)
        if paragraph.first_line_indent:
            attributes["FirstLineIndent"] = fmt(paragraph.first_line_indent)

        if paragraph.align == "justify":
            # InDesign defaults SingleWordJustification to FullyJustified,
            # which letter-spaces a lone word across the whole measure
            # ("a m o r e ."). Publisher left-aligns those, so match it.
            attributes["SingleWordJustification"] = "LeftAlign"

        range_element = ET.SubElement(story, "ParagraphStyleRange", attributes)
        _emit_tab_stops(range_element, paragraph)

        spans = paragraph.spans or [model.Span(size_pt=fill_size)]
        for span in spans:
            leading = _leading_for(paragraph, span)
            if in_cell:
                leading = _first_line_leading(paragraph, span, leading)
            self._emit_span(range_element, span, leading, box)

        # IDML marks the end of a paragraph with an explicit break.
        if not last:
            ET.SubElement(range_element, "Br")

    def _emit_span(
        self,
        parent: ET.Element,
        span: model.Span,
        leading: Optional[float] = None,
        box: Optional[tuple] = None,
    ) -> None:
        attributes = {"AppliedCharacterStyle": NO_CHARACTER_STYLE}
        if span.size_pt:
            attributes["PointSize"] = fmt(span.size_pt)
        # Text takes a fill the same way a shape does, so a WordArt ramp
        # can stay a ramp instead of collapsing to its first stop.
        reference = (
            self.gradient_ids.get(span.gradient) if span.gradient is not None else None
        )
        if reference:
            # A shape gets its ramp geometry from its own bounds; a run has
            # none of its own, and the default is a length of nothing --
            # which paints every stop before the start point in the first
            # colour and everything after it in the last, so a two-stop
            # ramp comes out as two solid halves with a hard edge down the
            # middle. The band the headline sits in is the distance the
            # ramp was meant to run over, so it is stated here.
            angle, start, length = _ramp_placement(span.gradient, *(box or ()))
            attributes["FillColor"] = reference
            attributes["GradientFillAngle"] = fmt(angle)
            if start is not None:
                attributes["GradientFillStart"] = start
                attributes["GradientFillLength"] = fmt(length)
        else:
            attributes["FillColor"] = self._color_ref(span.color, "Color/Black")
        if span.stroke is not None:
            attributes["StrokeColor"] = self._color_ref(span.stroke)
            if span.stroke_width:
                attributes["StrokeWeight"] = fmt(span.stroke_width)
        if span.underline:
            attributes["Underline"] = "true"
        if span.strikethrough:
            attributes["StrikeThru"] = "true"
        if span.superscript:
            attributes["Position"] = "Superscript"
        elif span.subscript:
            attributes["Position"] = "Subscript"
        # Not styling, but it decides hyphenation, and Dutch broken as
        # English reflows every line after the first bad break.
        language = _language_name(span.language)
        if language:
            attributes["AppliedLanguage"] = f"$ID/{language}"
        # Unscaled text says nothing rather than saying 100.
        if span.horizontal_scale and abs(span.horizontal_scale - 100.0) > 0.01:
            attributes["HorizontalScale"] = fmt(span.horizontal_scale)
        # Likewise text set at normal spacing, where 0 is the whole of it.
        if span.tracking and abs(span.tracking) > 0.01:
            attributes["Tracking"] = fmt(span.tracking)
        # The slant of a family with no italic in it, which Publisher draws
        # by shearing the glyphs and IDML states as an angle.
        if span.skew and abs(span.skew) > 0.01:
            attributes["Skew"] = fmt(span.skew)

        element = ET.SubElement(parent, "CharacterStyleRange", attributes)

        # Leading is a character property in IDML, not a paragraph one, so a
        # paragraph mixing type sizes gets a value per run and InDesign uses
        # the largest on each line -- which is what Publisher does too.
        properties = None
        if leading is not None:
            properties = ET.SubElement(element, "Properties")
            ET.SubElement(properties, "Leading", {"type": "unit"}).text = fmt(leading)
        if span.font:
            if properties is None:
                properties = ET.SubElement(element, "Properties")
            applied = ET.SubElement(properties, "AppliedFont", {"type": "string"})
            applied.text = span.font
        # A named style wins over the two flags: a family whose weights are
        # its styles has faces bold and italic cannot name between them, and
        # where the run is in one of those, saying 'Bold' reaches nothing.
        if span.font_style:
            element.set("FontStyle", span.font_style)
        elif span.bold or span.italic:
            style_name = " ".join(
                part for part, on in (("Bold", span.bold), ("Italic", span.italic)) if on
            )
            element.set("FontStyle", style_name)

        # Tabs are structural in IDML, so the run is split around them.
        segments = span.text.split("\t")
        for position, segment in enumerate(segments):
            if position:
                ET.SubElement(element, "Tab")
            if segment:
                content = ET.SubElement(element, "Content")
                content.text = segment


_TAB_ALIGNMENTS = {"left": "LeftAlign", "center": "CenterAlign", "right": "RightAlign"}


def _tab_stops_for(paragraph: model.Paragraph) -> List[model.TabStop]:
    """Every stop this paragraph should carry, in order along the measure.

    Two sources, and they add rather than compete. The .pub states stops
    for a paragraph the author set them on, and those are exact. A hanging
    indent implies one more at the left indent -- the outdented first line
    carries a label, the tab after it moves to where the wrapped lines
    start -- which Publisher and Word both honour without recording it.

    A paragraph with neither is left alone: its tabs land on the reader's
    own grid, half an inch in InDesign, and inventing a stop for it would
    move text that is currently where it should be.

    A stop at or left of the text edge is dropped. One style in the corpus
    puts three there, and IDML measures a stop from that edge, so there is
    nowhere to write them.
    """
    stops = [stop for stop in paragraph.tab_stops if stop.position > 0]
    hangs = paragraph.first_line_indent < 0 < paragraph.margin_left
    if hangs and not any(
        abs(stop.position - paragraph.margin_left) < 0.01 for stop in stops
    ):
        stops.append(model.TabStop(position=paragraph.margin_left))
    return sorted(stops, key=lambda stop: stop.position)


def _emit_tab_stops(parent: ET.Element, paragraph: model.Paragraph) -> None:
    """Write a paragraph's tab stops, where it has any to write."""
    stops = _tab_stops_for(paragraph)
    if not stops:
        return
    tabs = ET.SubElement(parent, "Properties")
    listing = ET.SubElement(tabs, "TabList", {"type": "list"})
    for stop in stops:
        ET.SubElement(listing, "ListItem", {"type": "record"}).extend(
            [
                _tab_field(
                    "Alignment",
                    "enumeration",
                    _TAB_ALIGNMENTS.get(stop.alignment, "LeftAlign"),
                ),
                _tab_field("AlignmentCharacter", "string", "."),
                _tab_field("Leader", "string", ""),
                _tab_field("Position", "unit", fmt(stop.position)),
            ]
        )


def _tab_field(name: str, kind: str, value: str) -> ET.Element:
    element = ET.Element(name, {"type": kind})
    element.text = value
    return element


class _TableSetting(NamedTuple):
    """How the body of a table is set, for a cell that records nothing.

    Publisher records no run -- often no paragraph at all -- in a cell
    with nothing in it, and what states no size and no leading is set in
    the reader's own: 12pt on Auto, against rows these documents build at
    9. A row cannot shrink to fit, so the reader's default grows it and
    everything below sinks. How the rest of the table is set is how that
    cell would have been set.
    """

    size: Optional[float] = None
    line_spacing_multiple: Optional[float] = None
    line_spacing_pt: Optional[float] = None

    def placeholder(self) -> model.Paragraph:
        """The paragraph to stand in for one the file never recorded."""
        return model.Paragraph(
            line_spacing_multiple=self.line_spacing_multiple,
            line_spacing_pt=self.line_spacing_pt,
        )


def _table_setting(table: model.Table) -> _TableSetting:
    """How most of a table's own text is set. Empty where it states none."""
    counts: Dict[_TableSetting, int] = {}
    for cell in table.cells:
        for paragraph in cell.story.paragraphs:
            for span in paragraph.spans:
                if not span.size_pt:
                    continue
                found = _TableSetting(
                    span.size_pt,
                    paragraph.line_spacing_multiple,
                    paragraph.line_spacing_pt,
                )
                counts[found] = counts.get(found, 0) + 1
    if not counts:
        return _TableSetting()

    def rank(setting: _TableSetting) -> tuple:
        # Most used first, then the tightest, then the smallest: where the
        # table is set two ways equally, the setting that cannot make a
        # row taller than the file states is the safer stand-in.
        return (-counts[setting], _setting_leading(setting), setting.size or 0.0)

    return min(counts, key=rank)


def _setting_leading(setting: _TableSetting) -> float:
    """What one line of `setting` measures, Auto resolved the way IDML does."""
    if setting.line_spacing_pt is not None:
        return setting.line_spacing_pt
    size = setting.size or DEFAULT_POINT_SIZE
    multiple = setting.line_spacing_multiple
    return (multiple if multiple is not None else 1.0) * SINGLE_LINE_SPACING * size


def _first_line_leading(
    paragraph: model.Paragraph, span: model.Span, leading: Optional[float]
) -> Optional[float]:
    """What one line of a cell paragraph measures where the reader sizes it.

    Publisher opens spacing above single *between* lines rather than above
    the first one, so a cell holding a single line is as tall as that line
    however wide the spacing is set. IDML has no way to say that -- its
    leading is every line -- and a reader given 150% of a 10pt line makes
    the row 18pt where Publisher makes it 12. A row only grows, so that
    lands as the whole table sitting low and running over what follows.

    Measured, not argued: 1338's page-7 agenda states 150% on its date and
    time cells, and Publisher's own PDF export puts the three rows 12.12
    and 12.24pt apart -- Calibri's natural line -- in a table its frame
    gives 35.81pt for. Spacing *below* single is left alone: Publisher
    does compress a single line, which is what 1336's 9.7pt rows on 75%
    cells are, and that already lands right.
    """
    multiple = paragraph.line_spacing_multiple
    if multiple is None or multiple <= 1.0:
        return leading
    return _single_line(span)


def _single_line(span: model.Span) -> float:
    """One space of Publisher line spacing, in points, for this run's face.

    Read from the font where it can be: Publisher's space is the face's own
    usWinAscent plus usWinDescent, and the 120% in `SINGLE_LINE_SPACING` is
    a stand-in for a font this machine has not got.
    """
    size = span.size_pt or DEFAULT_POINT_SIZE
    metrics = fontmetrics.line_metrics(span.font, span.bold, span.italic)
    return (metrics.height if metrics else SINGLE_LINE_SPACING) * size


def _leading_for(paragraph: model.Paragraph, span: model.Span) -> Optional[float]:
    """The leading in points for one run, or None to leave it on Auto.

    Publisher's exact point spacing maps straight across. Its "spaces"
    figure is proportional, so it is resolved against the run's own type
    size and the face that size is set in -- 0.9 spaces of 10pt Calibri is
    0.9 x 1.2207 x 10 = 10.99pt, which is what Publisher draws, not the
    10.8 that 120% gives.
    """
    if paragraph.line_spacing_pt is not None:
        return paragraph.line_spacing_pt
    if paragraph.line_spacing_multiple is None:
        # Left on the reader's Auto, which is 120% -- 1.7% tighter than
        # Publisher's space for Calibri. Stating it instead is the obvious
        # next step and is deliberately not taken here: it would put an
        # explicit leading on every run of every document, and the row
        # heights of a table are measured against exactly this (backlog.md
        # on the tables that sank), so it wants its own measurement first.
        return None
    return paragraph.line_spacing_multiple * _single_line(span)


def _flatten(items: List[model.Item]) -> List[model.Item]:
    output: List[model.Item] = []
    for item in items:
        if isinstance(item, model.Group):
            output.extend(_flatten(item.children))
        else:
            output.append(item)
    return output


def _subpaths_from_ops(ops: List[tuple], offset_x: float, offset_y: float):
    """Split normalised path operations into subpaths of IDML path points.

    IDML expresses curves as anchors with incoming/outgoing direction
    handles rather than as separate segment records, so cubic control
    points are folded onto the anchors they belong to.

    Every "M" starts a new subpath, and each is emitted separately. Running
    them together is not a harmless simplification: libmspub reports most
    Publisher paths as disconnected edges -- 50 of the 56 in the sample
    corpus -- and welding two horizontal rules into one outline drew a
    filled bowtie across the page instead of two lines.
    """
    subpaths: List[tuple] = []
    anchors: List[list] = []
    closed = False

    def finish() -> None:
        nonlocal anchors, closed
        if len(anchors) >= 2:
            subpaths.append(([tuple(a) for a in anchors], closed))
        anchors, closed = [], False

    for op in ops:
        kind = op[0]
        if kind == "M":
            finish()
            anchors.append([op[1] - offset_x, op[2] - offset_y, None, None])
        elif kind == "L":
            anchors.append([op[1] - offset_x, op[2] - offset_y, None, None])
        elif kind == "C":
            x1, y1, x2, y2, x, y = op[1:]
            if anchors:
                anchors[-1][3] = (x1 - offset_x, y1 - offset_y)
            anchors.append([x - offset_x, y - offset_y, (x2 - offset_x, y2 - offset_y), None])
        elif kind == "Q":
            x1, y1, x, y = op[1:]
            if anchors:
                anchors[-1][3] = (x1 - offset_x, y1 - offset_y)
            anchors.append([x - offset_x, y - offset_y, (x1 - offset_x, y1 - offset_y), None])
        elif kind == "Z":
            closed = True

    finish()
    return subpaths
