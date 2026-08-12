"""Route A: identify Publisher's text-wrap field by controlled diff.

Give this a set of .pub files that are identical except for one picture's
text-wrap setting, and it reports which shape block IDs change with it.
Because only one variable moves between the files, a block whose value
tracks the wrap setting is the wrap field.

Usage (names before the '=' are just labels shown in the report):

    python3 research/diff_wrap.py /tmp/pubdump_debug \\
        none=wrap-none.pub square=wrap-square.pub tight=wrap-tight.pub \\
        through=wrap-through.pub topbottom=wrap-topbottom.pub

Requires the debug-instrumented pubdump; see actions.md for how to build
it. Shapes are keyed across files by their SHAPE_WIDTH/SHAPE_HEIGHT, which
stay constant when only the wrap setting is changed.
"""

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from research.find_wrap import EMUS_IN_INCH, HANDLED, geometry, trace_shapes  # noqa: E402


def shape_table(pubdump_debug, path):
    """{(width_pt, height_pt): {block_id: value}} for one document."""
    shapes, _ = trace_shapes(pubdump_debug, path)
    table = {}
    for blocks in shapes.values():
        w, h = geometry(blocks)
        if w is None or h is None:
            continue
        key = (round(w, 1), round(h, 1))
        table[key] = {bid: data for bid, _, data in blocks if bid not in HANDLED}
    return table


def main(pubdump_debug, labelled_paths):
    variants = {}
    for entry in labelled_paths:
        label, _, path = entry.partition("=")
        if not path:
            label, path = Path(entry).stem, entry
        variants[label] = shape_table(pubdump_debug, Path(path))

    labels = list(variants)
    print(f"variants: {', '.join(labels)}\n")

    shared = set.intersection(*(set(v) for v in variants.values()))
    if not shared:
        print("No shapes matched across all variants. The files must differ "
              "only in the wrap setting, with geometry untouched.")
        return
    print(f"shapes present in every variant: {len(shared)}\n")

    hits = collections.defaultdict(list)
    for key in sorted(shared):
        for bid in sorted(set().union(*(set(variants[l].get(key, {})) for l in labels))):
            values = [variants[l].get(key, {}).get(bid) for l in labels]
            if len(set(values)) > 1:
                hits[bid].append((key, values))

    if not hits:
        print("No block changed between variants. Either the wrap setting is "
              "not stored per shape, or the samples are not actually different.")
        return

    print(f"{'block':>6}  {'shapes':>6}  values per variant ({' / '.join(labels)})")
    print("-" * 88)
    for bid, rows in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        key, values = rows[0]
        rendered = " / ".join("-" if v is None else str(v) for v in values)
        print(f"  0x{bid:02X}  {len(rows):>6}  {rendered}")

    print("\nA block that changes on the picture you edited, takes one small "
          "value per wrap mode, and does not change on other shapes is the "
          "wrap field. Check 0x34 first (see actions.md).")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2:])
