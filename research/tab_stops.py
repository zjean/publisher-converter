"""Print every tab stop a .pub states, and every tab left without one.

The evidence behind backlog §12. libmspub parses tab stops into
`ParagraphStyle::m_tabStopsInEmu` and its collector never reads that
member, so they are dropped before librevenge sees anything; `pubfile`
reads them out of the Quill stream instead.

    python3 research/tab_stops.py files

Three things this was written to settle, all of which it answers against
the corpus:

  - **What the record holds.** A stop is a container of a position in
    signed EMU and, on 22 of the corpus's 335 stops, one more U16 whose
    low byte reads 1 or 2. Those 22 come in pairs, one at the middle of
    the frame reading 2 and one at its right edge reading 1, which is a
    centre tab and a right tab -- a header or footer's own two stops.
  - **How much of the problem stops actually cover.** Very little: the
    corpus writes tabs in 203 paragraphs and states a stop on 3 of them.
    The rest fall on a default grid the file does not record.
  - **Where a paragraph's stops live.** Either in the paragraph itself
    (FDPP) or in the paragraph style it names (STSH, block 0x19). No
    tabbed paragraph in the corpus names a style that has any, so the
    inheritance is unexercised and `pubfile` does not implement it. This
    prints both, so a file that does exercise it can be recognised.
"""

import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import pubfile  # noqa: E402

# STSH holds the document's default styles, alternating character and
# paragraph; libmspub reads the second STSH chunk and takes paragraph
# style k from entry 2k+1, each of which opens with a U16 of its own.
_STYLES_CHUNK = "STSH"
_STYLE_INDEX = 0x19


def default_styles(quill: bytes, offset: int):
    """The tab stops of each default paragraph style, by index."""
    count = struct.unpack_from("<I", quill, offset + 4)[0]
    out = {}
    for index in range(count):
        at = struct.unpack_from("<I", quill, offset + 20 + 4 * index)[0]
        if index % 2:
            out[index // 2] = pubfile._style_tab_stops(quill, offset + 20 + at + 2)
    return out


def paragraph_styles(quill: bytes, chunks):
    """Every paragraph style in the file, as (style index it names, stops)."""
    out = []
    for _name, offset, _length in [c for c in chunks if c[0] == "FDPP"]:
        count = struct.unpack_from("<H", quill, offset)[0]
        table = offset + 8
        for index in range(count):
            at = struct.unpack_from("<H", quill, table + 4 * count + 2 * index)[0]
            position = offset + at
            length = struct.unpack_from("<I", quill, position)[0]
            names = None
            for block in pubfile._blocks(quill, position + 4, position + length):
                if block.id == _STYLE_INDEX:
                    names = block.data
            out.append((names, pubfile._style_tab_stops(quill, position)))
    return out


def dump(path: Path) -> None:
    data = path.read_bytes()
    quill = pubfile._read_stream(data, *pubfile._QUILL_STREAM)
    if not quill:
        print(f"{path.name}: no Quill stream")
        return
    chunks = pubfile._quill_chunks(quill)
    styles = [c for c in chunks if c[0] == _STYLES_CHUNK]
    defaults = default_styles(quill, styles[1][1]) if len(styles) > 1 else {}
    paragraphs = paragraph_styles(quill, chunks)

    stops = pubfile._paragraph_stops(quill)
    stated = [(text, tabs) for text, tabs in stops if tabs]

    # Every stop in the file, wherever it is stated. This is the census the
    # alignment reading rests on: a stop whose alignment is stated at all
    # is one of a pair, at the middle of a frame or at its right edge.
    census = Counter(
        alignment
        for tabs in [t for _index, t in paragraphs] + list(defaults.values())
        for _position, alignment in tabs
    )

    print(f"\n=== {path.name} ===")
    print(f"  stops stated anywhere in the file: {sum(census.values())} "
          f"{dict(census) or ''}")
    print(f"  paragraphs carrying a tab: {len(stops)}, "
          f"of which stating a stop: {len(stated)}")
    for text, tabs in stated:
        positions = ", ".join(
            f"{position:.2f}pt {alignment}" for position, alignment in tabs
        )
        print(f"    {text[:52]!r}\n        {positions}")
    for index, tabs in sorted(defaults.items()):
        if tabs:
            print(f"    default paragraph style {index}: "
                  + ", ".join(f"{p:.2f} {a}" for p, a in tabs))
    for names, tabs in paragraphs:
        if tabs and any(alignment != "left" for _position, alignment in tabs):
            print("    a paragraph's own stops: "
                  + ", ".join(f"{p:.2f} {a}" for p, a in tabs))
    if defaults:
        used = Counter(names for names, _tabs in paragraphs if names is not None)
        inheriting = {i: n for i, n in used.items() if defaults.get(i)}
        print(f"    paragraphs naming a style that states stops: {inheriting or 'none'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for argument in sys.argv[1:]:
        target = Path(argument)
        for source in sorted(target.rglob("*.pub")) if target.is_dir() else [target]:
            dump(source)
