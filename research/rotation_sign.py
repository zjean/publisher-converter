"""Check our rotation sign against the outline libmspub draws for a shape.

Written to settle actions.md §4, which asked whether a positive angle
turns the same way Publisher turns it and proposed answering it by
rendering the file in LibreOffice and comparing by eye.

There is a stricter answer that needs nothing installed. libmspub reports
a rotated shape **twice**: once as `librevenge:rotate` on the object, and
once as a `drawPolygon` giving the shape's outline in absolute page
coordinates, which libmspub computes from that property itself. So the
polygon is libmspub saying how it reads its own field, and the question
becomes arithmetic: does the `ItemTransform` that `idml.py` writes put
the frame's corners on that outline?

Run it over the corpus:

    python3 research/rotation_sign.py files

Each rotated object prints the worst corner error two ways -- as written,
and with the sign negated -- so a file too symmetric to tell the signs
apart is visible as one where both columns are small.

**What this does and does not settle.** It settles us against libmspub,
which is also all the LibreOffice comparison could have settled:
LibreOffice drives the same library. Whether *libmspub* matches
*Publisher* is a separate question and still needs Publisher -- it is one
line of the checklist, since any .pub exported to PDF from Publisher
answers it.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pubidml import units  # noqa: E402

PUBDUMP = REPO / "bin" / "pubdump"


def corners(rotation, centre_x, centre_y, width, height):
    """Where idml.py's ItemTransform puts the corners of a centred box.

    The same arithmetic as `_matrix` followed by IDML's own
    `x' = a*x + c*y + tx`, kept here rather than imported so the probe
    checks the convention rather than restating it.
    """
    radians = math.radians(rotation)
    cos, sin = math.cos(radians), math.sin(radians)
    return [
        (cos * x - sin * y + centre_x, sin * x + cos * y + centre_y)
        for x, y in (
            (-width / 2, -height / 2), (width / 2, -height / 2),
            (width / 2, height / 2), (-width / 2, height / 2),
        )
    ]


def worst_corner_error(placed, outline):
    """How far the furthest corner is from the outline libmspub drew."""
    return max(
        min(math.dist(corner, vertex) for vertex in outline)
        for corner in placed
    )


def outlines(outline, width, height, tolerance: float = 1.0) -> bool:
    """True when this polygon is the outline of a `width` x `height` box.

    The polygon nearest an object in the stream is not always its own: the
    `Blank Note Card` sheet draws a small logo box immediately before the
    rotated credit block, and pairing those two reported a 386pt error on a
    file that is laid out correctly (checked against LibreOffice, which
    renders the credits left and the photographs right, exactly as
    libmspub's numbers say). So the polygon has to earn the pairing by
    measuring the same box, whatever angle it is turned to.
    """
    if len(outline) < 4:
        return False
    sides = sorted((
        math.dist(outline[0], outline[1]),
        math.dist(outline[1], outline[2]),
    ))
    return all(
        abs(side - expected) <= tolerance
        for side, expected in zip(sides, sorted((width, height)))
    )


def check(source: Path):
    """One row per rotated object libmspub reports an outline for."""
    result = subprocess.run(
        [str(PUBDUMP), str(source)], capture_output=True
    )
    page = outline = None
    rows = []
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        props = entry.get("p", {})
        name = entry.get("e")
        if name == "startPage":
            page = (
                units.to_points(props.get("svg:width")),
                units.to_points(props.get("svg:height")),
            )
        elif name == "drawPolygon":
            points = props.get("svg:points") or []
            outline = [
                (units.to_points(p.get("svg:x")), units.to_points(p.get("svg:y")))
                for p in points
            ][:4]
        elif name in ("startTextObject", "drawGraphicObject"):
            rotation = units.to_float(props.get("librevenge:rotate"), 0.0) or 0.0
            if not rotation or not page or not outline or len(outline) < 4:
                continue
            width = units.to_points(props.get("svg:width"))
            height = units.to_points(props.get("svg:height"))
            x = units.to_points(props.get("svg:x"))
            y = units.to_points(props.get("svg:y"))
            if None in (width, height, x, y):
                continue
            if not outlines(outline, width, height):
                continue
            # Page coordinates, which is what the polygon is stated in.
            centre = (x + width / 2, y + height / 2)
            rows.append((
                rotation, width, height,
                worst_corner_error(
                    corners(rotation, *centre, width, height), outline
                ),
                worst_corner_error(
                    corners(-rotation, *centre, width, height), outline
                ),
            ))
    return rows


def main(argv):
    root = Path(argv[1]) if len(argv) > 1 else REPO / "files"
    sources = sorted(root.rglob("*.pub")) if root.is_dir() else [root]

    print(f"{'file':<38} {'angle':>7} {'size (pt)':>17} "
          f"{'as written':>11} {'negated':>9}")
    for source in sources:
        rows = check(source)
        if not rows:
            print(f"{source.name[:38]:<38} {'-':>7} "
                  f"{'no rotated object':>17}")
            continue
        for rotation, width, height, written, negated in rows:
            print(f"{source.name[:38]:<38} {rotation:>+7.1f} "
                  f"{width:>7.1f} x{height:>8.1f} "
                  f"{written:>10.3f}pt {negated:>8.1f}pt")

    print(
        "\nA sign that is right reads near zero as written and large "
        "negated.\nBoth columns small means the shape is too symmetric to "
        "tell them apart."
    )


if __name__ == "__main__":
    main(sys.argv)
