"""Check the angle we give a ramp against the one Publisher draws itself.

Written to settle a report that "sometimes gradients are rotated a bit".

`idml._ramp_angle` turns Publisher's angle into IDML's by adding a quarter
turn and nothing else, and its docstring says why that is as far as it
goes: "Every angle the corpus states is a half turn or none, so which way
a ramp between those turns is not something these files can settle." That
sentence is no longer true. `1336 kerkbode.pub` states 135 and -45 as well,
so the diagonal case *is* exercised, and it is the only case that can be
wrong while the rest of the corpus looks right -- which is what "sometimes"
means.

There is a ground truth for it that needs nothing installed. Publisher's
own PDF export of that file sits in `files/experiments`, and it states
each gradient as a PDF axial shading: a `/Coords [x0 y0 x1 y1]` giving the
ramp's axis in page points, and a `/Function` giving the colours along it.
Pattern space is the page's own default space, so those coordinates need
no interpreting -- they are Publisher saying, in points, which way the
ramp runs.

    python3 research/gradient_angle.py \
        "files/cgk/1336 kerkbode.pub" "files/experiments/1336 kerkbode.pdf"

Shapes are matched to shadings by colour, not by page: the PDF is imposed
for saddle stitch, so its sheets hold two non-adjacent pages each and
sheet numbers say nothing about page numbers. A ramp's colours are a good
enough handle, and the script says so when they are not unique.

**What this settles and what it does not.** It settles the *axis* a ramp
runs along, against Publisher rather than against libmspub -- and every
diagonal the corpus states lies on one axis (the file's 135 and -45 are
the same line, half a turn apart), so the mirrored diagonal is still
unmeasured here. A .pub with a diagonal ramp turned the other way would
settle that too, and nothing else will.
"""

from __future__ import annotations

import math
import re
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pubidml import convert, idml, model  # noqa: E402


# --- the PDF, far enough to read a shading -------------------------------

def _objects(data: bytes) -> dict:
    """Every object body, including those packed into object streams."""
    objs = {}
    for match in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", data):
        end = data.find(b"endobj", match.end())
        if end != -1:
            objs[int(match.group(1))] = data[match.end():end]

    for body in list(objs.values()):
        if b"/ObjStm" not in body:
            continue
        payload = _stream(body)
        if payload is None:
            continue
        count = int(re.search(rb"/N\s+(\d+)", body).group(1))
        first = int(re.search(rb"/First\s+(\d+)", body).group(1))
        header = payload[:first].split()
        for i in range(count):
            number, offset = int(header[2 * i]), int(header[2 * i + 1])
            following = (int(header[2 * i + 3]) if i + 1 < count
                         else len(payload) - first)
            objs[number] = payload[first + offset:first + following]
    return objs


def _stream(body: bytes):
    match = re.search(rb"stream\r?\n", body)
    if not match:
        return None
    payload = body[match.end():].rsplit(b"endstream", 1)[0]
    if b"/FlateDecode" not in body[:match.start()]:
        return payload
    try:
        # Publisher's streams carry trailing bytes past the deflate end.
        return zlib.decompressobj().decompress(payload)
    except zlib.error:
        return None


def _numbers(text: bytes):
    return [float(value) for value in re.findall(rb"-?[\d.]+", text)]


def _swatch(channels) -> str:
    if not channels:
        return "?"
    if len(channels) == 1:
        channels = channels * 3
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, round(v * 255))) for v in channels[:3]
    )


def _function_colors(objs: dict, number: int, depth: int = 0):
    """Every colour a function passes through, stitching functions flattened."""
    body = objs.get(number)
    if body is None or depth > 6:
        return []
    kind = re.search(rb"/FunctionType\s+(\d+)", body)
    if kind is None:
        return []
    if kind.group(1) == b"2":
        ends = []
        for key in (rb"/C0", rb"/C1"):
            found = re.search(key + rb"\s*\[([^\]]*)\]", body)
            ends.append(_numbers(found.group(1)) if found else None)
        return ends
    if kind.group(1) == b"3":
        listed = re.search(rb"/Functions\s*\[([^\]]*)\]", body, re.S)
        if listed is None:
            return []
        out = []
        for ref in re.findall(rb"(\d+)\s+\d+\s+R", listed.group(1)):
            out.extend(_function_colors(objs, int(ref), depth + 1))
        return out
    return []


def shadings(path: Path):
    """Every axial shading the PDF states: its axis and its colours."""
    objs = _objects(path.read_bytes())
    out = []
    for number in sorted(objs):
        body = objs[number]
        if b"/ShadingType 2" not in body:
            continue
        coords = re.search(rb"/Coords\s*\[([^\]]*)\]", body)
        if coords is None:
            continue
        x0, y0, x1, y1 = _numbers(coords.group(1))[:4]
        # /Function is a reference, or a one-element array holding one.
        function = re.search(rb"/Function\s*\[?\s*(\d+)\s+\d+\s+R", body)
        colours = (_function_colors(objs, int(function.group(1)))
                   if function else [])
        seen, chain = None, []
        for colour in colours:
            swatch = _swatch(colour)
            if swatch != seen:
                chain.append(swatch)
                seen = swatch
        out.append({
            "object": number,
            # PDF pages measure y upwards.
            "bearing": math.degrees(math.atan2(y1 - y0, x1 - x0)),
            "length": math.hypot(x1 - x0, y1 - y0),
            "colours": chain,
        })
    return out


# --- the two readings, side by side --------------------------------------

def ours(item) -> dict:
    """The axis `idml.py` writes, as a bearing with y measured upwards."""
    angle = idml._ramp_angle(item.style.gradient.angle)
    _start, length = idml._ramp_geometry(angle, item.width, item.height)
    # _ramp_geometry states the angle anticlockwise from left-to-right with
    # y increasing downwards, so the bearing is the angle itself once y is
    # turned back the way a PDF measures it.
    return {"angle": angle, "bearing": angle, "length": length}


def publishers_diagonal(width: float, height: float) -> float:
    """The bearing of a ramp whose bands lie along the shape's diagonal.

    What the measurement below shows Publisher doing: a diagonal shade is
    not a fixed 45 degrees on the page but perpendicular to the shape's own
    corner-to-corner line, so its angle follows the box's proportions. On a
    square the two agree, which is why this never showed up until a corpus
    with an 82 by 62 panel in it.
    """
    return math.degrees(math.atan2(width, height))


def main(pub: Path, pdf: Path) -> None:
    document = convert.parse_document(pub)
    found = shadings(pdf)

    print(f"{pub.name}: {len(document.pages)} pages")
    print(f"{pdf.name}: {len(found)} axial shadings\n")

    header = (f"{'page':>5} {'size':>14} {'pub':>7} {'ours':>7} "
              f"{'theirs':>7} {'out by':>7}  colours")
    print(header)
    print("-" * len(header))

    for index, page in enumerate(document.pages, 1):
        for item in model._walk(page.items):
            gradient = item.style.gradient
            if gradient is None:
                continue
            mine = ours(item)
            palette = {
                _swatch([c / 255.0 for c in stop.color])
                for stop in gradient.stops
            }
            matches = [
                shading for shading in found
                if palette.issubset(set(shading["colours"]))
            ]
            size = f"{item.width:.1f}x{item.height:.1f}"
            if not matches:
                print(f"{index:>5} {size:>14} {gradient.angle:>7.1f} "
                      f"{mine['bearing']:>7.1f} {'-':>7} {'-':>7}  "
                      f"no shading states these colours")
                continue
            # Several shapes in one file share a ramp's colours, so the
            # box settles which shading is which: a shading drawn for this
            # shape has to span it, and Publisher's span is the box
            # measured along the ramp, once or twice over.
            def spans(shading):
                across = abs(item.width * math.cos(math.radians(shading["bearing"]))) \
                    + abs(item.height * math.sin(math.radians(shading["bearing"])))
                ratio = shading["length"] / across if across else 0.0
                return min(abs(ratio - 1.0), abs(ratio - 2.0))

            matches.sort(key=spans)
            theirs = matches[0]
            if spans(theirs) > 0.02:
                print(f"{index:>5} {size:>14} {gradient.angle:>7.1f} "
                      f"{mine['bearing']:>7.1f} {'-':>7} {'-':>7}  "
                      f"{len(matches)} share these colours, none spans the box")
                continue
            # A ramp's axis is a line: half a turn either way is the same
            # line drawn from the other end, which the colour order settles
            # rather than the angle.
            gap = (theirs["bearing"] - mine["bearing"]) % 180.0
            gap = gap - 180.0 if gap > 90.0 else gap
            print(f"{index:>5} {size:>14} {gradient.angle:>7.1f} "
                  f"{mine['bearing']:>7.1f} {theirs['bearing']:>7.1f} "
                  f"{gap:>7.1f}  obj {theirs['object']}"
                  f"{'  <-- rotated' if abs(gap) > 0.5 else ''}")
            if abs(gap) > 0.5:
                predicted = publishers_diagonal(item.width, item.height)
                near = (theirs["bearing"] - predicted) % 180.0
                near = near - 180.0 if near > 90.0 else near
                print(f"{'':>5} {'':>14} bands along the shape's diagonal "
                      f"would be {predicted:.1f}, which is {near:+.1f} off")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
