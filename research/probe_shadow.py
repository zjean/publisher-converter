"""Build a probe .idml that exercises drop shadows on shapes.

libmspub reports a Publisher shadow completely -- `draw:shadow`,
`draw:shadow-color`, both offsets and `draw:shadow-opacity` -- and the
converter used to drop all of it, flattening 15 shapes in each newsletter.
It now writes an IDML `DropShadowSetting`. Whether that survives the trip
is a different question, and only Affinity can answer it.

Three things needed answering:

  1. Does Affinity honour `DropShadowSetting` at all, or ignore it?
  2. Is the offset read from `XOffset`/`YOffset`, or from the `Angle` and
     `Distance` that IDML also carries? The two are written to agree, so a
     shadow in the wrong place means our angle convention is inverted.
  3. Does `Opacity` arrive, and does `Size="0"` really mean no blur --
     Publisher's shadow is a hard offset copy, and a reader that softens
     it by default would look wrong on every one of those 15 shapes.

Every rectangle is the same size on a plain background and says what it
should look like. This drives `idml.IdmlWriter` itself, not a hand-built
package, so what Affinity opens is what the converter would really write.

    python3 research/probe_shadow.py

Writes converted/probe/probe-shadow.idml. Open it and read off:

  - A flat, B shadowed down-right, C the same shadow at half strength,
    D shadowed up-left            -> shadows work end to end.
  - Every rectangle flat          -> DropShadowSetting is ignored; the
                                     README limitation stands and a
                                     warning is the honest alternative.
  - B and D shadowed the same way -> the offsets are being ignored in
                                     favour of Angle/Distance, or a
                                     global light angle is overriding
                                     them.
  - D shadowed down-right         -> our angle is inverted: it must be
                                     the direction the light comes from,
                                     not the way the shadow falls.
  - C as solid as B               -> Opacity is ignored.
  - Soft, blurred edges           -> Size="0" is not being honoured;
                                     Publisher's shadow has no blur.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402

# Far larger than the 2-3pt Publisher actually uses: the question is which
# direction the shadow lands in, and a 2pt offset is hard to judge by eye.
OFFSET_PT = 18.0

GREY = (0xC0, 0xC0, 0xC0)   # the colour 12 of the 15 real shadows use
NAVY = (0x00, 0x33, 0x66)   # and the colour of the other three

ROWS = [
    ("A - no shadow (control)", None),
    (
        "B - grey shadow, down and right",
        model.Shadow(color=GREY, offset_x=OFFSET_PT, offset_y=OFFSET_PT),
    ),
    (
        "C - same shadow at 50% opacity",
        model.Shadow(
            color=GREY, offset_x=OFFSET_PT, offset_y=OFFSET_PT, opacity=0.5
        ),
    ),
    (
        "D - navy shadow, up and left",
        model.Shadow(color=NAVY, offset_x=-OFFSET_PT, offset_y=-OFFSET_PT),
    ),
]


def probe_document() -> model.Document:
    document = model.Document(title="Drop shadow probe")
    page = model.Page(width=612.0, height=792.0)

    for index, (label, shadow) in enumerate(ROWS):
        top = 108.0 + index * 162.0
        page.items.append(
            model.Rectangle(
                x=144.0, y=top, width=216.0, height=72.0,
                style=model.GraphicStyle(
                    fill=(0xFF, 0xFF, 0xFF),
                    stroke=(0x00, 0x00, 0x00),
                    stroke_width=1.0,
                    shadow=shadow,
                ),
            )
        )
        caption = model.TextFrame(x=144.0, y=top + 82.0, width=324.0, height=18.0)
        paragraph = model.Paragraph()
        paragraph.spans.append(model.Span(text=label, size_pt=10.0, font="Helvetica"))
        caption.story.paragraphs.append(paragraph)
        page.items.append(caption)

    document.pages.append(page)
    return document


def build(destination: Path) -> None:
    document = probe_document()
    writer = idml.IdmlWriter(document, image_dir_name="probe_images")
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer.write(destination)

    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        for name in names:
            if name.endswith(".xml"):
                ET.fromstring(archive.read(name))
        spread = next(n for n in names if n.startswith("Spreads/"))
        written = [
            (
                node.get("XOffset"), node.get("YOffset"),
                node.get("Angle"), node.get("Distance"),
                node.get("Opacity"), node.get("EffectColor"),
            )
            for node in ET.fromstring(archive.read(spread)).iter("DropShadowSetting")
        ]

    print(f"wrote {destination}")
    print("  rect      x       y   angle  distance  opacity  colour")
    for label, row in zip("BCD", written):
        x, y, angle, distance, opacity, color = row
        print(f"    {label}  {x:>6} {y:>7} {angle:>7} {distance:>9} {opacity:>8}  {color}")
    print("  open it and report whether A/B/C/D differ as labelled")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-shadow.idml"))
