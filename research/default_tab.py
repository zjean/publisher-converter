"""Print the one document-wide length each .pub states, and where it lives.

Written to chase actions.md §10: 200-odd paragraphs in the corpus write a
tab and state no stop for it, because they were lined up on Publisher's
document-wide "Default tab stops" grid, and nobody had found where the
file records that interval.

Publisher does record it somewhere: the setting is `DefaultTabStop` on
the Document object in its own VBA -- "the default tab stop for all text
in the active publication", valid range 1 to 1584 points -- so it is a
per-publication value, not an application preference, and it has to be
saved with the publication.

This prints the two places a document-wide length turns up, so a reading
of `ActiveDocument.DefaultTabStop` taken in Publisher can be matched
against them:

  - **Contents, DOCUMENT chunk, block 0x15.** It reads 359410 in all
    seven corpus files that have it and is absent from the two that do
    not, so on its own it cannot be the setting: the three `kerkbode`
    issues state 359410 here while stating something else entirely in
    the Quill stream. (The earlier note that ruled this block out gave
    the wrong reason -- it said `Blank Note Card` reads 359410 too, and
    that file does not carry the block at all.)

    Not entirely unrelated to `SGP `, though. At 12700 EMU per point
    359410 is **exactly 28.3pt**, which is what `SGP ` states in three of
    these files, and within a twentieth of a point of the fourth. So the
    two agree in four documents of seven and part company only in the
    `kerkbode` issues -- which reads as one quantity written twice, a
    template default beside the value in force, rather than two unrelated
    lengths. 28.3pt is also 1cm to within 0.05pt: a metric-locale default,
    against the 0.5in/36pt the VBA documentation quotes.

    Which is why the reading that settles this has to be taken on a
    `kerkbode` file. It is the only one where the two candidates predict
    different answers.

  - **Quill, `SGP ` chunk.** The candidate. The chunk is a bare U32
    length and then at most one block, of id 0x00 -- the same id a tab
    position carries inside a paragraph's stops -- type 0x22, holding a
    U32 of EMU. Length 4 means the block is absent and the document is
    on whatever Publisher's default is; length 10 means it states one.

What the corpus shows, and why `SGP ` is the candidate:

  - it is **absent from both files that contain no tab at all** and
    present in **all seven that contain one**;
  - it **varies**, which block 0x15 does not: 28.30pt for
    `Cantico_dei_Cantici`, `MISSAL` and `rotated_text`, 28.2898pt for
    `Lisa Hoogendijk`, and 8.0787pt for all three `kerkbode` issues.

8.0787pt against InDesign's assumed 36pt is a four-and-a-half-fold error
on every tab in the three biggest files in the corpus, which is the size
of mistake the warning is there to describe.

None of that identifies the field outright -- a document-wide length
could be a hyphenation zone as easily as a tab interval. One line typed
in Publisher's VBA immediate window settles it, against the files we
already have rather than files that have to be authored:

    ? ActiveDocument.DefaultTabStop

Open each .pub, read the number, and compare it with the `SGP ` column
below. Points are always what that property returns, which is the column
this prints.

    python3 research/default_tab.py files
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import pubfile  # noqa: E402

# The chunk name is four bytes wide and this one is three letters long.
_SECTION_CHUNK = "SGP "
# id 0x00 is the id a tab position carries inside a paragraph's stops;
# type 0x22 is one of the format's four-byte integers.
_SECTION_LENGTH, _SECTION_TYPE = 0x00, 0x22
# The document chunk's block 0x15, kept for comparison because it is the
# one previously suspected of holding the interval.
_DOCUMENT_CHUNK, _DOCUMENT_BLOCK = 0x44, 0x15


def section_length(quill: bytes):
    """The one length the `SGP ` chunk states, in points, or None."""
    for name, offset, length in pubfile._quill_chunks(quill):
        if name != _SECTION_CHUNK:
            continue
        stated = struct.unpack_from("<I", quill, offset)[0]
        for block in pubfile._blocks(quill, offset + 4, offset + stated):
            if (block.id, block.type) == (_SECTION_LENGTH, _SECTION_TYPE):
                return block.data / pubfile._EMU_PER_POINT
    return None


def document_block(contents: bytes):
    """Block 0x15 of the document chunk, in EMU, or None."""
    for _seq, kind, offset in pubfile._chunk_references(contents):
        if kind != _DOCUMENT_CHUNK:
            continue
        stated = struct.unpack_from("<I", contents, offset)[0]
        for block in pubfile._blocks(contents, offset + 4, offset + stated):
            if block.id == _DOCUMENT_BLOCK:
                return block.data
    return None


def report(path: Path) -> None:
    data = path.read_bytes()
    quill = pubfile._read_stream(data, *pubfile._QUILL_STREAM) or b""
    contents = pubfile._read_stream(data, *pubfile._CONTENTS_STREAM) or b""

    stops = pubfile._paragraph_stops(quill) if quill else []
    tabbed = len(stops)
    stated = sum(1 for _text, entries in stops if entries)

    length = section_length(quill) if quill else None
    block = document_block(contents) if contents else None

    print(f"{path.name[:38]:<40}"
          f"{('-' if length is None else f'{length:.4f}pt'):>16}"
          f"{str(block):>10}   "
          f"{tabbed:>3} tabbed, {stated:>3} with a stop of their own")


def main(targets) -> None:
    print(f"{'file':<40}{'SGP':>16}{'0x15':>10}   paragraphs")
    for target in targets:
        for path in ([target] if target.is_file() else sorted(target.rglob("*.pub"))):
            report(path)
    print("\nCompare the SGP column with `? ActiveDocument.DefaultTabStop`\n"
          "read in Publisher for the same file. A match identifies the field.")


if __name__ == "__main__":
    main([Path(a) for a in sys.argv[1:]] or [Path("files")])
