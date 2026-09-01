"""Measure the stretch Publisher gives a WordArt headline, against its own PDF.

`_wordart_fit` works a headline's point size back from the band's height
and its horizontal scale back from the band's width. The height half is
checked: the README records the dropped initial inking 39.7pt of its 40.1pt
band. The width half never was, and it is wrong.

Publisher's own PDF export draws WordArt as vector outlines rather than as
text -- neither Pristina nor Monotype Corsiva is embedded in it -- and that
turns out to be better than text would be. An outline can be matched
against the font's own outline, and the affine transform between the two is
Publisher saying, in points, exactly what x and y sizes it drew the glyphs
at. There is nothing left to infer: the fit below closes to 0.017pt across
41 points.

    python3 research/wordart_stretch.py \
        "files/cgk/1336 kerkbode.pub" "files/experiments/1336 kerkbode.pdf"

What it said on the corpus's two dropped initials, when it was written:

    file  glyph  y-size drawn  x-size drawn  stretch drawn  stretch written
    1336  D           57.857        55.355          95.68%         121.20%
    1338  L           55.775        54.102          97.00%         151.73%

The point size is right to within 0.25%. The stretch was 27% and 57% too
wide, and Publisher draws these two at essentially no stretch at all --
so `_wordart_fit` now states none for a headline of one glyph, and the
last column of this script's output reads `-` for both. Rerun it after
touching the fit: it is the only ground truth there is for the width.

**Why.** Both are set in Pristina, which ships no italic face, and both
shapes ask for italic -- so Publisher slants them itself, by 13.2 and 13.4
degrees (the shear the same fit recovers). The band a WordArt shape states
is the bounding box of the slanted text, so a slice of its width is slant
overhang and not glyph width at all. libmspub says so too, in the guides it
reports for the shape: they are a parallelogram, two equal edges offset
horizontally, and `band width = guide length + offset` on every headline in
the corpus. The offset is zero on the one headline that is not italic.

Fitting the glyph advances to the whole band therefore counts the overhang
as room for glyphs. On a long headline the overhang is a small part of a
wide band and the error is a few percent; on a single dropped glyph it is a
fifth of the band, and the letter comes out a quarter too wide.

**What this does not settle.** Subtracting the overhang the fit measures
(9.5pt of a 44.9pt band) predicts the D to within 0.25% and the L not at
all -- 119% against the 97% drawn. So the overhang is the mechanism and not
yet the formula, and one more measured case is what would settle it. The
long headlines cannot supply it from this machine: the Monotype Corsiva
installed here is a different cut from the one Publisher drew with (34
outline points against 49 for a capital M), so only the Pristina shapes can
be measured at all.
"""

from __future__ import annotations

import math
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pubidml import convert, fontmetrics, pubfile  # noqa: E402


# --- the PDF, far enough to read one glyph's outline ---------------------

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


def _stream(body: bytes) -> Optional[bytes]:
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


def _pages(objs: dict) -> List[bytes]:
    return [body for _n, body in sorted(objs.items())
            if re.search(rb"/Type\s*/Page\b", body)]


def _content(objs: dict, page: bytes) -> bytes:
    one = re.search(rb"/Contents\s+(\d+)\s+\d+\s+R", page)
    if one:
        return _stream(objs.get(int(one.group(1)), b"")) or b""
    listed = re.search(rb"/Contents\s*\[([^\]]*)\]", page, re.S)
    if listed:
        return b"".join(
            _stream(objs.get(int(ref), b"")) or b""
            for ref in re.findall(rb"(\d+)\s+\d+\s+R", listed.group(1))
        )
    return b""


_TOKEN = re.compile(
    rb"""\[(?:[^\[\]\\]|\\.)*\]|\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>"""
    rb"""|/[^\s/\[\]<>(){}]+|-?[\d.]+|[A-Za-z'"*]+""",
    re.S,
)

_PAINT = ("f", "F", "f*", "B", "B*", "b", "b*", "S", "s", "n")


def _compose(a: List[float], b: List[float]) -> List[float]:
    return [
        a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5],
    ]


def _place(m: List[float], x: float, y: float) -> Tuple[float, float]:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def painted_paths(stream: bytes) -> List[List[Tuple[str, float, float]]]:
    """Every painted path, as its points in page space.

    Each point is tagged 'on' for a point the curve passes through and
    'c1'/'c2' for a Bezier control, because only the on-curve points can be
    matched against a TrueType outline: the quadratics the font states have
    been converted to cubics here and their controls have moved.
    """
    ctm: List[float] = [1, 0, 0, 1, 0, 0]
    saved: List[List[float]] = []
    stack: List = []
    points: List[Tuple[str, float, float]] = []
    out = []
    for match in _TOKEN.finditer(stream):
        token = match.group(0)
        if token[:1] in b"-0123456789.":
            stack.append(float(token))
            continue
        if token[:1] in b"/([<":
            stack.append(token)
            continue
        op = token.decode("latin1")
        if op == "cm" and len(stack) >= 6:
            ctm = _compose([float(v) for v in stack[-6:]], ctm)
        elif op == "q":
            saved.append(list(ctm))
        elif op == "Q":
            if saved:
                ctm = saved.pop()
        elif op in ("m", "l") and len(stack) >= 2:
            points.append(("on",) + _place(ctm, stack[-2], stack[-1]))
        elif op == "c" and len(stack) >= 6:
            points.append(("c1",) + _place(ctm, stack[-6], stack[-5]))
            points.append(("c2",) + _place(ctm, stack[-4], stack[-3]))
            points.append(("on",) + _place(ctm, stack[-2], stack[-1]))
        elif op in _PAINT:
            if points:
                out.append(points)
            points = []
        stack = []
    return out


# --- the font, far enough to read the same outline ----------------------

def outline(face: "fontmetrics.Face", glyph: int):
    """One simple glyph's contours as (x, y, on_curve) in font units."""
    loca = face.tables["loca"][0]
    if face.long_loca:
        start, end = struct.unpack_from(">II", face.buf, loca + 4 * glyph)
    else:
        short_start, short_end = struct.unpack_from(">HH", face.buf,
                                                    loca + 2 * glyph)
        start, end = short_start * 2, short_end * 2
    if end <= start:
        return []
    at = face.tables["glyf"][0] + start
    count = struct.unpack_from(">h", face.buf, at)[0]
    if count <= 0:
        # A composite glyph would need its components followed; no headline
        # in the corpus is set in one.
        return []
    ends = list(struct.unpack_from(f">{count}H", face.buf, at + 10))
    total = ends[-1] + 1
    cursor = at + 10 + 2 * count
    cursor += 2 + struct.unpack_from(">H", face.buf, cursor)[0]

    flags: List[int] = []
    while len(flags) < total:
        flag = face.buf[cursor]
        cursor += 1
        flags.append(flag)
        if flag & 8:
            repeat = face.buf[cursor]
            cursor += 1
            flags.extend([flag] * repeat)

    def coordinates(short_bit: int, same_bit: int) -> List[int]:
        nonlocal cursor
        values: List[int] = []
        value = 0
        for flag in flags:
            if flag & short_bit:
                delta = face.buf[cursor]
                cursor += 1
                value += delta if flag & same_bit else -delta
            elif not flag & same_bit:
                value += struct.unpack_from(">h", face.buf, cursor)[0]
                cursor += 2
            values.append(value)
        return values

    xs = coordinates(2, 16)
    ys = coordinates(4, 32)

    contours = []
    first = 0
    for last in ends:
        contours.append([
            (xs[i], ys[i], bool(flags[i] & 1)) for i in range(first, last + 1)
        ])
        first = last + 1
    return contours


def on_curve_points(contours) -> List[Tuple[float, float]]:
    """The points the curve passes through, implied midpoints included.

    TrueType lets two controls sit side by side and implies an on-curve
    point halfway between them; a cubic conversion has to state that point,
    so it is in the PDF and has to be here too for the two to line up.
    """
    out: List[Tuple[float, float]] = []
    for points in contours:
        run = list(points)
        if not run[0][2]:
            start = next((i for i, p in enumerate(run) if p[2]), None)
            if start is None:
                continue
            run = run[start:] + run[:start]
        for i, point in enumerate(run):
            following = run[(i + 1) % len(run)]
            if point[2]:
                out.append((point[0], point[1]))
            elif not following[2]:
                out.append(((point[0] + following[0]) / 2.0,
                            (point[1] + following[1]) / 2.0))
    return out


# --- the transform between them -----------------------------------------

def fit_affine(source, target):
    """Least squares for x' = a x + c y + e and y' = b x + d y + f."""

    def solve(values):
        normal = [[0.0] * 4 for _ in range(3)]
        for (x, y), value in zip(source, values):
            row = [x, y, 1.0]
            for i in range(3):
                for j in range(3):
                    normal[i][j] += row[i] * row[j]
                normal[i][3] += row[i] * value
        for i in range(3):
            pivot = max(range(i, 3), key=lambda r: abs(normal[r][i]))
            normal[i], normal[pivot] = normal[pivot], normal[i]
            for r in range(3):
                if r == i:
                    continue
                factor = normal[r][i] / normal[i][i]
                for column in range(i, 4):
                    normal[r][column] -= factor * normal[i][column]
        return [normal[i][3] / normal[i][i] for i in range(3)]

    a, c, e = solve([p[0] for p in target])
    b, d, f = solve([p[1] for p in target])
    residual = max(
        max(abs(a * x + c * y + e - px), abs(b * x + d * y + f - py))
        for (x, y), (px, py) in zip(source, target)
    )
    return (a, b, c, d, e, f), residual


# --- putting the two side by side ---------------------------------------

# Publisher exports a saddle-stitched booklet imposed two pages to a sheet,
# so a page sits at one of two offsets on it. Both are tried and the one
# holding a matching outline is the answer -- sheet numbers say nothing
# about page numbers in an imposed file.
def sheet_offsets(page_width: float, page_height: float, sheet: bytes):
    box = re.search(rb"/MediaBox\s*\[([^\]]*)\]", sheet)
    if box is None:
        return []
    values = [float(v) for v in re.findall(rb"-?[\d.]+", box.group(1))]
    width, height = values[2] - values[0], values[3] - values[1]
    margin_y = (height - page_height) / 2.0
    gutter = (width - 2.0 * page_width) / 2.0
    return [(gutter, margin_y), (gutter + page_width, margin_y)]


def measure(pub: Path, pdf: Path) -> None:
    document = convert.parse_document(pub)
    structure = pubfile.read_structure(pub)
    if structure is None or not structure.wordart:
        print(f"{pub.name}: no WordArt in the file")
        return
    page_width = document.pages[0].width if document.pages else 0.0
    page_height = document.pages[0].height if document.pages else 0.0

    objs = _objects(pdf.read_bytes())
    sheets = _pages(objs)
    drawn = [(sheet, painted_paths(_content(objs, sheet))) for sheet in sheets]

    print(f"{pub.name} against {pdf.name}")
    print(f"  {'glyph':6s} {'band':>15s} {'y drawn':>8s} {'x drawn':>8s} "
          f"{'stretch':>8s} {'written':>8s} {'slant':>7s} {'fit':>7s}")

    for art in structure.wordart:
        # One glyph, no rotation: the only case where a path in the PDF can
        # be named without guessing which letter it is.
        if art.rotation or len(art.text.strip()) != 1:
            continue
        try:
            face = fontmetrics.find_face(art.font, art.bold, art.italic)
        except Exception:
            face = None
        if face is None:
            print(f"  {art.text!r:6s} {art.font} is not installed here")
            continue
        glyph = face.cmap.get(ord(art.text.strip()))
        if not glyph:
            continue
        source = on_curve_points(outline(face, glyph))
        if len(source) < 8:
            continue

        band_x = page_width / 2.0 + art.centre_x - art.width / 2.0
        band_y = page_height / 2.0 + art.centre_y - art.height / 2.0
        found = None
        for sheet, paths in drawn:
            for offset_x, offset_y in sheet_offsets(page_width, page_height,
                                                    sheet):
                # PDF pages measure y upwards, Publisher's page downwards.
                left, right = offset_x + band_x, offset_x + band_x + art.width
                top = offset_y + (page_height - band_y)
                bottom = top - art.height
                for path in paths:
                    xs = [p[1] for p in path]
                    ys = [p[2] for p in path]
                    if not (left - 4 <= min(xs) and max(xs) <= right + 4):
                        continue
                    if not (bottom - 4 <= min(ys) and max(ys) <= top + 4):
                        continue
                    target = [(p[1], p[2]) for p in path if p[0] == "on"]
                    # The PDF closes the contour by restating its first
                    # point; the font does not.
                    if len(target) == len(source) + 1:
                        target = target[:-1]
                    if len(target) != len(source):
                        continue
                    transform, residual = fit_affine(source, target)
                    if found is None or residual < found[1]:
                        found = (transform, residual)
        if found is None:
            print(f"  {art.text!r:6s} no matching outline in the PDF")
            continue

        (a, _b, c, d, _e, _f), residual = found
        x_size, y_size = a * face.upem, d * face.upem
        lines = re.split(r"\r\n|\r|\n", art.text)
        _size, written, _source = convert._wordart_fit(
            art, lines, fontmetrics.measure
        )
        slant = math.degrees(math.atan(c * face.upem / y_size)) if y_size else 0.0
        print(f"  {art.text!r:6s} {art.width:6.2f}x{art.height:6.2f} "
              f"{y_size:8.3f} {x_size:8.3f} {x_size / y_size * 100:7.2f}% "
              f"{(f'{written:.2f}%' if written else '-'):>8s} "
              f"{slant:6.2f}° {residual:6.3f}pt")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[0])
        print("usage: wordart_stretch.py <file.pub> <publisher-export.pdf>")
        raise SystemExit(2)
    measure(Path(sys.argv[1]), Path(sys.argv[2]))
