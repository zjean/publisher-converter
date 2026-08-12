"""Look for Publisher's field markers in the Quill text stream.

A page-number field is stored as a bare '#' in the text. libmspub has no
field handling of any kind, so it arrives as a literal character and a
15-page document converts with '#' in the footer of every page instead of
1, 2, 3. Nothing in the text run distinguishes it from a '#' somebody
typed.

Something else must mark it, and the candidate is the `TOKN` chunk --
"token" -- one of six Quill chunk types libmspub never reads. It only
looks at TEXT, STRS, SYID, "PL  ", FDPC, FDPP and STSH; BTEC, BTEP, FONT,
"INK ", MCLD, "SGP " and TOKN are all skipped.

What this script establishes, across the sample set:

  - No file contains a '#' in its text without also having a TOKN chunk.
  - The one document with page numbers has two TOKN chunks, two '#'
    characters and two master pages, each master carrying one footer.
  - A file can have TOKN and no '#', which is expected: TOKN is a general
    token table and Publisher has field types that do not render as '#'.

That is enough to use TOKN as a *gate* -- no TOKN chunk means every '#'
in the document is literal text -- without decoding it fully.

Two things about its contents are legible without a controlled sample.
The chunk ends with a counted UTF-16 string in one file: 07 00 "orgname",
Publisher's Organization Name field, so TOKN is a field table and names
are stored in it. And the two files differ at byte 16, which reads 3 in
the document whose only field is a page number and 28 in the document
whose field is "orgname" -- consistent with a field-type code, though
two samples cannot establish that.

What is still missing is the link from a token to a position in the text.
Both TOKN chunks in the page-numbered file are byte-identical 52-byte
blobs and carry no character offsets, so the association must live
elsewhere -- probably the chunk id, which is 6 and 7 there. Pinning that
down needs a controlled pair of files differing only in one field, which
needs Publisher.

Quill layout, from libmspub 0.1.5 MSPUBParser::parseQuill:

    0x18  U16 (unused), U16 chunk count, U32 next-list offset
    then `count` references of 24 bytes:
      U16 (0x18), 4-char name, U16 id, 4 bytes, 4-char name2,
      U32 offset, U32 length
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from master_pages import read_stream  # noqa: E402

# Everything libmspub reads out of the Quill stream. Anything else in a
# file is information the converter currently cannot see.
READ_BY_LIBMSPUB = {"TEXT", "STRS", "SYID", "PL  ", "FDPC", "FDPP", "STSH"}


def chunk_references(quill: bytes):
    """Every Quill chunk: (name, id, offset, length)."""
    out, offset, seen = [], 0x18, set()
    while offset != 0xFFFFFFFF and offset + 8 <= len(quill) and offset not in seen:
        seen.add(offset)
        count = struct.unpack_from("<H", quill, offset + 2)[0]
        nxt = struct.unpack_from("<I", quill, offset + 4)[0]
        pos = offset + 8
        for _ in range(count):
            if pos + 24 > len(quill):
                break
            name = quill[pos + 2:pos + 6].decode("ascii", "replace")
            chunk_id = struct.unpack_from("<H", quill, pos + 6)[0]
            chunk_offset, length = struct.unpack_from("<II", quill, pos + 16)
            out.append((name, chunk_id, chunk_offset, length))
            pos += 24
        offset = nxt
    return out


def hash_positions(quill: bytes, text_offset: int, text_length: int):
    """Character indices of '#' within the TEXT chunk, which is UTF-16LE."""
    body = quill[text_offset:text_offset + text_length]
    return [i // 2 for i in range(0, len(body) - 1, 2) if body[i:i + 2] == b"#\x00"]


def report(path: Path) -> None:
    try:
        quill = read_stream(path, "CONTENTS")
    except (KeyError, ValueError) as exc:
        print(f"\n{path.name}\n  no Quill stream ({exc.__class__.__name__})")
        return

    chunks = chunk_references(quill)
    text = next((c for c in chunks if c[0] == "TEXT"), None)
    hashes = hash_positions(quill, text[2], text[3]) if text else []
    tokens = [c for c in chunks if c[0] == "TOKN"]

    print(f"\n{path.name}")
    print(f"  {len(chunks)} Quill chunks, {len(tokens)} TOKN, "
          f"{len(hashes)} '#' in text at {hashes or '-'}")
    for name, chunk_id, offset, length in chunks:
        if name in READ_BY_LIBMSPUB:
            continue
        print(f"    {name!r:8} id {chunk_id:<4} {length:>6} bytes   ignored by libmspub")
    for name, chunk_id, offset, length in tokens:
        print(f"    TOKN id {chunk_id} raw: {quill[offset:offset + length].hex()}")

    if hashes and not tokens:
        print("  *** hypothesis broken: '#' present with no TOKN chunk ***")


if __name__ == "__main__":
    targets = [Path(a) for a in sys.argv[1:]] or sorted(Path("files").glob("*.pub"))
    for target in targets:
        report(target)
