"""What the .pub says its page order is, and which pages libmspub loses.

libmspub calls `startPage` only for a page carrying shapes of its own, so
a page whose content comes from its master alone -- a numbered, otherwise
blank leaf -- never reaches the event stream. The loss is silent and it is
not at the end, so everything after it arrives one place early.

Chunk 0x44's 0x02 array is the file's own page order: one entry per page
chunk, masters and Publisher's scratch band included. This prints it
against what libmspub reported, so the claim the converter rests on can be
re-checked on any corpus -- that the entries carrying shapes are exactly
libmspub's pages, in exactly libmspub's order.

    python3 research/page_order.py files/cgk/*.pub

Ground truth for the two newsletters is Publisher's own exported PDFs in
files/experiments: both impose fourteen sheets two pages up, so both
documents are 28 pages, where libmspub reports 27 and 28.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import convert, pubfile


def report(source: Path) -> None:
    contents = pubfile._read_stream(source.read_bytes(), *pubfile._CONTENTS_STREAM)
    if not contents:
        print(f"{source.name}: no Contents stream")
        return
    refs = pubfile._chunk_references(contents)
    chunks = {
        seq: pubfile._page_structure(contents, seq, offset)
        for seq, kind, offset in refs
        if kind == pubfile._PAGE_CHUNK
    }
    order = pubfile._read_page_order(contents, refs)
    structure = pubfile.read_structure(source)
    document = convert.parse_document(source)
    measured = convert._page_by_chunk(document, structure)
    blanks = {chunk.seq for _position, chunk in structure.blank_pages}

    print(f"\n=== {source.name}")
    print(f"    {len(order)} entries listed, {len(chunks)} page chunks, "
          f"{len(document.pages)} pages reported, "
          f"{len(document.pages) + len(structure.blank_pages)} stated")

    # The same trailing run `_blank_pages` drops: past it, an entry is
    # Publisher's scratch band rather than a page of the document.
    listed = [
        seq for seq in order
        if seq in chunks
        and not chunks[seq].is_master
        and seq not in pubfile._DUMMY_PAGE_SEQNUMS
    ]
    while listed and not chunks[listed[-1]].shape_count:
        listed.pop()
    body = set(listed)

    position = 0
    for seq in order:
        page = chunks.get(seq)
        if page is None:
            print(f"      {'':>4} {seq:>6}  not a page chunk")
            continue
        if page.is_master:
            print(f"      {'':>4} {seq:>6}  master")
            continue
        if seq in pubfile._DUMMY_PAGE_SEQNUMS:
            print(f"      {'':>4} {seq:>6}  dummy")
            continue
        if seq not in body:
            print(f"      {'':>4} {seq:>6}  scratch band -- not a page")
            continue
        position += 1
        if seq in blanks:
            note = "BLANK -- libmspub never reported it, restored here"
        elif page.shape_count:
            index = measured.get(seq)
            note = (f"{page.shape_count:>3} shapes"
                    + (f", measured onto libmspub page {index + 1}" if index is not None
                       else ", not measured onto any page"))
        else:
            note = "no shapes"
        print(f"      p{position:<3} {seq:>6}  {note}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for argument in sys.argv[1:]:
        report(Path(argument))
