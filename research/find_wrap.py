"""Route B: hunt for Publisher's text-wrap field in the blocks libmspub discards.

libmspub's shape-chunk loop consumes only six block IDs and throws the
rest away. This script traces a debug build, groups the discarded blocks
per shape, keys each shape to converter geometry via SHAPE_WIDTH/HEIGHT,
labels it (picture overlapping text = probably wrapped) and reports which
discarded block IDs partition the shapes the same way the label does.

A perfect partition is a candidate wrap field. It is still only a
correlation: confirming it needs the controlled diff in actions.md.
"""
import collections, io, json, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pubidml import model  # noqa: E402

EMUS_IN_INCH = 914400.0
HANDLED = {0xAA, 0xAB, 0x09, 0x07, 0x27, 0x35, 0xB7}

BLOCK = re.compile(r'parseBlock dataOffset (0x[0-9a-f]+), id (0x[0-9a-f]+), '
                   r'type (0x[0-9a-f]+), dataLength (0x[0-9a-f]+), integral data (0x[0-9a-f]+)')
SHAPE = re.compile(r'parseShape: seqNum (0x[0-9a-f]+)')


def trace_shapes(pubdump_debug, path):
    proc = subprocess.run([pubdump_debug, str(path)], capture_output=True)
    trace = proc.stderr.decode('utf-8', 'replace')
    shapes, current = collections.OrderedDict(), None
    for line in trace.splitlines():
        m = SHAPE.search(line)
        if m:
            current = int(m.group(1), 16)
            shapes.setdefault(current, [])
            continue
        m = BLOCK.search(line)
        if m and current is not None:
            _, bid, btype, blen, data = (int(g, 16) for g in m.groups())
            shapes[current].append((bid, btype, data))
    return shapes, proc.stdout.decode('utf-8', 'replace')


def geometry(shape_blocks):
    w = h = None
    for bid, _, data in shape_blocks:
        if bid == 0xAA:
            w = data / EMUS_IN_INCH * 72.0
        elif bid == 0xAB:
            h = data / EMUS_IN_INCH * 72.0
    return w, h


def overlaps(a, b):
    return not (a.x + a.width <= b.x or b.x + b.width <= a.x
                or a.y + a.height <= b.y or b.y + b.height <= a.y)


def label_items(doc):
    """Return [(w, h, label)] for every item, matched later with tolerance."""
    out = []
    for page in doc.pages:
        texts = [i for i in model._walk(page.items) if isinstance(i, model.TextFrame)]
        for item in model._walk(page.items):
            if isinstance(item, model.Image):
                lab = "picture_over_text" if any(overlaps(item, t) for t in texts) else "picture_alone"
            elif isinstance(item, model.TextFrame):
                lab = "text"
            else:
                lab = "shape"
            out.append((item.width, item.height, lab))
    return out


def match(w, h, labels, tolerance=2.0):
    """Key a traced shape to a converter item by size, within tolerance."""
    best, best_err = None, tolerance
    for iw, ih, lab in labels:
        err = max(abs(iw - w), abs(ih - h))
        if err <= best_err:
            best, best_err = lab, err
    return best


def main(pubdump_debug, files):
    # discarded block id -> label -> Counter(values)
    table = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    label_totals = collections.Counter()
    unmatched = 0

    for path in files:
        shapes, events = trace_shapes(pubdump_debug, path)
        doc = model.build(io.StringIO(events))
        labels = label_items(doc)
        for seq, blocks in shapes.items():
            w, h = geometry(blocks)
            if w is None or h is None:
                unmatched += 1
                continue
            lab = match(w, h, labels)
            if lab is None:
                unmatched += 1
                continue
            label_totals[lab] += 1
            for bid, _, data in blocks:
                if bid in HANDLED:
                    continue
                table[bid][lab][data] += 1

    print(f"shapes labelled: {dict(label_totals)}   unmatched: {unmatched}\n")
    print("Discarded block IDs seen on PICTURE shapes, ranked as wrap candidates.")
    print("A wrap field should: appear on pictures, take few distinct small")
    print("values, and not be a coordinate or identifier.\n")
    print(f"{'block':>6} {'#pics':>6} {'distinct values on pictures':>34}  {'also on text?':>13}  note")
    print("-" * 92)

    rows = []
    for bid, per in table.items():
        pic = collections.Counter()
        pic.update(per.get("picture_over_text", {}))
        pic.update(per.get("picture_alone", {}))
        if not pic:
            continue
        values = sorted(pic)
        n_pics = sum(pic.values())
        small_enum = all(v <= 16 for v in values) and len(values) <= 6
        on_text = bool(per.get("text"))
        rows.append((not small_enum, len(values), bid, values, n_pics, on_text))

    for _, _, bid, values, n_pics, on_text in sorted(rows):
        shown = ", ".join(str(v) for v in values[:6])
        small_enum = all(v <= 16 for v in values) and len(values) <= 6
        note = "enum-like -> CHECK FIRST" if small_enum else "wide range, likely coord/id"
        print(f"  0x{bid:02X} {n_pics:>6} {shown:>34}  {str(on_text):>13}  {note}")


if __name__ == "__main__":
    main(sys.argv[1], [Path(p) for p in sys.argv[2:]])
