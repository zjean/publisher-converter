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

import math
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional
from urllib.parse import quote

from . import imagemeta, model
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

NO_PARAGRAPH_STYLE = "ParagraphStyle/$ID/[No paragraph style]"
NO_CHARACTER_STYLE = "CharacterStyle/$ID/[No character style]"

# InDesign's Auto leading is 120% of the type size, and that is also what
# Publisher calls one "space" of line spacing. Equating the two is what
# makes the most common case right by construction: libmspub omits the
# property entirely at 1 sp, so single-spaced text is written with no
# leading at all and picks up Auto -- the same 120%.
SINGLE_LINE_SPACING = 1.2

# The point size IDML assumes for a run that does not state one.
DEFAULT_POINT_SIZE = 12.0


def _pkg(tag: str) -> str:
    return f"{{{IDPKG}}}{tag}"


class MalformedPartError(Exception):
    """Raised when a generated part is not well-formed XML."""


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

    Images are written alongside the .idml into a sidecar folder and
    referenced by relative URI, which is how InDesign packages normally
    carry placed artwork and what Affinity resolves most reliably.
    """

    def __init__(
        self,
        document: model.Document,
        image_dir_name: Optional[str] = None,
        wrap_images: bool = True,
        facing_pages: bool = False,
    ):
        self.doc = document
        self.ids = _Ids()
        self.image_dir_name = image_dir_name
        self.wrap_images = wrap_images
        self.facing_pages = facing_pages
        self.color_ids: Dict[model.Color, str] = {}
        # Equal gradients share one resource, which is why model.Gradient is
        # frozen: Publisher repeats the same ramp across a document.
        self.gradient_ids: Dict[model.Gradient, str] = {}
        self.fonts: List[str] = []
        # (relative path, bytes) pairs the caller must write next to the IDML
        self.image_files: List[tuple] = []
        self._parts: Dict[str, bytes] = {}
        # id(frame) -> _ChainLink, for the frames of a threaded story. Keyed
        # by identity, not equality: the continuation links of a chain hold no
        # text and often share their geometry, so as dataclasses they compare
        # equal to one another and a lookup by value would conflate them.
        self._chain_links: Dict[int, _ChainLink] = {}

    # -- public API -------------------------------------------------------

    def write(self, idml_path: Path) -> None:
        idml_path = Path(idml_path)
        self._build()

        idml_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(idml_path, "w", zipfile.ZIP_DEFLATED) as archive:
            # The mimetype entry must be first and uncompressed.
            archive.writestr(
                zipfile.ZipInfo("mimetype"),
                "application/vnd.adobe.indesign-idml-package",
                compress_type=zipfile.ZIP_STORED,
            )
            for name, payload in self._parts.items():
                archive.writestr(name, payload)

        if self.image_files:
            image_root = idml_path.parent / self.image_dir_name
            image_root.mkdir(parents=True, exist_ok=True)
            for relative_name, payload in self.image_files:
                (idml_path.parent / relative_name).write_bytes(payload)

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
        for item in self.doc.all_items():
            gradient = item.style.gradient
            if gradient is not None and gradient not in self.gradient_ids:
                self.gradient_ids[gradient] = f"Gradient/G_{len(self.gradient_ids) + 1}"

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

    def _master_spread_part(self, master: model.Master, story_parts: List[str]) -> bytes:
        """A MasterSpread holding content Publisher kept once, not per page.

        Same shape as a Spread, and its items keep the page coordinates
        they already had: a master serves only pages of its own size, so
        the centred origin is identical and nothing needs moving.
        """
        root = ET.Element(
            "idPkg:MasterSpread", {"xmlns:idPkg": IDPKG, "DOMVersion": DOM_VERSION}
        )
        identifier = self.master_id(master.name)
        spread = ET.SubElement(
            root,
            "MasterSpread",
            {
                "Self": identifier,
                "Name": f"{master.name}-Master",
                "NamePrefix": master.name,
                "BaseName": "Master",
                "ShowMasterItems": "true",
                "PageCount": "1",
                "OverriddenPageItemProps": "",
                "ItemTransform": "1 0 0 1 0 0",
            },
        )
        half_w, half_h = master.width / 2.0, master.height / 2.0
        ET.SubElement(
            spread,
            "Page",
            {
                "Self": f"{identifier}_page",
                "Name": master.name,
                "AppliedMaster": "n",
                "OverrideList": "",
                "GeometricBounds": f"0 0 {fmt(master.height)} {fmt(master.width)}",
                "ItemTransform": f"1 0 0 1 {fmt(-half_w)} {fmt(-half_h)}",
                "AppliedTrapPreset": "TrapPreset/$ID/kDefaultTrapStyleName",
                "GridStartingPoint": "TopOutside",
                "UseMasterGrid": "true",
            },
        )
        page = model.Page(width=master.width, height=master.height)
        for item in _flatten(master.items):
            self._emit_item(spread, item, page, story_parts)
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
            ET.SubElement(
                group,
                "Font",
                {
                    "Self": f"font{index}_regular",
                    "FontFamily": family,
                    "Name": f"{family} Regular",
                    "PostScriptName": family.replace(" ", ""),
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
            for index, stop in enumerate(gradient.stops):
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
        ET.SubElement(
            root,
            "DocumentPreferences",
            {
                "PageHeight": fmt(first.height),
                "PageWidth": fmt(first.width),
                "PagesPerDocument": str(max(1, len(self.doc.pages))),
                "FacingPages": "true" if self.facing_pages else "false",
                "PageOrientation": "Portrait" if first.height >= first.width else "Landscape",
            },
        )
        ET.SubElement(
            root,
            "ViewPreference",
            {"HorizontalMeasurementUnits": "Points", "VerticalMeasurementUnits": "Points"},
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
            ET.SubElement(
                spread,
                "Page",
                {
                    "Self": f"page{number}",
                    "Name": str(number),
                    "AppliedMaster": self.master_id(page.master) if page.master else "n",
                    "OverrideList": "",
                    "GeometricBounds": f"0 0 {fmt(page.height)} {fmt(page.width)}",
                    "ItemTransform": (
                        f"1 0 0 1 {fmt(offset_x)} {fmt(-page.height / 2.0)}"
                    ),
                    "AppliedTrapPreset": "TrapPreset/$ID/kDefaultTrapStyleName",
                    "GridStartingPoint": "TopOutside",
                    "UseMasterGrid": "true",
                },
            )

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
            # Publisher's angle is degrees, the same convention IDML uses.
            attributes["GradientFillAngle"] = fmt(gradient.angle)
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

        attributes = self._frame_attributes(
            frame, page, "$ID/[Normal Text Frame]", self_id, offset_x
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
        _rect_path(properties, frame.width, frame.height)

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
        if columns > 1:
            # Stated explicitly, never left to the reader's default: InDesign's
            # is 12pt against Publisher's 2mm, which would widen every gap and
            # narrow every column. Publisher's column spacing is the space
            # between columns only, the same thing IDML calls the gutter, so
            # it does not overlap InsetSpacing above.
            preference["TextColumnGutter"] = fmt(frame.column_gap)
        ET.SubElement(element, "TextFramePreference", preference)
        self._emit_transparency(element, frame.style)

        # A chain's text belongs to the story, not to each frame that shows
        # it, so it is written once — at the head, the only link still
        # holding the paragraphs.
        if link is None or link.head:
            part_name = f"Stories/Story_{story_id}.xml"
            story_parts.append(part_name)
            self._parts[part_name] = self._story_part(story_id, frame.story)

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
            node = ET.SubElement(element, "Cell", attributes)
            paragraphs = cell.story.paragraphs or [model.Paragraph()]
            for position, block in enumerate(paragraphs):
                self._emit_paragraph(
                    node, block, last=(position == len(paragraphs) - 1)
                )
        return _serialise(root)

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
            wrap = ET.SubElement(
                rectangle,
                "TextWrapPreference",
                {
                    "Inverse": "false",
                    "ApplyToMasterPageOnly": "false",
                    "TextWrapSide": "BothSides",
                    "TextWrapMode": "BoundingBoxTextWrap",
                },
            )
            wrap_properties = ET.SubElement(wrap, "Properties")
            ET.SubElement(
                wrap_properties,
                "TextWrapOffset",
                {"Top": "0", "Left": "0", "Bottom": "0", "Right": "0"},
            )

        # On the frame rather than the Image inside it: Publisher shadows
        # the picture as placed, and a shadow on the content would sit
        # under the frame's own clipping.
        self._emit_transparency(rectangle, item.style)

        index = len(self.image_files) + 1
        filename = f"{self.image_dir_name}/image{index}{model.extension_for(item.mime_type)}"
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
                "LinkResourceURI": "file:" + quote(filename, safe=_URI_PATH_SAFE),
                "LinkResourceFormat": type_name,
                "StoredState": "Normal",
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

    def _story_part(self, story_id: str, story: model.Story) -> bytes:
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
            self._emit_paragraph(element, paragraph, last=(position == len(paragraphs) - 1))
        return _serialise(root)

    def _emit_paragraph(
        self, story: ET.Element, paragraph: model.Paragraph, last: bool
    ) -> None:
        attributes = {
            "AppliedParagraphStyle": NO_PARAGRAPH_STYLE,
            "Justification": _JUSTIFICATION.get(paragraph.align, "LeftAlign"),
        }
        if paragraph.space_before:
            attributes["SpaceBefore"] = fmt(paragraph.space_before)
        if paragraph.space_after:
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

        spans = paragraph.spans or [model.Span()]
        for span in spans:
            self._emit_span(range_element, span, _leading_for(paragraph, span))

        # IDML marks the end of a paragraph with an explicit break.
        if not last:
            ET.SubElement(range_element, "Br")

    def _emit_span(
        self, parent: ET.Element, span: model.Span, leading: Optional[float] = None
    ) -> None:
        attributes = {"AppliedCharacterStyle": NO_CHARACTER_STYLE}
        if span.size_pt:
            attributes["PointSize"] = fmt(span.size_pt)
        attributes["FillColor"] = self._color_ref(span.color, "Color/Black")
        if span.underline:
            attributes["Underline"] = "true"
        if span.strikethrough:
            attributes["StrikeThru"] = "true"
        if span.superscript:
            attributes["Position"] = "Superscript"
        elif span.subscript:
            attributes["Position"] = "Subscript"

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
        if span.bold or span.italic:
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


def _leading_for(paragraph: model.Paragraph, span: model.Span) -> Optional[float]:
    """The leading in points for one run, or None to leave it on Auto.

    Publisher's exact point spacing maps straight across. Its "spaces"
    figure is proportional, so it is resolved against the run's own type
    size -- 0.9 spaces of 10pt type is 0.9 x 1.2 x 10 = 10.8pt.
    """
    if paragraph.line_spacing_pt is not None:
        return paragraph.line_spacing_pt
    if paragraph.line_spacing_multiple is None:
        return None
    size = span.size_pt or DEFAULT_POINT_SIZE
    return paragraph.line_spacing_multiple * SINGLE_LINE_SPACING * size


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
