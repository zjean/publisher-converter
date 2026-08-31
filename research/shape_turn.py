"""Check the turn we draw a shape at against the one Publisher draws itself.

Written to settle a report that a band in `1337 kerkbode.pub` came out
"oriented weirdly": its two top corners sat 6.5pt apart in y where both
should have sat level, and so did its two bottom ones.

That band is a rectangle turned a degree, and the degree is not the
file's. Publisher states a shape's turn in Escher property 0x0004 as
16.16 fixed point, and the kerkbode section bands all state the same
value -- `0xfde3f536`, which is -540.042145 degrees, a turn and a half
and a hair more. libmspub keeps the whole degrees of that and drops the
fraction, and it keeps them by flooring, which is what taking the top
half of the word amounts to: -541 rather than -540. A degree of error
either way is 6.5pt of drop across an A5 page.

There is a ground truth for it that needs nothing installed. Publisher's
own PDF exports sit in `files/experiments`, and they draw each band as a
four-point path in the page's own space, so its corners are Publisher
saying in points which way the band lies.

    python3 research/shape_turn.py \
        "files/cgk/1336 kerkbode.pub" "files/experiments/1336 kerkbode.pdf"

Bands are matched to paths by size, not by page: the PDF is imposed for
saddle stitch, so its sheets hold two non-adjacent pages each and sheet
numbers say nothing about page numbers. A band's two side lengths are a
good enough handle at this size, and the script says so when they are not.

**What this settles and what it does not.** It settles that the fraction
the file states is the one Publisher draws: across the sixteen bands the
two exports hold, Publisher's corners agree with the file to a thousandth
of a degree and are a whole degree away from libmspub. Which whole degree
libmspub landed on says the rest -- it draws all sixteen at exactly 181,
which is what flooring -540.042145 gives and truncating does not.

Every fractional turn the corpus states is negative, so flooring is told
apart from truncating here but not from rounding towards negative. A .pub
turning a shape a fraction of a degree the other way would settle that
too, and nothing else will.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pubidml import pubfile  # noqa: E402
from research.gradient_angle import _objects, _stream  # noqa: E402


# --- the PDF, far enough to read a four-point path -----------------------

def quads(pdf: Path):
    """Every four-point path the PDF draws, as corners in page points."""
    found = []
    number = re.compile(rb"-?\d*\.?\d+")
    run = re.compile(rb"(?:-?\d*\.?\d+ ){2}m(?:\s+(?:-?\d*\.?\d+ ){2}l)+")
    for body in _objects(pdf.read_bytes()).values():
        payload = _stream(body)
        if payload is None:
            continue
        for match in run.finditer(payload):
            values = [float(v) for v in number.findall(match.group(0))]
            points = list(zip(values[0::2], values[1::2]))
            if len(points) == 4:
                # PDF pages measure y upwards; the rest of this measures
                # it down, the way both libmspub and Publisher's own
                # property state a turn.
                found.append([(x, -y) for x, y in points])
    return found


def sides(points):
    """The two side lengths of a quadrilateral, longer one first."""
    a = math.dist(points[0], points[1])
    b = math.dist(points[1], points[2])
    return (max(a, b), min(a, b))


def lean(points) -> float:
    """How far off square a quadrilateral lies, in degrees.

    Read off the longer pair of sides and folded onto the quarter turn,
    so a band's lean is the same number whichever corner it starts from
    and whichever way round its points run.
    """
    edges = [
        (points[1][0] - points[0][0], points[1][1] - points[0][1]),
        (points[2][0] - points[1][0], points[2][1] - points[1][1]),
    ]
    dx, dy = max(edges, key=lambda e: math.hypot(*e))
    return (math.degrees(math.atan2(dy, dx)) + 45.0) % 90.0 - 45.0


# --- the file's own word on the same shapes ------------------------------

def stated_turns(pub: Path):
    """Every shape the file states a turn and a gradient for.

    The gradient shapes are the reachable ones: `ShapeGradient` is the
    only record `pubfile` builds that carries a shape's turn next to the
    box it turns, which is also why `convert._restore_floored_turns` can
    only put the fraction back on those.
    """
    structure = pubfile.read_structure(pub)
    return [
        (found.rotation, found.width, found.height)
        for found in (structure.gradients if structure else [])
    ]


def main(pub: Path, pdf: Path) -> None:
    drawn = quads(pdf)
    print(f"{pub.name}: gradient shapes the file states a turn for")
    print(f"{pdf.name}: {len(drawn)} four-point paths\n")

    header = (f"{'size':>16} {'stated':>12} {'lean':>8} "
              f"{'Publisher':>10} {'libmspub':>9}  verdict")
    print(header)
    print("-" * len(header))

    for turn, width, height in stated_turns(pub):
        if turn == math.floor(turn):
            continue
        near = [q for q in drawn
                if abs(sides(q)[0] - max(width, height)) < 0.5
                and abs(sides(q)[1] - min(width, height)) < 0.5]
        seen = {round(lean(q), 3) for q in near}
        size = f"{width:.2f}x{height:.2f}"
        # A shape is drawn at the turn it states, the other way about --
        # the corners of a band stating -540.042145 lean +0.042145 -- so
        # both readings are negated before the same fold `lean` uses.
        ours = (-turn + 45.0) % 90.0 - 45.0
        theirs = (-math.floor(turn) + 45.0) % 90.0 - 45.0
        if not near:
            print(f"{size:>16} {turn:12.6f} {ours:8.4f} "
                  f"{'not drawn':>10} {theirs:9.4f}  -")
            continue
        if len(seen) > 1:
            print(f"{size:>16} {turn:12.6f} {ours:8.4f} "
                  f"{'ambiguous':>10} {theirs:9.4f}  {len(near)} paths differ")
            continue
        measured = seen.pop()
        verdict = "file" if abs(measured - ours) < abs(measured - theirs) else "libmspub"
        print(f"{size:>16} {turn:12.6f} {ours:8.4f} "
              f"{measured:10.4f} {theirs:9.4f}  {verdict}")

    print("\n'lean' is how far off square the turn the file states leaves "
          "the shape,\nand 'libmspub' the same for the whole degree it "
          "floors that turn to.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
