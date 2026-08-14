"""Print every table's grid and every cell's raw fields, from the .pub.

The tool behind `actions.md` §9. `pubfile` reads the two fields of a cell
record whose meaning is settled -- the row/column bounds and the four
insets -- and ignores the rest. This prints all of them, unnamed and
unconverted, so a file made deliberately in Publisher can be diffed
against its control and the remaining fields identified.

    python3 research/table_cells.py files/table-samples/*.pub

Each cell prints as its position, then `id=value` for every field in the
record, with EMU values also shown in points, since insets and cached
extents are both stored in EMU.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import pubfile  # noqa: E402

EMU_PER_POINT = 12700.0

# What is settled, and printed with a name rather than a bare id.
KNOWN = {
    0x01: "first-row", 0x02: "last-row",
    0x03: "first-column", 0x04: "last-column",
    0x0A: "inset-left", 0x0B: "inset-top",
    0x0C: "inset-right", 0x0D: "inset-bottom",
}
# Fields that hold a length rather than a count or a flag.
LENGTHS = frozenset({0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E})


def describe(field_id: int, value: int) -> str:
    name = KNOWN.get(field_id, f"0x{field_id:02x}")
    if field_id in LENGTHS:
        return f"{name}={value} ({value / EMU_PER_POINT:.3f}pt)"
    return f"{name}={value}"


def dump(source: Path) -> None:
    data = source.read_bytes()
    contents = pubfile._read_stream(data, "Contents")
    if not contents:
        print(f"{source.name}: no Contents stream")
        return

    refs = pubfile._chunk_references(contents)
    cells_at = {seq: off for seq, kind, off in refs if kind == pubfile._CELLS_CHUNK}
    tables = [(seq, off) for seq, kind, off in refs if kind == pubfile._TABLE_CHUNK]
    print(f"\n===== {source.name}: {len(tables)} table(s)")

    for seq, offset in tables:
        fields, signature = pubfile._table_grid(contents, offset)
        if signature is None:
            print(f"  table seq={seq}: no grid")
            continue
        widths, heights = signature
        print(f"\n  table seq={seq}  {len(heights)} rows x {len(widths)} columns")
        print(f"    columns (pt): {list(widths)}")
        print(f"    rows    (pt): {list(heights)}")
        other = {
            f"0x{k:02x}": v
            for k, v in sorted(fields.items())
            if k not in (0x66, 0x67, 0x68, 0x69, 0x6B)
        }
        print(f"    other table fields: {other}")

        cells_offset = cells_at.get(fields.get(pubfile._TABLE_CELLS_SEQNUM))
        if cells_offset is None:
            print("    no cells chunk")
            continue
        for block in pubfile._chunk_blocks(contents, cells_offset):
            if block.id != pubfile._CELL_ARRAY:
                continue
            for record in pubfile._children(contents, block):
                cell = {
                    sub.id: sub.data for sub in pubfile._children(contents, record)
                }
                position = (cell.get(0x01, 0), cell.get(0x03, 0))
                rest = " ".join(
                    describe(k, v) for k, v in sorted(cell.items()) if k > 0x04
                )
                print(f"    cell r{position[0]} c{position[1]}: {rest}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for argument in sys.argv[1:]:
        dump(Path(argument))
