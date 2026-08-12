"""Diff the Contents-stream blocks of .pub files that differ in one setting.

Companion to diff_wrap.py, which works on a debug libmspub trace and so
only sees blocks libmspub already reads. This one parses the file
directly, so it also sees the blocks libmspub walks past -- which is
where page margins would have to be, since libmspub has no page-margin
concept at all.

Use it the same way: produce two or more files identical except for the
one setting you are chasing, then look for a block whose value tracks
that setting.

    python3 research/diff_blocks.py \\
      a=files/margin-samples/margins-a.pub \\
      b=files/margin-samples/margins-b.pub

Chunks are matched across files by (chunk type, ordinal), which is stable
as long as the documents have the same structure -- guaranteed if they
came from one document saved repeatedly with a single setting changed.

Values are reported raw and, where they could plausibly be a length, in
inches at 914400 EMU per inch. A margin of 0.5in reads 457200.
"""

import struct
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from master_pages import (  # noqa: E402
    chunk_references,
    parse_block,
    read_stream,
)

EMU_PER_INCH = 914400

CHUNK_NAMES = {
    0x01: "SHAPE", 0x10: "TABLE", 0x20: "ALTSHAPE", 0x30: "GROUP",
    0x31: "LOGO", 0x43: "PAGE", 0x44: "DOCUMENT", 0x46: "BORDER_ART",
    0x5C: "PALETTE", 0x63: "CELLS", 0x6C: "FONT",
}


def blocks_in_chunk(contents: bytes, offset: int):
    """Every top-level block of one chunk, as {(id, type): value}."""
    if offset + 4 > len(contents):
        return {}
    length = struct.unpack_from("<I", contents, offset)[0]
    end = min(offset + length, len(contents))
    pos, out = offset + 4, {}
    while pos < end:
        before = pos
        block, pos = parse_block(contents, pos, skip_hierarchical=True)
        if pos <= before:
            break
        raw = contents[block.data_offset:block.data_offset + min(block.data_length, 32)]
        out[(block.id, block.type)] = (block.data, raw.hex())
    return out


def profile(path: Path):
    """{(chunk type, ordinal, block id, block type): (value, raw)}."""
    contents = read_stream(path, "Contents")
    ordinal = defaultdict(int)
    out = {}
    for ref in chunk_references(contents):
        kind = ref["type"]
        index = ordinal[kind]
        ordinal[kind] += 1
        for (block_id, block_type), value in blocks_in_chunk(contents, ref["offset"]).items():
            out[(kind, index, block_id, block_type)] = value
    return out


def maybe_length(value: int) -> str:
    if 0 < value <= 60 * EMU_PER_INCH and value % 25 == 0:
        return f"  ({value / EMU_PER_INCH:.4f} in)"
    return ""


def main(pairs) -> None:
    profiles = {label: profile(path) for label, path in pairs}
    labels = [label for label, _ in pairs]

    keys = set()
    for values in profiles.values():
        keys |= set(values)

    changed = []
    for key in sorted(keys):
        seen = [profiles[label].get(key) for label in labels]
        if any(v is None for v in seen):
            continue                       # block absent somewhere; not a clean signal
        if len({v[1] for v in seen}) > 1:
            changed.append((key, seen))

    if not changed:
        print("no block changed across these files")
        print("(if you expected one: check the files really differ, and that "
              "nothing else was touched between saves)")
        return

    print(f"{len(changed)} block(s) differ across {len(labels)} file(s)\n")
    for (kind, index, block_id, block_type), seen in changed:
        name = CHUNK_NAMES.get(kind, f"0x{kind:02x}")
        print(f"{name}[{index}]  block id 0x{block_id:02x} type 0x{block_type:02x}")
        for label, (value, raw) in zip(labels, seen):
            print(f"    {label:<12} {value:>12}{maybe_length(value):<16}  {raw}")
        print()


if __name__ == "__main__":
    args = []
    for argument in sys.argv[1:]:
        if "=" not in argument:
            sys.exit(f"expected label=path, got {argument!r}")
        label, _, path = argument.partition("=")
        args.append((label, Path(path)))
    if len(args) < 2:
        sys.exit(__doc__)
    main(args)
