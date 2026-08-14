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

    Table chunk (chunk type 0x10)
      bare U32 length, then blocks:
        0x66  row count            0x67  column count
        0x68  total width (EMU)    0x69  total height (EMU)
        0x6B  seqnum of this table's cells chunk
        0x6D  array of one container per column and then per row, each
              giving 0x01 the running offset and 0x02 the size, in EMU

    Cells chunk (chunk type 0x63)
      bare U32 length, then blocks:
        0x01  cell count
        0x02  array of one container per cell, each giving
                0x01/0x02  first and last row      0x03/0x04  ditto columns
                0x0A-0x0D  left, top, right and bottom inset, in EMU
                0x07       1 or 2, uniform across a table -- unidentified
                0x09/0x0E  cached extents -- unidentified
              A field left out is absent, not defaulted: one table in the
              corpus writes 0x0A-0x0D as 36576 EMU on every cell, which is
              Publisher's own 0.04in default, so the writer states the
              value it means and omission is zero.

    EscherStm stream, from libmspub 0.1.5 MSPUBParser.cpp again
      OfficeArt records: U16 version|instance<<4, U16 type, U32 length.
      A version of 0xF means a container, whose children follow inline.
      Publisher differs from OfficeArt in two ways, both of which desync a
      naive walk: a DGG (0xF000) or DG (0xF002) container is followed by
      four bytes of tail, and a CLIENT_ANCHOR (0xF010) or CLIENT_DATA
      (0xF011) repeats its own length before its contents.

        0xF004  shape container, holding
          0xF010  anchor, an (U16 id, U32 value) list:
                    0x2001-0x2004 xs, ys, xe, ye in EMU, measured from
                    the centre of the page (Coordinate::getXIn)
          0xF00B/0xF121/0xF122  property tables: `instance` six-byte
                    entries of (U16 opid, U32 value), then the payload of
                    every entry whose opid has 0x8000 set, in order. The
                    property id is the low fourteen bits.
                      0x0004  rotation, degrees in 16.16 fixed point
                      0x00C0  WordArt text, UTF-16LE
                      0x00C3  WordArt point size, 16.16 fixed point
                      0x00C5  WordArt font name, UTF-16LE
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

_TABLE_CHUNK, _CELLS_CHUNK = 0x10, 0x63
_TABLE_ROW_COUNT, _TABLE_COLUMN_COUNT = 0x66, 0x67
_TABLE_CELLS_SEQNUM = 0x6B
_TABLE_ROWCOL_ARRAY, _TABLE_ROWCOL_SIZE = 0x6D, 0x02
_CELL_ARRAY = 0x02
# Every entry of an array carries id 0, which is how libmspub tells an
# entry from anything else the array happens to hold.
_ARRAY_ENTRY = 0x00
_CELL_FIRST_ROW, _CELL_FIRST_COLUMN = 0x01, 0x03
# Left, top, right, bottom -- the order the same file format uses for a
# text frame's own margins, where Escher numbers them 0x81 to 0x84. Only
# one table in the corpus sets two sides differently, so the corpus cannot
# tell this apart from left/right/top/bottom on its own.
_CELL_INSETS = (0x0A, 0x0B, 0x0C, 0x0D)
_EMU_PER_POINT = 12700.0

_ESCHER_STREAM = "EscherStm"
_SHAPE_CONTAINER = 0xF004
_CLIENT_ANCHOR = 0xF010
_PROPERTY_RECORDS = frozenset({0xF00B, 0xF121, 0xF122})
# Publisher's own departures from OfficeArt's record layout.
_ESCHER_TAIL = {0xF000: 4, 0xF002: 4}
_ESCHER_EXTRA_HEADER = {0xF010: 4, 0xF011: 4}
_ANCHOR_SIDES = (0x2001, 0x2002, 0x2003, 0x2004)
_PROP_ROTATION = 0x0004
_PROP_WORDART_TEXT, _PROP_WORDART_SIZE, _PROP_WORDART_FONT = 0x00C0, 0x00C3, 0x00C5
_FIXED_16_16 = 65536.0
# WordArt stretches its glyphs to fill the shape, so the band is not a
# fixed multiple of the size the file states: across the 39 sized shapes in
# the corpus it runs 1.02 to 1.59, averaging this. Only ever used for a
# shape that states no size at all, and reported when it is.
_BAND_TO_SIZE = 1.33

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
class TableStructure:
    """One table's cell insets, in points, keyed by first row and column.

    First row and column is what libmspub reports as a cell's position, so
    a spanning cell is keyed by the corner it starts in either way.
    """

    insets: Dict[tuple, tuple] = field(default_factory=dict)


@dataclass
class WordArt:
    """A WordArt shape: its words, and the band they are stretched into.

    `centre_x`/`centre_y` are measured from the centre of the page, which
    is the only frame of reference the Escher stream uses, and `width` and
    `height` describe the band before `rotation` turns it.
    """

    text: str
    font: Optional[str] = None
    size: Optional[float] = None
    centre_x: float = 0.0
    centre_y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    rotation: float = 0.0
    #: True when the file stated no size and one was taken from the band.
    fitted: bool = False


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
    #: Tables by grid signature. Nothing in the event stream identifies
    #: which chunk a table came from, so its own measurements are the only
    #: way back to it; a signature two tables share maps to None, because
    #: then there is no telling which of them is on screen.
    tables: Dict[tuple, Optional[TableStructure]] = field(default_factory=dict)
    #: Every WordArt shape in the file. libmspub reports neither their text
    #: nor an id for them, so they are matched by where they sit.
    wordart: List[WordArt] = field(default_factory=list)

    def wordart_near(
        self, centre_x: float, centre_y: float, tolerance: float = 0.5
    ) -> Optional[WordArt]:
        """The one WordArt shape centred here, if exactly one is.

        Both sides measure the same EMU by different routes, so they agree
        to a fraction of a point rather than exactly; a nearest match
        inside a tolerance avoids turning that into a rounding cliff. Two
        candidates equally close is an ambiguity, not a match.
        """
        near = [
            art for art in self.wordart
            if abs(art.centre_x - centre_x) <= tolerance
            and abs(art.centre_y - centre_y) <= tolerance
        ]
        return near[0] if len(near) == 1 else None

    def master_for(self, page_index: int) -> Optional[PageStructure]:
        if not 0 <= page_index < len(self.pages):
            return None
        applied = self.pages[page_index].applied_master
        return self.masters.get(applied) if applied is not None else None

    def cell_insets(
        self, column_widths: List[float], row_heights: List[float]
    ) -> Optional[Dict[tuple, tuple]]:
        """Insets by (row, column) for the table with this grid, if known."""
        found = self.tables.get(table_signature(column_widths, row_heights))
        return found.insets if found is not None else None


def table_signature(column_widths: List[float], row_heights: List[float]) -> tuple:
    """Identify a table by the grid it draws, in points.

    Both halves measure the same EMU, but libmspub's arrive by way of
    four-decimal inches, so they agree to about a fortieth of a point and
    no further. A tenth is finer than any two tables in the corpus are
    apart and coarse enough to survive that rounding.
    """
    return (
        tuple(round(width, 1) for width in column_widths),
        tuple(round(height, 1) for height in row_heights),
    )


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


def _blocks(buf: bytes, pos: int, end: int):
    """Every block between two offsets, stopping on anything malformed.

    A block whose payload is cut off by the end of the file ends the walk
    rather than the read: the caller has usually collected something worth
    keeping by then, and a damaged table must not cost a document its
    master pages.
    """
    end = min(end, len(buf))
    while pos + 2 <= end:
        before = pos
        try:
            block, pos = _parse_block(buf, pos, skip_hierarchical=True)
        except struct.error:
            return
        if pos <= before:
            return
        yield block


def _chunk_blocks(contents: bytes, offset: int):
    """A chunk's own blocks. Every chunk opens with a U32 covering itself."""
    if offset < 0 or offset + 4 > len(contents):
        return iter(())
    length = struct.unpack_from("<I", contents, offset)[0]
    return _blocks(contents, offset + 4, offset + length)


def _children(contents: bytes, block: "_Block"):
    """The blocks inside a container, whose payload also opens with a U32."""
    return _blocks(contents, block.data_offset + 4, block.end)


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
    for block in _chunk_blocks(contents, offset):
        if block.id == _THIS_MASTER_NAME and any(block.string):
            page.is_master = True
        elif block.id == _APPLIED_MASTER_NAME:
            page.applied_master = block.data
        elif block.id == _PAGE_SHAPES:
            page.shape_count += sum(
                1
                for entry in _children(contents, block)
                if entry.type == _SHAPE_SEQNUM
            )
    return page


def _table_grid(contents: bytes, offset: int):
    """One table chunk's fields and its grid, in points.

    The row/column array runs every column and then every row, the same
    split libmspub makes, and both are sizes rather than positions.
    """
    fields: Dict[int, int] = {}
    sizes: List[int] = []
    for block in _chunk_blocks(contents, offset):
        if block.id == _TABLE_ROWCOL_ARRAY:
            for entry in _children(contents, block):
                if entry.id != _ARRAY_ENTRY:
                    continue
                sizes += [
                    sub.data
                    for sub in _children(contents, entry)
                    if sub.id == _TABLE_ROWCOL_SIZE
                ]
        else:
            fields[block.id] = block.data

    rows = fields.get(_TABLE_ROW_COUNT, 0)
    columns = fields.get(_TABLE_COLUMN_COUNT, 0)
    if not rows or not columns or len(sizes) < rows + columns:
        return None, None
    widths = [size / _EMU_PER_POINT for size in sizes[:columns]]
    heights = [size / _EMU_PER_POINT for size in sizes[columns:columns + rows]]
    return fields, table_signature(widths, heights)


def _table_cells(contents: bytes, offset: int) -> TableStructure:
    """One cells chunk: what each cell says about its own insets."""
    table = TableStructure()
    for block in _chunk_blocks(contents, offset):
        if block.id != _CELL_ARRAY:
            continue
        for record in _children(contents, block):
            if record.id != _ARRAY_ENTRY:
                continue
            cell = {sub.id: sub.data for sub in _children(contents, record)}
            position = (
                cell.get(_CELL_FIRST_ROW, 0),
                cell.get(_CELL_FIRST_COLUMN, 0),
            )
            table.insets[position] = tuple(
                cell.get(side, 0) / _EMU_PER_POINT for side in _CELL_INSETS
            )
    return table


def _read_tables(contents: bytes, refs) -> Dict[tuple, Optional[TableStructure]]:
    """Cell insets for every table in the file, keyed by grid signature."""
    cells_at = {seq: offset for seq, kind, offset in refs if kind == _CELLS_CHUNK}
    tables: Dict[tuple, Optional[TableStructure]] = {}
    for _seq, kind, offset in refs:
        if kind != _TABLE_CHUNK:
            continue
        fields, signature = _table_grid(contents, offset)
        if signature is None:
            continue
        cells_offset = cells_at.get(fields.get(_TABLE_CELLS_SEQNUM))
        if cells_offset is None:
            continue
        table = _table_cells(contents, cells_offset)
        # Two tables drawing the same grid are only usable while they agree
        # about their cells; where they differ, neither is.
        if tables.setdefault(signature, table) != table:
            tables[signature] = None
    return tables


# -- Escher (OfficeArt) ---------------------------------------------------

def _escher_records(buf: bytes, start: int, end: int):
    """Every record at one level, as (version, instance, type, from, to)."""
    pos = start
    while pos + 8 <= end:
        version_instance, rec_type, length = struct.unpack_from("<HHI", buf, pos)
        # The length covers the repeated length of the records that carry
        # one, so the contents end is measured from the header, not the body.
        body = pos + 8 + _ESCHER_EXTRA_HEADER.get(rec_type, 0)
        contents_end = min(pos + 8 + length, end)
        yield version_instance & 0x0F, version_instance >> 4, rec_type, body, contents_end
        if rec_type == 0 and length == 0:
            return  # padding rather than a record, and so is everything after
        pos = contents_end + _ESCHER_TAIL.get(rec_type, 0)


def _escher_shapes(buf: bytes, start: int, end: int):
    """Every shape container, at whatever depth it sits."""
    for version, _instance, rec_type, body, contents_end in _escher_records(
        buf, start, end
    ):
        if rec_type == _SHAPE_CONTAINER:
            yield body, contents_end
        elif version == 0x0F:
            yield from _escher_shapes(buf, body, contents_end)


def _escher_values(buf: bytes, start: int, end: int) -> Dict[int, int]:
    """An (id, value) list, which is how every non-property record reads."""
    out: Dict[int, int] = {}
    pos = start
    while pos + 6 <= end:
        key, value = struct.unpack_from("<HI", buf, pos)
        out[key] = value
        pos += 6
    return out


def _escher_properties(buf: bytes, start: int, end: int, count: int) -> dict:
    """A property table: the entries, then the payload of the complex ones.

    An entry's top bit flags a complex value and the next one a blip
    reference, so the property id is the low fourteen bits. libmspub keeps
    the flags inside its own constants instead, which is why its
    FILL_SHADE_COMPLEX reads 0xC197 rather than 0x0197.
    """
    entries, pos = [], start
    for _ in range(count):
        if pos + 6 > end:
            break
        entries.append(struct.unpack_from("<HI", buf, pos))
        pos += 6
    out: dict = {}
    for opid, value in entries:
        if opid & 0x8000:
            out[opid & 0x3FFF] = buf[pos:pos + value]
            pos += value
        else:
            out[opid & 0x3FFF] = value
    return out


def _signed(value: int) -> int:
    return value - 0x100000000 if value & 0x80000000 else value


def _escher_text(blob) -> Optional[str]:
    if not isinstance(blob, bytes) or not blob:
        return None
    text = blob.decode("utf-16-le", "replace").rstrip("\x00").strip()
    return text or None


def _read_wordart(data: bytes) -> List[WordArt]:
    """Every WordArt shape in the file, with its words and its band.

    libmspub reads the Escher stream for a shape's geometry and fill and
    walks past this: WordArt text, font and size are properties it has no
    constants for, so a Publisher headline set in WordArt arrives as an
    empty frame beside a pair of guide edges that enclose no area. Both
    halves are in here.
    """
    escher = _read_stream(data, _ESCHER_STREAM)
    return _wordart_shapes(escher) if escher else []


def _wordart_shapes(escher: bytes) -> List[WordArt]:
    """The WordArt shapes in an Escher stream."""
    found: List[WordArt] = []
    for body, end in _escher_shapes(escher, 0, len(escher)):
        text = font = None
        size = rotation = None
        box = None
        for _version, instance, rec_type, sub_body, sub_end in _escher_records(
            escher, body, end
        ):
            if rec_type in _PROPERTY_RECORDS:
                props = _escher_properties(escher, sub_body, sub_end, instance)
                text = _escher_text(props.get(_PROP_WORDART_TEXT)) or text
                font = _escher_text(props.get(_PROP_WORDART_FONT)) or font
                if isinstance(props.get(_PROP_WORDART_SIZE), int):
                    size = props[_PROP_WORDART_SIZE] / _FIXED_16_16
                if isinstance(props.get(_PROP_ROTATION), int):
                    rotation = _signed(props[_PROP_ROTATION]) / _FIXED_16_16
            elif rec_type == _CLIENT_ANCHOR:
                anchor = _escher_values(escher, sub_body, sub_end)
                if all(side in anchor for side in _ANCHOR_SIDES):
                    box = [
                        _signed(anchor[side]) / _EMU_PER_POINT
                        for side in _ANCHOR_SIDES
                    ]

        # A shape with no anchor cannot be placed, and one with no text is
        # an ordinary shape libmspub has already reported.
        if text is None or box is None:
            continue
        width, height = box[2] - box[0], box[3] - box[1]
        if width <= 0 or height <= 0:
            continue
        fitted = size is None
        found.append(
            WordArt(
                text=text,
                font=font,
                size=(height / _BAND_TO_SIZE) if fitted else size,
                centre_x=(box[0] + box[2]) / 2.0,
                centre_y=(box[1] + box[3]) / 2.0,
                width=width,
                height=height,
                rotation=rotation or 0.0,
                fitted=fitted,
            )
        )
    return found


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
    """Masters, field presence and cell insets, or None if unreadable."""
    try:
        data = Path(source).read_bytes()
        contents = _read_stream(data, "Contents")
        if not contents:
            return None

        refs = _chunk_references(contents)
        structure = FileStructure(
            has_fields=_has_field_table(data),
            tables=_read_tables(contents, refs),
            wordart=_read_wordart(data),
        )
        for seq, kind, offset in refs:
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
