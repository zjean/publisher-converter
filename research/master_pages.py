"""Read page/master structure straight out of a .pub, bypassing libmspub.

libmspub resolves master pages internally and never announces them: it
replays the master's shapes onto each page and the drawing interface sees
only ordinary shapes. Everything needed to know *which* page is a master,
and which master a page applies, is nevertheless sitting in the file's
`Contents` stream, in the same page chunks libmspub already walks past.

This reads it directly. It is deliberately a research script -- if the
result is worth shipping it belongs in the C++ shim or a proper module --
but it settles the question of whether the information is reachable
without forking the library.

Format, taken from libmspub 0.1.5 MSPUBParser.cpp:

    Contents stream
      0x1a  U32 trailerOffset
      at trailerOffset: U32 trailerLength, then <= 3 blocks; the one of
      type TRAILER_DIRECTORY (0x90) holds a list of chunk references.
      Each reference is a GENERAL_CONTAINER (0x88) whose sub-blocks give
      CHUNK_TYPE (id 0x02), CHUNK_OFFSET (id 0x04) and optionally
      CHUNK_PARENT_SEQNUM (id 0x05). Sequence numbers are assigned by
      position in that list.

    Block
      U8 id, U8 type, then a payload whose length comes from the type;
      types 0x80/0x82/0x88/0x8a/0x90/0x98/0xa0/0xc0 are variable and
      begin with a U32 length that includes the length field itself.

    Page chunk (chunk type PAGE = 0x43)
      id 0x0E THIS_MASTER_NAME     -> a non-zero string means "this page
                                      is itself a master"
      id 0x0D APPLIED_MASTER_NAME  -> seqnum of the master it applies
"""

import struct
import sys
from pathlib import Path

# Block types whose payload length is fixed by the type alone.
FIXED_LENGTH = {
    0x78: 0, 0x05: 0, 0x08: 0, 0x0A: 0,
    0x10: 2, 0x12: 2, 0x18: 2, 0x1A: 2, 0x07: 2,
    0x20: 4, 0x22: 4, 0x58: 4, 0x68: 4, 0x70: 4, 0xB8: 4,
    0x28: 8, 0x38: 16, 0x48: 24,
}
VARIABLE = {0xC0, 0x80, 0x82, 0x88, 0x8A, 0x90, 0x98, 0xA0}
STRING_CONTAINER = 0xC0
GENERAL_CONTAINER = 0x88
TRAILER_DIRECTORY = 0x90

CHUNK_TYPE, CHUNK_OFFSET, CHUNK_PARENT_SEQNUM = 0x02, 0x04, 0x05
THIS_MASTER_NAME, APPLIED_MASTER_NAME, PAGE_SHAPES = 0x0E, 0x0D, 0x02
PAGE_CHUNK = 0x43
SHAPE_SEQNUM = 0x70

# libmspub hard-codes these page sequence numbers as dummies and never
# emits them (MSPUBParser::getPageTypeBySeqNum).
DUMMY_PAGE_SEQNUMS = {0x10D, 0x110, 0x113, 0x117}


# -- OLE compound file ----------------------------------------------------

def read_stream(path: Path, want: str):
    """Pull one named stream out of a CFB container."""
    data = path.read_bytes()
    if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("not an OLE compound file")
    sector = 1 << struct.unpack_from("<H", data, 30)[0]
    mini_sector = 1 << struct.unpack_from("<H", data, 32)[0]
    fat_count = struct.unpack_from("<I", data, 44)[0]
    dir_start = struct.unpack_from("<I", data, 48)[0]
    mini_cutoff = struct.unpack_from("<I", data, 56)[0]
    mini_fat_start = struct.unpack_from("<I", data, 60)[0]
    difat_start = struct.unpack_from("<I", data, 68)[0]
    difat_count = struct.unpack_from("<I", data, 72)[0]

    def at(sec):
        return (sec + 1) * sector

    fat_sectors = [struct.unpack_from("<I", data, 76 + i * 4)[0] for i in range(min(fat_count, 109))]
    sec = difat_start
    for _ in range(difat_count):
        if sec >= 0xFFFFFFFE:
            break
        base = at(sec)
        for i in range((sector // 4) - 1):
            entry = struct.unpack_from("<I", data, base + i * 4)[0]
            if entry < 0xFFFFFFFE:
                fat_sectors.append(entry)
        sec = struct.unpack_from("<I", data, base + sector - 4)[0]

    fat = []
    for fs in fat_sectors:
        base = at(fs)
        fat += [struct.unpack_from("<I", data, base + i * 4)[0] for i in range(sector // 4)]

    def chain(start):
        out, seen = [], set()
        s = start
        while s < 0xFFFFFFFE and s not in seen and s < len(fat):
            seen.add(s)
            out.append(s)
            s = fat[s]
        return out

    def read_chain(start, size):
        return b"".join(data[at(s):at(s) + sector] for s in chain(start))[:size]

    entries = []
    for s in chain(dir_start):
        base = at(s)
        for i in range(sector // 128):
            e = base + i * 128
            nlen = struct.unpack_from("<H", data, e + 64)[0]
            if nlen < 2:
                continue
            entries.append((
                data[e:e + nlen - 2].decode("utf-16-le", "replace"),
                data[e + 66],
                struct.unpack_from("<I", data, e + 116)[0],
                struct.unpack_from("<I", data, e + 120)[0],
            ))

    root = next(e for e in entries if e[1] == 5)
    target = next((e for e in entries if e[0] == want and e[1] == 2), None)
    if target is None:
        raise KeyError(want)
    _, _, start, size = target
    if size >= mini_cutoff:
        return read_chain(start, size)

    # Small streams live in the mini-FAT, inside the root entry's chain.
    mini_fat = []
    for s in chain(mini_fat_start):
        base = at(s)
        mini_fat += [struct.unpack_from("<I", data, base + i * 4)[0] for i in range(sector // 4)]
    ministore = read_chain(root[2], root[3])
    out, s = b"", start
    seen = set()
    while s < 0xFFFFFFFE and s not in seen:
        seen.add(s)
        out += ministore[s * mini_sector:(s + 1) * mini_sector]
        s = mini_fat[s] if s < len(mini_fat) else 0xFFFFFFFE
    return out[:size]


# -- Publisher blocks -----------------------------------------------------

class Block:
    __slots__ = ("id", "type", "data", "string", "data_offset", "data_length", "end")


def parse_block(buf, pos, skip_hierarchical=False):
    b = Block()
    b.id = buf[pos]
    b.type = buf[pos + 1]
    pos += 2
    b.data_offset = pos
    b.data = 0
    b.string = b""
    if b.type in VARIABLE:
        b.data_length = struct.unpack_from("<I", buf, pos)[0]
        if b.type == STRING_CONTAINER:
            b.string = buf[pos + 4:b.data_offset + b.data_length]
        b.end = b.data_offset + b.data_length
        pos = b.end if (b.type == STRING_CONTAINER or skip_hierarchical) else pos + 4
    else:
        n = FIXED_LENGTH.get(b.type, 0)
        b.data_length = n
        if n in (1, 2, 4):
            b.data = int.from_bytes(buf[pos:pos + n], "little")
        pos += n
        b.end = pos
    return b, pos


def chunk_references(contents):
    """Every chunk in the trailer directory, with its sequence number."""
    trailer_offset = struct.unpack_from("<I", contents, 0x1A)[0]
    pos = trailer_offset + 4                      # skip trailerLength
    # libmspub starts m_lastSeenSeqNum at -1 and increments before use, so
    # the first chunk in the directory is sequence number 0.
    refs, seq = [], -1
    for _ in range(3):
        part, pos = parse_block(contents, pos)
        if part.type != TRAILER_DIRECTORY:
            pos = part.end
            continue
        end = part.data_offset + part.data_length
        inner = part.data_offset + 4
        while inner < end and inner < len(contents):
            block, after = parse_block(contents, inner, skip_hierarchical=True)
            seq += 1
            if block.type == GENERAL_CONTAINER:
                sub = block.data_offset + 4
                kind = offset = parent = None
                while sub < block.end and sub < len(contents):
                    s, sub = parse_block(contents, sub, skip_hierarchical=True)
                    if s.id == CHUNK_TYPE:
                        kind = s.data
                    elif s.id == CHUNK_OFFSET:
                        offset = s.data
                    elif s.id == CHUNK_PARENT_SEQNUM:
                        parent = s.data
                if kind is not None and offset is not None:
                    refs.append({"seq": seq, "type": kind, "offset": offset, "parent": parent})
            inner = block.end
        break
    return refs


def page_master_info(contents, offset):
    """Master role, applied master and shape count for one page chunk.

    A page chunk is not itself a block: it opens with a bare U32 length
    and its blocks follow immediately (MSPUBParser::parsePageChunk).

    The shape count matters for lining these chunks up with the event
    stream. libmspub calls startPage only for pages that have shapes
    (MSPUBCollector::writePage), and skips four seqnums outright as dummy
    pages, so a file holds more page chunks than it has visible pages.
    Filtering on "not a master, not a dummy, has shapes" reproduces
    libmspub's page count exactly across the whole sample set.
    """
    length = struct.unpack_from("<I", contents, offset)[0]
    end = min(offset + length, len(contents))
    pos = offset + 4
    is_master, applied, shapes = False, None, 0
    while pos < end:
        before = pos
        b, pos = parse_block(contents, pos, skip_hierarchical=True)
        if pos <= before:                      # refuse to spin on a bad length
            break
        if b.id == THIS_MASTER_NAME and any(b.string):
            is_master = True
        elif b.id == APPLIED_MASTER_NAME:
            applied = b.data
        elif b.id == PAGE_SHAPES:
            sub = b.data_offset + 4
            while sub < b.end:
                s, after = parse_block(contents, sub, skip_hierarchical=True)
                if after <= sub:
                    break
                if s.type == SHAPE_SEQNUM:
                    shapes += 1
                sub = after
    return is_master, applied, shapes


def report(path: Path):
    contents = read_stream(path, "Contents")
    refs = chunk_references(contents)
    pages = [r for r in refs if r["type"] == PAGE_CHUNK]
    print(f"\n{path.name}")
    print(f"  {len(refs)} chunks, {len(pages)} page chunks")
    if not pages:
        return
    emitted = 0
    for p in pages:
        is_master, applied, shapes = page_master_info(contents, p["offset"])
        dummy = p["seq"] in DUMMY_PAGE_SEQNUMS
        if is_master:
            role = "MASTER"
        elif dummy:
            role = "dummy "
        elif shapes:
            role = "page  "
            emitted += 1
        else:
            role = "empty "
        note = f"applies master seq {applied}" if applied else ""
        print(f"    seq {p['seq']:>3}  {role}  {shapes:>3} shapes  {note}")
    print(f"  -> {emitted} page(s) libmspub will emit, "
          f"{sum(1 for p in pages if page_master_info(contents, p['offset'])[0])} master(s)")


if __name__ == "__main__":
    targets = [Path(a) for a in sys.argv[1:]] or sorted(Path("files").glob("*.pub"))
    for target in targets:
        try:
            report(target)
        except Exception as exc:
            print(f"\n{target.name}\n  failed: {exc.__class__.__name__}: {exc}")
