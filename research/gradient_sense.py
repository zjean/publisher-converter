"""Check which *end* of a ramp each colour is at, against Publisher itself.

`research/gradient_angle.py` measures the axis a ramp runs along and says so
in as many words: it folds the comparison into half a turn, because "a ramp's
axis is a line: half a turn either way is the same line drawn from the other
end". That is exactly the half turn this script measures, and the one a
reader sees: a heading band shaded navy at the top into white at the foot,
where Publisher shades it white at the top into navy at the foot, is a
perfect match to the other script and wrong on the page.

    python3 research/gradient_sense.py \
        "files/cgk/1337 kerkbode.pub" files/pdf/1337_kerkbode.pdf

Publisher's own PDF export is the ground truth, and it needs nothing
installed. Each gradient is a `/PatternType 2` shading whose `/Coords` give
its axis in page points and whose `/Function` gives the colours along it.
The shading is not the ramp, though, and that is the whole difficulty: over
a band Publisher draws the ramp two or three times across an axis centred on
the box, so only the middle of it is the shape. So this walks the *box*,
asks Publisher what colour is at each step, and asks our own stop list the
same -- once as written and once reversed. The reversed reading winning is
the ramp being upside down, and by how much says whether the match is real.

What it settles is the sense, on the shapes it can match. A shading is matched
to a shape by two things together: its axis has to span the box, and every
colour our ramp holds has to appear along it. Neither on its own is enough
-- a newsletter repeats one ramp over eight bands, and a sheet imposed for
saddle stitch puts two unrelated pages at the same coordinates. Where
nothing matches it says so rather than guessing, and where the two readings
score alike it says that too: a symmetrical ramp has no sense to measure.
"""

from __future__ import annotations

import math
import re
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pubidml import convert, idml, model, pubfile  # noqa: E402


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


def _sample(objs: dict, number: int, t: float, depth: int = 0):
    """The colour a PDF function passes through at `t`, in 0..1."""
    body = objs.get(number)
    if body is None or depth > 6:
        return None
    kind = re.search(rb"/FunctionType\s+(\d+)", body)
    if kind is None:
        return None
    domain = re.search(rb"/Domain\s*\[([^\]]*)\]", body)
    low, high = (_numbers(domain.group(1))[:2] if domain else (0.0, 1.0))

    if kind.group(1) == b"2":
        ends = []
        for key in (rb"/C0", rb"/C1"):
            found = re.search(key + rb"\s*\[([^\]]*)\]", body)
            ends.append(_numbers(found.group(1)) if found else [0.0])
        exponent = re.search(rb"/N\s+([\d.]+)", body)
        power = float(exponent.group(1)) if exponent else 1.0
        x = 0.0 if high == low else (t - low) / (high - low)
        x = max(0.0, min(1.0, x)) ** power
        return [a + x * (b - a) for a, b in zip(*ends)]

    if kind.group(1) == b"3":
        listed = re.search(rb"/Functions\s*\[([^\]]*)\]", body, re.S)
        if listed is None:
            return None
        parts = [int(r) for r in re.findall(rb"(\d+)\s+\d+\s+R", listed.group(1))]
        bounds = re.search(rb"/Bounds\s*\[([^\]]*)\]", body, re.S)
        edges = [low] + (_numbers(bounds.group(1)) if bounds else []) + [high]
        encode = re.search(rb"/Encode\s*\[([^\]]*)\]", body, re.S)
        mapping = _numbers(encode.group(1)) if encode else []
        i = 0
        while i < len(parts) - 1 and t >= edges[i + 1]:
            i += 1
        lo, hi = edges[i], edges[i + 1]
        e0, e1 = (mapping[2 * i], mapping[2 * i + 1]) if len(mapping) > 2 * i + 1 else (0.0, 1.0)
        x = e0 if hi == lo else e0 + (t - lo) / (hi - lo) * (e1 - e0)
        return _sample(objs, parts[i], x, depth + 1)

    if kind.group(1) == b"0":
        # A sampled ramp, which is how Publisher states the ones it draws
        # for WordArt.
        payload = _stream(body)
        size = re.search(rb"/Size\s*\[\s*(\d+)", body)
        if payload is None or size is None:
            return None
        count = int(size.group(1))
        width = len(payload) // count if count else 0
        if width < 1:
            return None
        i = max(0, min(count - 1, int(round(t * (count - 1)))))
        return [payload[i * width + c] / 255.0 for c in range(min(3, width))]
    return None


def _rgb(channels):
    """A function's output as 8-bit RGB, whatever space it states."""
    if not channels:
        return None
    if len(channels) == 1:
        channels = channels * 3
    if len(channels) >= 4:
        cyan, magenta, yellow, black = channels[:4]
        return tuple(
            max(0, min(255, round(255 * (1 - v) * (1 - black))))
            for v in (cyan, magenta, yellow)
        )
    return tuple(max(0, min(255, round(v * 255))) for v in channels[:3])


def shadings(path: Path):
    """Every axial shading the PDF states, with the page it is used on.

    Publisher writes each one as a shading pattern in the page's own
    coordinate space, so its `/Coords` need no interpreting.
    """
    objs = _objects(path.read_bytes())
    order, root = [], None
    for number, body in objs.items():
        if re.search(rb"/Type\s*/Pages\b", body) and b"/Parent" not in body:
            root = number

    def walk(number: int, depth: int = 0) -> None:
        body = objs.get(number)
        if body is None or depth > 8:
            return
        if re.search(rb"/Type\s*/Page\b", body) and not re.search(rb"/Type\s*/Pages\b", body):
            order.append(number)
            return
        kids = re.search(rb"/Kids\s*\[([^\]]*)\]", body, re.S)
        if kids:
            for ref in re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1)):
                walk(int(ref), depth + 1)

    if root is not None:
        walk(root)

    sheet = (0.0, 0.0)
    found = []
    for pageno, pobj in enumerate(order, 1):
        body = objs[pobj]
        box = re.search(rb"/MediaBox\s*\[([^\]]*)\]", body)
        if box:
            values = _numbers(box.group(1))[:4]
            sheet = (values[2] - values[0], values[3] - values[1])
        patterns = re.search(rb"/Pattern\s*<<(.*?)>>", body, re.S)
        if not patterns:
            continue
        for number in {int(n) for _, n in re.findall(rb"/(\w+)\s+(\d+)\s+\d+\s+R", patterns.group(1))}:
            pattern = objs.get(number)
            if pattern is None or b"/ShadingType 2" not in pattern:
                continue
            coords = re.search(rb"/Coords\s*\[([^\]]*)\]", pattern)
            function = re.search(rb"/Function\s+(\d+)\s+\d+\s+R", pattern)
            if not (coords and function):
                continue
            x0, y0, x1, y1 = _numbers(coords.group(1))[:4]
            found.append({
                "page": pageno, "object": number,
                "axis": (x0, y0, x1, y1), "function": int(function.group(1)),
            })
    return objs, found, sheet


# --- the two readings, laid over the same box ----------------------------

def our_colour(stops, u: float):
    """The colour our stop list holds at `u`, a fraction along the ramp."""
    places = [stop.location / 100.0 for stop in stops]
    for i in range(len(stops) - 1):
        if places[i] <= u <= places[i + 1]:
            span = places[i + 1] - places[i]
            f = 0.0 if span <= 0 else (u - places[i]) / span
            return tuple(round(a + f * (b - a))
                         for a, b in zip(stops[i].color, stops[i + 1].color))
    return stops[0].color if u < places[0] else stops[-1].color


def page_origins(sheet, page_width: float, page_height: float):
    """Where a page's own origin sits on the sheet the PDF holds.

    A booklet is imposed two pages to a sheet, centred, so a shape's place
    on the page reaches the sheet through one of two offsets.
    """
    width, height = sheet
    down = (height - page_height) / 2.0
    if width >= 2 * page_width - 1.0:
        across = (width - 2 * page_width) / 2.0
        return [(across, down), (across + page_width, down)]
    return [((width - page_width) / 2.0, down)]


def ramps_on(page):
    """Every ramp the writer will be handed on this page, with its item.

    A shape carries its ramp on its style; a recovered WordArt headline
    carries it on the text run instead, because what libmspub reported
    describes the glyphs rather than a box behind them. Both are ramps the
    reader draws, and only the first used to be measured.
    """
    for item in model._walk(page.items):
        if item.style.gradient is not None:
            yield item, item.style.gradient
        story = getattr(item, "story", None)
        if story is None:
            continue
        for paragraph in story.paragraphs:
            for span in paragraph.spans:
                if span.gradient is not None:
                    yield item, span.gradient


def main(pub: Path, pdf: Path) -> None:
    objs, found, sheet = shadings(pdf)
    document = convert.parse_document(pub)
    structure = pubfile.read_structure(pub)
    # The ramps are what the writer will be handed, so the passes that
    # settle them have to have run.
    convert._restore_gradient_ramps(document, structure)
    convert._restore_floored_turns(document, structure)
    # And the pass that turns a WordArt band into a frame carrying the
    # ramp on its text run. Without it the headlines' ramps are invisible
    # here -- which is how the masthead ribbon's turn came to be counted
    # twice with both of these scripts calling every ramp a match.
    convert._recover_wordart(document, structure)

    print(f"{pub.name}: {len(document.pages)} pages")
    print(f"{pdf.name}: {len(found)} shadings on {sheet[0]:.0f} x {sheet[1]:.0f}pt sheets\n")

    header = (f"{'page':>5} {'size':>13} {'flip':>5} {'turn':>9} {'ours':>8} "
              f"{'as written':>11} {'reversed':>9}  verdict")
    print(header)
    print("-" * len(header))

    upside_down = 0
    for pageno, page in enumerate(document.pages, 1):
        origins = page_origins(sheet, page.width, page.height)
        for item, ramp in ramps_on(page):
            stops = idml._spanning_stops(ramp.stops)
            placed, _start, _length = idml._ramp_placement(
                ramp, item.width, item.height
            )
            # The item's own turn is applied by its ItemTransform, so the
            # bearing on the page is what the reader draws once both have
            # gone on. A polygon carries no turn and this is the placed
            # angle itself; a recovered headline carries the shape's.
            angle = model._fold_angle(placed - item.rotation)
            stated = structure.gradient_for(
                item.x + item.width / 2 - page.width / 2,
                item.y + item.height / 2 - page.height / 2,
                item.width, item.height,
            ) if structure is not None else None
            size = f"{item.width:6.1f}x{item.height:5.1f}"
            flip = "-" if stated is None else ("V" if stated.flipped_v else "") + \
                   ("H" if stated.flipped_h else "") or "."
            turn = 0.0 if stated is None else stated.rotation

            # Walk our own ramp across the box, and for each shading that
            # could be this shape's ask Publisher what it draws at the
            # same place.
            ux, uy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
            # The distance the writer actually runs the ramp over, which
            # `_ramp_placement` above already worked out: the box the file
            # states rather than the item's own, where the two differ --
            # an outline, or a turn.
            half = _length / 2.0
            steps = 21
            best = None
            for across, down in origins:
                for shading in found:
                    x0 = shading["axis"][0] - across
                    y0 = shading["axis"][1] - down
                    x1 = shading["axis"][2] - across
                    y1 = shading["axis"][3] - down
                    length = math.hypot(x1 - x0, y1 - y0)
                    if length < 1.0:
                        continue
                    places, theirs = [], []
                    for i in range(steps):
                        u = i / (steps - 1)
                        x = item.x + item.width / 2.0 + (2 * u - 1) * half * ux
                        y = (page.height - (item.y + item.height / 2.0)) \
                            + (2 * u - 1) * half * uy
                        t = ((x - x0) * (x1 - x0) + (y - y0) * (y1 - y0)) / (length * length)
                        places.append(t)
                        theirs.append(_rgb(_sample(objs, shading["function"],
                                                   max(0.0, min(1.0, t)))))
                    # The ramp has to run over the box rather than past it,
                    # and it has to be made of the colours ours is made of.
                    if min(places) < -0.05 or max(places) > 1.05:
                        continue
                    drawn = [c for c in theirs if c is not None]
                    if len(drawn) < steps:
                        continue
                    # The colour check is over the whole shading rather
                    # than the part of it the box covers: Publisher draws a
                    # band's ramp two or three times across an axis longer
                    # than the band, so both end colours lie outside it.
                    # The ends are what is checked, and only the ends -- a
                    # waypoint sits between two samples and a shading drawn
                    # at 60% opacity states it a shade off.
                    whole = [
                        _rgb(_sample(objs, shading["function"], i / 200.0))
                        for i in range(201)
                    ]
                    if not all(
                        any(c is not None
                            and max(abs(a - b) for a, b in zip(end.color, c)) <= 4
                            for c in whole)
                        for end in (stops[0], stops[-1])
                    ):
                        continue
                    written = sum(
                        sum(abs(a - b) for a, b in zip(our_colour(stops, i / (steps - 1)), c))
                        for i, c in enumerate(drawn)
                    ) / (steps * 3)
                    backwards = sum(
                        sum(abs(a - b) for a, b in zip(our_colour(stops, 1 - i / (steps - 1)), c))
                        for i, c in enumerate(drawn)
                    ) / (steps * 3)
                    if best is None or min(written, backwards) < min(best[1], best[2]):
                        best = (shading, written, backwards)
            if best is None:
                print(f"{pageno:>5} {size:>13} {flip:>5} {turn:>9.3f} {angle:>8.2f} "
                      f"{'-':>11} {'-':>9}  no shading over this box holds these colours")
                continue

            shading, written, backwards = best
            if abs(written - backwards) < 1.0:
                verdict = "either way (a symmetrical ramp)"
            elif backwards < written:
                verdict = "UPSIDE DOWN"
                upside_down += 1
            else:
                verdict = "ok"
            print(f"{pageno:>5} {size:>13} {flip:>5} {turn:>9.3f} {angle:>8.2f} "
                  f"{written:>11.1f} {backwards:>9.1f}  {verdict} (obj {shading['object']})")

    print()
    if upside_down:
        print(f"{upside_down} ramp(s) run the wrong way. The two columns are the "
              f"average channel error over the box, so a small one beside a "
              f"large one is the reading and not a coincidence.")
    else:
        print("Every ramp matched runs the way Publisher draws it.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
