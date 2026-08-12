"""Structure read straight out of the .pub, alongside libmspub.

libmspub resolves master pages internally and never announces them: it
replays a master's shapes onto each page ahead of that page's own and the
drawing interface sees only ordinary shapes, with nothing saying where
they came from. It also has no concept of fields at all, so a page-number
field arrives as a bare '#'.

Both facts are recoverable from the file itself, so this reads the small
part of it that libmspub walks past. Everything else still comes from
libmspub; this only supplies what it drops.

Nothing here is allowed to fail a conversion. Every entry point returns
None on any difficulty, and the converter carries on exactly as it did
before -- the information is an improvement on the output, not a
prerequisite for it.

Format, from libmspub 0.1.5 MSPUBParser.cpp:

    Contents stream
      0x1a  U32 trailerOffset
      at trailerOffset: U32 length, then up to 3 blocks; the one of type
      TRAILER_DIRECTORY (0x90) lists chunk references, each a
      GENERAL_CONTAINER (0x88) giving CHUNK_TYPE, CHUNK_OFFSET and
      optionally CHUNK_PARENT_SEQNUM. Sequence numbers follow position.

    Block
      U8 id, U8 type, then a payload sized by the type. The types listed
      in _VARIABLE begin with a U32 length that includes itself.

    Page chunk (chunk type 0x43)
      bare U32 length, then blocks:
        0x0E  non-empty string -> this page is a master
        0x0D  seqnum of the master this page applies
        0x02  the page's own shape list
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import logsetup

log = logsetup.get_logger("pubfile")

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_FIXED_LENGTH = {
    0x78: 0, 0x05: 0, 0x08: 0, 0x0A: 0,
    0x10: 2, 0x12: 2, 0x18: 2, 0x1A: 2, 0x07: 2,
    0x20: 4, 0x22: 4, 0x58: 4, 0x68: 4, 0x70: 4, 0xB8: 4,
    0x28: 8, 0x38: 16, 0x48: 24,
}
_VARIABLE = {0xC0, 0x80, 0x82, 0x88, 0x8A, 0x90, 0x98, 0xA0}
_STRING_CONTAINER = 0xC0
_GENERAL_CONTAINER = 0x88
_TRAILER_DIRECTORY = 0x90

_CHUNK_TYPE, _CHUNK_OFFSET = 0x02, 0x04
_THIS_MASTER_NAME, _APPLIED_MASTER_NAME, _PAGE_SHAPES = 0x0E, 0x0D, 0x02
_SHAPE_SEQNUM = 0x70
_PAGE_CHUNK = 0x43

# Sequence numbers libmspub hard-codes as dummy pages and never emits
# (MSPUBParser::getPageTypeBySeqNum).
_DUMMY_PAGE_SEQNUMS = frozenset({0x10D, 0x110, 0x113, 0x117})

# Quill chunk types libmspub reads. TOKN, the field table, is not among
# them, and its presence is what tells us the document has fields at all.
_TOKEN_CHUNK = "TOKN"


@dataclass
class PageStructure:
    seq: int
    is_master: bool = False
    applied_master: Optional[int] = None
    shape_count: int = 0


@dataclass
class FileStructure:
    """What the file says that libmspub does not pass on."""

    #: Pages in the order libmspub emits them, so index i lines up with
    #: document.pages[i].
    pages: List[PageStructure] = field(default_factory=list)
    masters: Dict[int, PageStructure] = field(default_factory=dict)
    #: True when the document contains at least one field of any kind. A
    #: document with none cannot contain a page-number field, so every
    #: '#' in it is literal text.
    has_fields: bool = False

    def master_for(self, page_index: int) -> Optional[PageStructure]:
        if not 0 <= page_index < len(self.pages):
            return None
        applied = self.pages[page_index].applied_master
        return self.masters.get(applied) if applied is not None else None


# -- OLE compound file ----------------------------------------------------

def _read_stream(data: bytes, want: str) -> Optional[bytes]:
    """Pull one named stream out of a CFB container."""
    if data[:8] != _OLE_MAGIC:
        return None
    sector = 1 << struct.unpack_from("<H", data, 30)[0]
    mini_sector = 1 << struct.unpack_from("<H", data, 32)[0]
    fat_count = struct.unpack_from("<I", data, 44)[0]
    dir_start = struct.unpack_from("<I", data, 48)[0]
    mini_cutoff = struct.unpack_from("<I", data, 56)[0]
    mini_fat_start = struct.unpack_from("<I", data, 60)[0]
    difat_start = struct.unpack_from("<I", data, 68)[0]
    difat_count = struct.unpack_from("<I", data, 72)[0]

    def at(sec: int) -> int:
        return (sec + 1) * sector

    fat_sectors = [
        struct.unpack_from("<I", data, 76 + i * 4)[0]
        for i in range(min(fat_count, 109))
    ]
    sec = difat_start
    for _ in range(difat_count):
        if sec >= 0xFFFFFFFE or at(sec) + sector > len(data):
            break
        base = at(sec)
        for i in range((sector // 4) - 1):
            entry = struct.unpack_from("<I", data, base + i * 4)[0]
            if entry < 0xFFFFFFFE:
                fat_sectors.append(entry)
        sec = struct.unpack_from("<I", data, base + sector - 4)[0]

    fat: List[int] = []
    for fat_sector in fat_sectors:
        base = at(fat_sector)
        if base + sector > len(data):
            break
        fat += [struct.unpack_from("<I", data, base + i * 4)[0] for i in range(sector // 4)]

    def chain(start: int) -> List[int]:
        out, seen, s = [], set(), start
        while s < 0xFFFFFFFE and s not in seen and s < len(fat):
            seen.add(s)
            out.append(s)
            s = fat[s]
        return out

    def read_chain(start: int, size: int) -> bytes:
        return b"".join(data[at(s):at(s) + sector] for s in chain(start))[:size]

    entries = []
    for s in chain(dir_start):
        base = at(s)
        if base + sector > len(data):
            break
        for i in range(sector // 128):
            entry = base + i * 128
            name_length = struct.unpack_from("<H", data, entry + 64)[0]
            if name_length < 2:
                continue
            entries.append((
                data[entry:entry + name_length - 2].decode("utf-16-le", "replace"),
                data[entry + 66],
                struct.unpack_from("<I", data, entry + 116)[0],
                struct.unpack_from("<I", data, entry + 120)[0],
            ))

    target = next((e for e in entries if e[0] == want and e[1] == 2), None)
    if target is None:
        return None
    _, _, start, size = target
    if size >= mini_cutoff:
        return read_chain(start, size)

    root = next((e for e in entries if e[1] == 5), None)
    if root is None:
        return None
    mini_fat: List[int] = []
    for s in chain(mini_fat_start):
        base = at(s)
        if base + sector > len(data):
            break
        mini_fat += [struct.unpack_from("<I", data, base + i * 4)[0] for i in range(sector // 4)]
    ministore = read_chain(root[2], root[3])
    out, s, seen = b"", start, set()
    while s < 0xFFFFFFFE and s not in seen:
        seen.add(s)
        out += ministore[s * mini_sector:(s + 1) * mini_sector]
        s = mini_fat[s] if s < len(mini_fat) else 0xFFFFFFFE
    return out[:size]


# -- Publisher blocks -----------------------------------------------------

class _Block:
    __slots__ = ("id", "type", "data", "string", "data_offset", "data_length", "end")


def _parse_block(buf: bytes, pos: int, skip_hierarchical: bool = False):
    block = _Block()
    block.id = buf[pos]
    block.type = buf[pos + 1]
    pos += 2
    block.data_offset = pos
    block.data = 0
    block.string = b""
    if block.type in _VARIABLE:
        block.data_length = struct.unpack_from("<I", buf, pos)[0]
        if block.type == _STRING_CONTAINER:
            block.string = buf[pos + 4:block.data_offset + block.data_length]
        block.end = block.data_offset + block.data_length
        pos = block.end if (block.type == _STRING_CONTAINER or skip_hierarchical) else pos + 4
    else:
        size = _FIXED_LENGTH.get(block.type, 0)
        block.data_length = size
        if size in (1, 2, 4):
            block.data = int.from_bytes(buf[pos:pos + size], "little")
        pos += size
        block.end = pos
    return block, pos


def _chunk_references(contents: bytes):
    trailer_offset = struct.unpack_from("<I", contents, 0x1A)[0]
    pos = trailer_offset + 4
    refs, seq = [], -1
    for _ in range(3):
        part, pos = _parse_block(contents, pos)
        if part.type != _TRAILER_DIRECTORY:
            pos = part.end
            continue
        end = min(part.data_offset + part.data_length, len(contents))
        inner = part.data_offset + 4
        while inner < end:
            block, _after = _parse_block(contents, inner, skip_hierarchical=True)
            seq += 1
            if block.type == _GENERAL_CONTAINER:
                sub = block.data_offset + 4
                kind = offset = None
                while sub < block.end and sub < len(contents):
                    before = sub
                    entry, sub = _parse_block(contents, sub, skip_hierarchical=True)
                    if sub <= before:
                        break
                    if entry.id == _CHUNK_TYPE:
                        kind = entry.data
                    elif entry.id == _CHUNK_OFFSET:
                        offset = entry.data
                if kind is not None and offset is not None:
                    refs.append((seq, kind, offset))
            if block.end <= inner:
                break
            inner = block.end
        break
    return refs


def _page_structure(contents: bytes, seq: int, offset: int) -> PageStructure:
    page = PageStructure(seq=seq)
    length = struct.unpack_from("<I", contents, offset)[0]
    end = min(offset + length, len(contents))
    pos = offset + 4
    while pos < end:
        before = pos
        block, pos = _parse_block(contents, pos, skip_hierarchical=True)
        if pos <= before:
            break
        if block.id == _THIS_MASTER_NAME and any(block.string):
            page.is_master = True
        elif block.id == _APPLIED_MASTER_NAME:
            page.applied_master = block.data
        elif block.id == _PAGE_SHAPES:
            sub = block.data_offset + 4
            while sub < block.end:
                entry, after = _parse_block(contents, sub, skip_hierarchical=True)
                if after <= sub:
                    break
                if entry.type == _SHAPE_SEQNUM:
                    page.shape_count += 1
                sub = after
    return page


def _has_field_table(data: bytes) -> bool:
    """True when the Quill stream carries a TOKN chunk of any kind."""
    quill = _read_stream(data, "CONTENTS")
    if not quill:
        return False
    offset, seen = 0x18, set()
    while offset != 0xFFFFFFFF and offset + 8 <= len(quill) and offset not in seen:
        seen.add(offset)
        count = struct.unpack_from("<H", quill, offset + 2)[0]
        nxt = struct.unpack_from("<I", quill, offset + 4)[0]
        pos = offset + 8
        for _ in range(count):
            if pos + 24 > len(quill):
                break
            if quill[pos + 2:pos + 6].decode("ascii", "replace") == _TOKEN_CHUNK:
                return True
            pos += 24
        offset = nxt
    return False


def read_structure(source: Path) -> Optional[FileStructure]:
    """Master pages and field presence, or None if anything is unreadable."""
    try:
        data = Path(source).read_bytes()
        contents = _read_stream(data, "Contents")
        if not contents:
            return None

        structure = FileStructure(has_fields=_has_field_table(data))
        for seq, kind, offset in _chunk_references(contents):
            if kind != _PAGE_CHUNK:
                continue
            page = _page_structure(contents, seq, offset)
            if page.is_master:
                structure.masters[seq] = page
            elif seq not in _DUMMY_PAGE_SEQNUMS and page.shape_count:
                # libmspub calls startPage only for pages carrying shapes,
                # so this filter is what keeps the list aligned with the
                # event stream.
                structure.pages.append(page)
        return structure
    except Exception as exc:  # a damaged file must not fail the conversion
        log.info("could not read structure from %s: %s", source, exc)
        return None
