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

Streams are addressed by their full path inside the compound file, not by
name: an embedded OLE object brings its own storage, and one file in the
corpus holds two streams called CONTENTS.

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
        0x02  the page's own shape list, one 0x70 entry per shape holding
              that shape's seqnum. The lists are disjoint across pages --
              519 shapes across the corpus, none listed twice -- so this
              is where every shape's page is stated. It is *not* stated in
              libmspub's page order: the two are a permutation of one
              another in every newsletter in the corpus.

    Shape chunk (chunk type 0x01)
      bare U32 length, then blocks:
        0x27  the story this shape holds. Shapes sharing one are the
              frames Publisher linked, which is the whole of what
              librevenge's drawing interface cannot say.
        0x28  this shape's place in that story, 1 upwards; the head of a
              chain leaves it out, the same way an inset that is zero is
              left out. A story only one shape holds is not a chain, which
              is what keeps a page-number footer -- one master shape
              replayed onto every page -- from reading as a run of frames.

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
                0x09       cached height of the cell's laid-out text: it
                             grows with the wrapping and is what makes the
                             row arithmetic below come out
                0x0E       cached extent, 9pt or 18pt -- unidentified
              A cell states an inset exactly when it has one, so a side
              left out is zero rather than a default. Three readings agree
              on that. **No stated side is ever zero** -- 3289 stated
              across the corpus against 1751 left out, and not one of the
              3289 reads 0. The tables that mean Publisher's own 0.04in
              default *write it*, all four sides, 36576 EMU each. And the
              geometry says the same independently: on the 454 rows grown
              to fit their text, the row's height less its top inset and
              its 0x09 text height leaves a residual of about zero, where
              a 0.04in bottom inset would leave 2.88pt.
              Omission is per-side, not truncation of the trailing ones:
              cells state 0x0B while leaving 0x0A out.

    EscherStm stream, from libmspub 0.1.5 MSPUBParser.cpp again
      OfficeArt records: U16 version|instance<<4, U16 type, U32 length.
      A version of 0xF means a container, whose children follow inline.
      Publisher differs from OfficeArt in two ways, both of which desync a
      naive walk: a DGG (0xF000) or DG (0xF002) container is followed by
      four bytes of tail, and a CLIENT_ANCHOR (0xF010) or CLIENT_DATA
      (0xF011) repeats its own length before its contents.

        0xF004  shape container, holding
          0xF00A  the shape itself, whose `instance` is its type: 136 is
                    unwarped WordArt and 137-175 the warped presets
                    (MS-ODRAW's MSO_SPT)
          0xF010  anchor, an (U16 id, U32 value) list:
                    0x2001-0x2004 xs, ys, xe, ye in EMU, measured from
                    the centre of the page (Coordinate::getXIn)
          0xF011  client data, the same (U16 id, U32 value) layout:
                    0x6801 this shape's seqnum, which is the number the
                    page chunks list. It is the only thing tying an Escher
                    shape to the rest of the file.
          0xF00B/0xF121/0xF122  property tables: `instance` six-byte
                    entries of (U16 opid, U32 value), then the payload of
                    every entry whose opid has 0x8000 set, in order. The
                    property id is the low fourteen bits.
                      0x0004  rotation, degrees in 16.16 fixed point
                      0x00C0  WordArt text, UTF-16LE
                      0x00C3  WordArt point size, 16.16 fixed point
                      0x00C4  WordArt character spacing, 16.16 fixed
                              point, as a multiple of normal
                      0x00C5  WordArt font name, UTF-16LE
                      0x00FF  WordArt's sixteen booleans in one word:
                              the low half their values, the high half
                              which of them the file states at all
                      0x0180  fill type; 4-8 are the shaded ones
                      0x0181/0x0183  fill and fill-back colour
                      0x0197  shade list: U16 count, four bytes, then a
                              colour and a 16.16 position per waypoint
                    A colour is BGR unless its top byte is 0x08, which
                    indexes the palette chunk, or 0x10, an intensity
                    change of the colour it is stated against.

    Quill/QuillSub/CONTENTS stream, from MSPUBParser::parseQuill
      0x18  U16 (unused), U16 chunk count, U32 offset of the next list;
            then one 24-byte reference per chunk: U16 (0x18), 4-char name,
            U16 id, 4 bytes, 4-char second name, U32 offset, U32 length.

        TEXT  the document's words, UTF-16LE, every story end to end
        FDPP  paragraph formatting: U16 count, 6 bytes, then that many U32
              offsets -- the stream offset each paragraph ends at, in text
              order -- and that many U16 offsets into this chunk, each the
              position of a paragraph style
        TOKN  the field table, which libmspub does not read
        SGP   a bare U32 length and then at most one block, id 0x00 type
              0x22, holding the document's default tab interval in EMU --
              the grid every tab with no stop of its own lands on

      A paragraph style is a bare U32 length and then blocks, of which
      0x32 holds the tab stops:
        0x32  container, holding
          0x28  array of one GENERAL_CONTAINER per stop, each giving
                  0x00  position, signed EMU from the frame's text edge
                  0x01  alignment in its low byte, absent for a left tab
      libmspub reads all of this into ParagraphStyle::m_tabStopsInEmu and
      then never uses the member, so the stops never reach librevenge.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
_SHAPE_CHUNK = 0x01
_SHAPE_STORY_ID, _SHAPE_CHAIN_INDEX = 0x27, 0x28
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

_CONTENTS_STREAM = ("Contents",)
_ESCHER_STREAM = ("Escher", "EscherStm")
_QUILL_STREAM = ("Quill", "QuillSub", "CONTENTS")
_SHAPE_CONTAINER = 0xF004
_CLIENT_ANCHOR = 0xF010
# Client data, which for Publisher holds one thing worth having: the
# shape's own seqnum, the number its page chunk lists it by.
_CLIENT_DATA = 0xF011
_CLIENT_DATA_SEQNUM = 0x6801
_PROPERTY_RECORDS = frozenset({0xF00B, 0xF121, 0xF122})
# Publisher's own departures from OfficeArt's record layout.
_ESCHER_TAIL = {0xF000: 4, 0xF002: 4}
_ESCHER_EXTRA_HEADER = {0xF010: 4, 0xF011: 4}
_ANCHOR_SIDES = (0x2001, 0x2002, 0x2003, 0x2004)
_PROP_ROTATION = 0x0004
_PROP_WORDART_TEXT, _PROP_WORDART_SIZE, _PROP_WORDART_FONT = 0x00C0, 0x00C3, 0x00C5
_PROP_WORDART_SPACING = 0x00C4

# WordArt's character formatting, which is not the shape's: bold, italic
# and the rest are booleans of its own, packed into one property. MS-ODRAW
# writes a boolean set with the *highest* id in the group in the low bit,
# so bit n is property 0xFF - n -- 0xFF strikethrough, 0xFE small caps,
# 0xFD shadow, 0xFC underline, 0xFB italic, 0xFA bold, and the fitting
# flags above those. The top half of the word says which bits the file
# states at all; a bit it leaves out is not false, it is unstated.
_PROP_WORDART_BOOLS = 0x00FF
_WORDART_STRIKETHROUGH, _WORDART_UNDERLINE = 0x00, 0x03
_WORDART_ITALIC, _WORDART_BOLD = 0x04, 0x05

# The shape record, whose `instance` is the shape type. 136 is
# msosptTextPlainText -- WordArt that is not warped at all, only fitted to
# its band -- and 137 to 175 are the presets that bend the words. 47 of
# the corpus's 48 WordArt shapes are 136, which is why a headline arriving
# as straight text is usually no loss and worth saying apart from one that
# is.
_SHAPE_RECORD = 0xF00A
_WORDART_PLAIN = 136
_WORDART_WARPS = {
    137: "stop sign", 138: "triangle up", 139: "triangle down",
    140: "chevron up", 141: "chevron down", 142: "ring inside",
    143: "ring outside", 144: "arch up curve", 145: "arch down curve",
    146: "circle curve", 147: "button curve", 148: "arch up pour",
    149: "arch down pour", 150: "circle pour", 151: "button pour",
    152: "curve up", 153: "curve down", 154: "cascade up",
    155: "cascade down", 156: "wave 1", 157: "wave 2", 158: "wave 3",
    159: "wave 4", 160: "inflate", 161: "deflate", 162: "inflate bottom",
    163: "deflate bottom", 164: "inflate top", 165: "deflate top",
    166: "deflate inflate", 167: "deflate inflate deflate",
    168: "fade right", 169: "fade left", 170: "fade up", 171: "fade down",
    172: "slant up", 173: "slant down", 174: "can up", 175: "can down",
}

# A gradient fill, and the colours it ramps between. libmspub reads all of
# these (EscherFieldIds.h) but builds the ramp from the shade list *alone*
# when there is one, so a Publisher gradient stated as "these two colours,
# with a waypoint in between" reaches librevenge as the waypoint by itself.
_PROP_FILL_TYPE, _PROP_FILL_COLOR, _PROP_FILL_BACK = 0x0180, 0x0181, 0x0183
_PROP_FILL_SHADE = 0x0197
_PROP_FILL_ANGLE, _PROP_FILL_FOCUS = 0x018B, 0x018C
# Two angles the file states ninety degrees out, corrected by name in
# MSPUBParser::getShapeFill -- "totally arbitrary", as its comment says.
_FILL_ANGLE_FIXUPS = {-135: -45, -45: 225}
# Where the ramp starts from: the fill colour, or the fill-back colour at
# the far end. Any other value folds the ramp back on itself, and no shape
# in the corpus states one of those together with a shade list, so there
# is nothing to check a reconstruction of it against.
_KNOWN_FILL_FOCUS = frozenset({0, 100})
# fillType values that mean a shaded ramp rather than a solid or a bitmap,
# from MS-ODRAW's MSO_FILLTYPE: shade, shadeCenter, shadeShape, shadeScale
# and shadeTitle.
_GRADIENT_FILL_TYPES = frozenset({4, 5, 6, 7, 8})
_PALETTE_CHUNK = 0x5C
_PALETTE_ENTRY_COLOR = 0x01
# How a colour reference resolves, from ColorReference::getRealColor: the
# top byte says what the rest means.
_COLOR_FROM_PALETTE = 0x08
_COLOR_CHANGE_INTENSITY = 0x10
_INTENSITY_BLACK_BASE, _INTENSITY_WHITE_BASE = 0x01, 0x02
_FIXED_16_16 = 65536.0
# WordArt stretches its glyphs to fill the shape, so the band is not a
# fixed multiple of the size the file states: across the 39 sized shapes in
# the corpus it runs 1.02 to 1.59, averaging this. Measured per line of the
# headline, since a band holds as many lines as the words are set on. Only
# ever used for a shape that states no size at all, and reported when it is.
_BAND_TO_SIZE = 1.33

# Sequence numbers libmspub hard-codes as dummy pages and never emits
# (MSPUBParser::getPageTypeBySeqNum).
_DUMMY_PAGE_SEQNUMS = frozenset({0x10D, 0x110, 0x113, 0x117})

# Quill chunk types. TOKN, the field table, is one libmspub never reads,
# and its presence is what tells us the document has fields at all. TEXT
# holds the words and FDPP the paragraph formatting that runs over them.
_TOKEN_CHUNK, _TEXT_CHUNK, _PARAGRAPHS_CHUNK = "TOKN", "TEXT", "FDPP"

# Tab stops, which libmspub reads into ParagraphStyle::m_tabStopsInEmu and
# its collector then never looks at, so they are dropped before librevenge
# sees anything. The layout below is therefore known rather than inferred.
_PARAGRAPH_TABS, _TAB_ARRAY = 0x32, 0x28
_TAB_POSITION, _TAB_ALIGNMENT = 0x00, 0x01
# The alignment field's low byte. 313 of the corpus's 335 stops leave the
# field out altogether, which is a left tab; the 22 that state it come in
# pairs, one at the middle of the frame reading 2 and one at its right
# edge reading 1 -- the centre and right tabs of a header or a footer.
_TAB_ALIGNMENTS = {1: "right", 2: "center"}

# The document's "Default tab stops" interval -- Publisher's own
# `Document.DefaultTabStop`, a per-publication value it documents as
# points in the range 1 to 1584. The Quill stream's SGP chunk is a bare
# U32 length and then at most one block, id 0x00 and type 0x22, holding
# the interval in EMU; a chunk stating no block leaves the document on
# Publisher's default of half an inch. See research/default_tab.py for
# the evidence, which is circumstantial: the block is absent from both
# corpus files carrying no tab, present in all seven that carry one, and
# reads three different values where the block once suspected of holding
# the interval reads the same number in every file that has it.
_SECTION_CHUNK = "SGP "
_DEFAULT_TAB_STOP, _DEFAULT_TAB_STOP_TYPE = 0x00, 0x22
# What Publisher uses when the file states nothing, and what InDesign
# falls back to as well -- so a document reading this needs no ruler
# written for it.
PUBLISHER_DEFAULT_TAB_STOP = 36.0


@dataclass
class PageStructure:
    seq: int
    is_master: bool = False
    applied_master: Optional[int] = None
    shape_count: int = 0
    #: The seqnum of every shape this page lists, which is how the file
    #: states what is on it. Disjoint from every other page's list.
    shape_seqnums: List[int] = field(default_factory=list)


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
    #: WordArt's own character formatting, which is stated on the shape
    #: rather than on the text and is therefore lost with it.
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    #: How the glyphs are bent, in the words Publisher's gallery uses, or
    #: None for a shape that is not bent at all -- which is what nearly
    #: every WordArt in a real document turns out to be.
    warp: Optional[str] = None
    #: Character spacing as a multiple of normal, stated only when it is
    #: not normal. Publisher's gallery calls 1.2 loose and 0.9 tight.
    spacing: Optional[float] = None
    #: This shape's seqnum, the number its page chunk lists it by. The only
    #: way to the page a shape libmspub never reported belongs to.
    shape_seq: Optional[int] = None


@dataclass
class ShapeAnchor:
    """Where one Escher shape sits, and which shape the file says it is.

    Only the shapes libmspub *does* report matter here: matching an anchor
    to an item it drew is what says which libmspub page a page chunk turned
    into. Coordinates are measured from the centre of the page, like
    everything else the Escher stream states.
    """

    shape_seq: int
    centre_x: float
    centre_y: float


@dataclass
class ShapeGradient:
    """One shape's gradient as the file states it, and where the shape sits.

    `stops` is (position 0..1, (r, g, b)) in ramp order. Coordinates are
    measured from the centre of the page, the only frame of reference the
    Escher stream has, exactly as `WordArt` measures them.
    """

    stops: List[Tuple[float, tuple]] = field(default_factory=list)
    #: Degrees, as libmspub would have reported them in `draw:angle`.
    angle: float = 0.0
    centre_x: float = 0.0
    centre_y: float = 0.0
    width: float = 0.0
    height: float = 0.0


@dataclass
class FileStructure:
    """What the file says that libmspub does not pass on."""

    #: Every page chunk carrying shapes, in the order the file holds them,
    #: which is *not* libmspub's page order: matching Escher anchors to the
    #: items libmspub drew shows the two are a permutation of one another in
    #: every multi-page file in the corpus (`backlog.md` §14). Anything
    #: needing the page a *particular* shape sits on should ask
    #: `page_seq_of` and take that to a page through the event stream, as
    #: `convert._page_by_chunk` does, rather than index into this list.
    pages: List[PageStructure] = field(default_factory=list)
    masters: Dict[int, PageStructure] = field(default_factory=dict)
    #: Which page chunk each shape seqnum belongs to, across pages and
    #: masters alike. The file's own statement of what is on which page.
    shape_pages: Dict[int, int] = field(default_factory=dict)
    #: Where every Escher shape sits, for tying page chunks to the pages
    #: libmspub emitted.
    anchors: List[ShapeAnchor] = field(default_factory=list)
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
    #: Tab stops by the text of the paragraph they belong to, one entry per
    #: paragraph in the file that carries a tab -- including those stating
    #: no stops, because a text that appears both with stops and without is
    #: an ambiguity rather than a match. Text is the only handle there is:
    #: nothing in the event stream identifies which paragraph libmspub is
    #: reporting. Each stop is (position in points, alignment).
    paragraph_stops: List[Tuple[str, Tuple[Tuple[float, str], ...]]] = field(
        default_factory=list
    )
    #: Every shape whose fill the file states as a gradient, matched to the
    #: event stream the same way WordArt is: by where the shape sits.
    gradients: List[ShapeGradient] = field(default_factory=list)
    #: The document's "Default tab stops" interval in points, where the file
    #: states one. None means it does not, which is Publisher's own default
    #: of half an inch -- the same grid InDesign falls back to, so there is
    #: then nothing to carry.
    default_tab_stop: Optional[float] = None
    #: Every run of linked text frames, as shape seqnums in the order the
    #: story flows through them. libmspub says nothing about a link, so
    #: without this the order is whatever order the frames turned up in.
    story_chains: List[List[int]] = field(default_factory=list)

    def gradient_for(
        self,
        centre_x: float,
        centre_y: float,
        width: float,
        height: float,
        tolerance: float = 0.5,
    ) -> Optional[ShapeGradient]:
        """The gradient shape sitting here, told apart from its neighbours
        by size where the centre alone is not enough.

        A newsletter stacks a banner and the panel behind it within half a
        point of one another, so two ramps can share a centre. Their sizes
        differ by tens of points, which settles it -- but the size cannot
        be required to *match*, because the anchor box measures the shape
        with its outline and libmspub reports the path inside it, a gap of
        16pt on one shape in the corpus. So the nearest size wins, and two
        equally near is an ambiguity rather than a guess.
        """
        near = [
            found for found in self.gradients
            if abs(found.centre_x - centre_x) <= tolerance
            and abs(found.centre_y - centre_y) <= tolerance
        ]
        if not near:
            return None
        near.sort(key=lambda f: abs(f.width - width) + abs(f.height - height))
        if len(near) > 1:
            first = abs(near[0].width - width) + abs(near[0].height - height)
            second = abs(near[1].width - width) + abs(near[1].height - height)
            if abs(first - second) <= tolerance:
                return None
        return near[0]

    def page_seq_of(self, shape_seq: Optional[int]) -> Optional[int]:
        """The page chunk that lists this shape, if the file lists it."""
        if shape_seq is None:
            return None
        return self.shape_pages.get(shape_seq)

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

    def master_of_chunk(self, page_seq: Optional[int]) -> Optional[PageStructure]:
        """The master this page chunk applies, as the file states it.

        Asked by chunk rather than by page index: the chunk list is not in
        libmspub's page order, so which chunk a page is has to be measured
        first (`convert._page_by_chunk`).
        """
        chunk = next((p for p in self.pages if p.seq == page_seq), None)
        if chunk is None or chunk.applied_master is None:
            return None
        return self.masters.get(chunk.applied_master)

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

def _read_stream(data: bytes, *path: str) -> Optional[bytes]:
    """Pull one stream out of a CFB container, named by its full path.

    A name on its own is not unique: an embedded OLE object brings its own
    storage with it, and `1336 kerkbode.pub` holds two streams called
    CONTENTS -- Publisher's text, and a metafile belonging to an embedded
    object. Walking the directory tree from the root picks the one meant,
    where matching on the name alone picked whichever came first.
    """
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
    # Both the count and the chain pointers are the file's own claim, so a
    # sector pointing at itself would otherwise run to the U32 limit: a 1KB
    # file is enough to hang the read for ever, and a hung worker costs a
    # whole batch its report rather than one file its structure. A file
    # cannot hold more DIFAT sectors than it holds sectors, and none of them
    # is worth visiting twice -- the same guard `chain` below already uses.
    seen_difat: set = set()
    for _ in range(min(difat_count, len(data) // sector + 1)):
        if sec >= 0xFFFFFFFE or sec in seen_difat or at(sec) + sector > len(data):
            break
        seen_difat.add(sec)
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

    # A directory entry is 128 bytes: the name, then its kind, then the
    # red-black tree links -- left and right siblings, and the first child
    # of a storage -- and finally where its own bytes start and end.
    entries = []
    for s in chain(dir_start):
        base = at(s)
        if base + sector > len(data):
            break
        for i in range(sector // 128):
            entry = base + i * 128
            name_length = struct.unpack_from("<H", data, entry + 64)[0]
            entries.append((
                data[entry:entry + max(0, name_length - 2)].decode("utf-16-le", "replace"),
                data[entry + 66],
                struct.unpack_from("<I", data, entry + 68)[0],   # left sibling
                struct.unpack_from("<I", data, entry + 72)[0],   # right sibling
                struct.unpack_from("<I", data, entry + 76)[0],   # first child
                struct.unpack_from("<I", data, entry + 116)[0],
                struct.unpack_from("<I", data, entry + 120)[0],
            ))

    def named(parent: int, want: str) -> Optional[int]:
        """The child of `parent` called `want`, wherever it sits in the tree.

        The siblings form a balanced tree ordered by a comparison this does
        not need to reproduce, so every node under the child pointer is
        visited rather than descending by name.
        """
        seen, stack = set(), [entries[parent][4]] if parent < len(entries) else []
        while stack:
            index = stack.pop()
            if index >= len(entries) or index in seen:
                continue
            seen.add(index)
            if entries[index][0] == want:
                return index
            stack += [entries[index][2], entries[index][3]]
        return None

    target = 0  # the root storage
    for part in path:
        found = named(target, part)
        if found is None:
            return None
        target = found
    if entries[target][1] != 2:  # a storage is not a stream
        return None
    _, _, _, _, _, start, size = entries[target]
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
    ministore = read_chain(root[5], root[6])
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
            page.shape_seqnums += [
                entry.data
                for entry in _children(contents, block)
                if entry.type == _SHAPE_SEQNUM
            ]
            page.shape_count = len(page.shape_seqnums)
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


def _read_story_chains(contents: bytes, refs) -> List[List[int]]:
    """Every linked run of text frames, as shape seqnums in flow order.

    A shape names the story it holds and its own place in that story, so
    the frames sharing a story *are* the chain and the index puts them in
    order. The head leaves the index out, the way every field in this
    format is left out when it has nothing to say.

    Two things this rules out that guessing from the text cannot. A story
    one shape holds is not a chain, which is what keeps a page-number
    footer -- one master shape replayed onto every page -- from being read
    as a run of linked frames. And two shapes claiming the same place in
    one story contradict each other, so that story states no order at all.
    """
    stories: Dict[int, List[tuple]] = {}
    for seq, kind, offset in refs:
        if kind != _SHAPE_CHUNK:
            continue
        fields = {block.id: block.data for block in _chunk_blocks(contents, offset)}
        story = fields.get(_SHAPE_STORY_ID)
        if story is None:
            continue
        stories.setdefault(story, []).append((fields.get(_SHAPE_CHAIN_INDEX, 0), seq))

    chains = []
    for links in stories.values():
        if len(links) < 2:
            continue
        if len({index for index, _seq in links}) != len(links):
            continue
        chains.append([seq for _index, seq in sorted(links)])
    # A shape belongs to one story, so no two chains share a head and
    # sorting by it is a total order -- worth having, since the caller
    # reports what it threaded.
    return sorted(chains)


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
    escher = _read_stream(data, *_ESCHER_STREAM)
    return _wordart_shapes(escher) if escher else []


def _read_shape_anchors(data: bytes) -> List[ShapeAnchor]:
    """Where every Escher shape sits, by the seqnum the file knows it as.

    Nothing in the event stream says which page chunk libmspub turned into
    which page, and the chunk order is not the answer. What is available is
    this: the file says which page lists a shape, and libmspub draws the
    shape somewhere. Line the two up by where the shape sits and the pages
    identify themselves -- which is the only route to the page of a shape
    libmspub never reported at all.
    """
    escher = _read_stream(data, *_ESCHER_STREAM)
    return _shape_anchors(escher) if escher else []


def _shape_anchors(escher: bytes) -> List[ShapeAnchor]:
    """Every shape in an Escher stream that states both a seqnum and a box."""
    found: List[ShapeAnchor] = []
    for body, end in _escher_shapes(escher, 0, len(escher)):
        shape_seq = box = None
        for _version, _instance, rec_type, sub_body, sub_end in _escher_records(
            escher, body, end
        ):
            if rec_type == _CLIENT_DATA:
                shape_seq = _escher_values(escher, sub_body, sub_end).get(
                    _CLIENT_DATA_SEQNUM
                )
            elif rec_type == _CLIENT_ANCHOR:
                anchor = _escher_values(escher, sub_body, sub_end)
                if all(side in anchor for side in _ANCHOR_SIDES):
                    box = [
                        _signed(anchor[side]) / _EMU_PER_POINT
                        for side in _ANCHOR_SIDES
                    ]
        if shape_seq is None or box is None:
            continue
        found.append(
            ShapeAnchor(
                shape_seq=shape_seq,
                centre_x=(box[0] + box[2]) / 2.0,
                centre_y=(box[1] + box[3]) / 2.0,
            )
        )
    return found


def _wordart_boolean(value, bit: int) -> bool:
    """One of WordArt's packed booleans, false unless the file states it."""
    if not isinstance(value, int):
        return False
    return bool(value >> 16 & (1 << bit)) and bool(value & (1 << bit))


def _wordart_shapes(escher: bytes) -> List[WordArt]:
    """The WordArt shapes in an Escher stream."""
    found: List[WordArt] = []
    for body, end in _escher_shapes(escher, 0, len(escher)):
        text = font = None
        size = rotation = spacing = None
        box = None
        shape_type = _WORDART_PLAIN
        flags = shape_seq = None
        for _version, instance, rec_type, sub_body, sub_end in _escher_records(
            escher, body, end
        ):
            if rec_type in _PROPERTY_RECORDS:
                props = _escher_properties(escher, sub_body, sub_end, instance)
                text = _escher_text(props.get(_PROP_WORDART_TEXT)) or text
                font = _escher_text(props.get(_PROP_WORDART_FONT)) or font
                if isinstance(props.get(_PROP_WORDART_SIZE), int):
                    size = props[_PROP_WORDART_SIZE] / _FIXED_16_16
                if isinstance(props.get(_PROP_WORDART_SPACING), int):
                    spacing = props[_PROP_WORDART_SPACING] / _FIXED_16_16
                if isinstance(props.get(_PROP_ROTATION), int):
                    rotation = _signed(props[_PROP_ROTATION]) / _FIXED_16_16
                if isinstance(props.get(_PROP_WORDART_BOOLS), int):
                    flags = props[_PROP_WORDART_BOOLS]
            elif rec_type == _SHAPE_RECORD:
                shape_type = instance
            elif rec_type == _CLIENT_DATA:
                shape_seq = _escher_values(escher, sub_body, sub_end).get(
                    _CLIENT_DATA_SEQNUM
                )
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
        # A headline set on three lines stacks three of them into the same
        # band, so it is a line's share of the band that stands for the
        # size, not the whole of it. Sizing a three-line headline from the
        # full band trebles it.
        lines = len(re.split(r"\r\n|\r|\n", text)) or 1
        found.append(
            WordArt(
                text=text,
                font=font,
                size=(height / lines / _BAND_TO_SIZE) if fitted else size,
                centre_x=(box[0] + box[2]) / 2.0,
                centre_y=(box[1] + box[3]) / 2.0,
                width=width,
                height=height,
                rotation=rotation or 0.0,
                fitted=fitted,
                bold=_wordart_boolean(flags, _WORDART_BOLD),
                italic=_wordart_boolean(flags, _WORDART_ITALIC),
                underline=_wordart_boolean(flags, _WORDART_UNDERLINE),
                strikethrough=_wordart_boolean(flags, _WORDART_STRIKETHROUGH),
                warp=_WORDART_WARPS.get(shape_type),
                spacing=spacing if spacing is not None and spacing != 1.0 else None,
                shape_seq=shape_seq,
            )
        )
    return found


# -- Quill (the text stream) ----------------------------------------------

def _quill_chunks(quill: bytes):
    """Every chunk in the Quill stream, as (name, offset, length).

    The list starts at 0x18 and can continue in further lists; each
    reference is 24 bytes, of which this needs the four-character name and
    the last two words.
    """
    chunks, offset, seen = [], 0x18, set()
    while offset != 0xFFFFFFFF and offset + 8 <= len(quill) and offset not in seen:
        seen.add(offset)
        count = struct.unpack_from("<H", quill, offset + 2)[0]
        nxt = struct.unpack_from("<I", quill, offset + 4)[0]
        pos = offset + 8
        for _ in range(count):
            if pos + 24 > len(quill):
                break
            chunks.append((
                quill[pos + 2:pos + 6].decode("ascii", "replace"),
                struct.unpack_from("<I", quill, pos + 16)[0],
                struct.unpack_from("<I", quill, pos + 20)[0],
            ))
            pos += 24
        offset = nxt
    return chunks


def _read_palette(contents: bytes, refs) -> List[tuple]:
    """The document's colour scheme, in the order a reference indexes it.

    An entry that states no colour still takes its place in the list --
    libmspub adds a black for it -- so the positions have to be kept.
    """
    palette: List[tuple] = []
    for _seq, kind, offset in refs:
        if kind != _PALETTE_CHUNK:
            continue
        for block in _chunk_blocks(contents, offset):
            if block.type != 0xA0:
                continue
            for entry in _children(contents, block):
                colour = next(
                    (
                        sub.data
                        for sub in _children(contents, entry)
                        if sub.id == _PALETTE_ENTRY_COLOR
                    ),
                    None,
                )
                palette.append(
                    (0, 0, 0) if colour is None
                    else (colour & 0xFF, (colour >> 8) & 0xFF, (colour >> 16) & 0xFF)
                )
    return palette


def _real_color(value: int, palette: List[tuple]) -> Optional[tuple]:
    """A colour reference read directly, as ColorReference::getRealColor.

    Either an index into the document palette or a BGR triple. This never
    consults a base colour, which is what stops an intensity change stated
    against another intensity change from chasing its own tail.
    """
    if (value >> 24) & 0xFF == _COLOR_FROM_PALETTE:
        index = value & 0xFFFFFF
        return palette[index] if index < len(palette) else None
    return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def _resolve_color(value: int, base: int, palette: List[tuple]) -> Optional[tuple]:
    """A colour reference as ColorReference::getFinalColor resolves it.

    An intensity change is a shade of the colour it is stated against, so
    it takes a second reference -- and that one is read directly, the way
    libmspub reads it, rather than resolved in its turn.
    """
    kind = (value >> 24) & 0xFF
    if kind == _COLOR_CHANGE_INTENSITY:
        under = _real_color(base, palette)
        if under is None:
            return None
        intensity = ((value >> 16) & 0xFF) / 255.0
        which = (value >> 8) & 0xFF
        if which == _INTENSITY_BLACK_BASE:
            return tuple(round(channel * intensity) for channel in under)
        if which == _INTENSITY_WHITE_BASE:
            return tuple(
                round(channel + (255 - channel) * (1 - intensity)) for channel in under
            )
        return None
    return _real_color(value, palette)


def _shade_stops(blob, palette: List[tuple], base: int):
    """The waypoints of a shade list: (position, colour) in file order.

    Each entry is a colour and a position in 16.16 fixed point, after a
    six-byte header whose first two bytes count them.
    """
    if not isinstance(blob, bytes) or len(blob) <= 6:
        return []
    count = blob[0] | (blob[1] << 8)
    stops, at = [], 6
    for _ in range(min(count, (len(blob) - 6) // 8)):
        colour = int.from_bytes(blob[at:at + 4], "little")
        position = int.from_bytes(blob[at + 4:at + 8], "little") / _FIXED_16_16
        at += 8
        resolved = _resolve_color(colour, base, palette)
        if resolved is not None:
            stops.append((min(1.0, max(0.0, position)), resolved))
    return stops


def _gradient_angle(props: dict) -> float:
    """The ramp's angle, stated the way libmspub would have reported it.

    Three transformations sit between the file and `draw:angle`, and all
    three have to be reproduced or a restored ramp runs a different way
    from an identical one libmspub reported itself: the value is degrees
    in the high half of a 16.16 fixed point; two angles are offset by
    ninety degrees in the file format, which libmspub corrects by name;
    and the result is negated, because ODF measures clockwise.
    """
    raw = props.get(_PROP_FILL_ANGLE)
    if not isinstance(raw, int):
        return 0.0
    degrees = _signed(raw) >> 16
    degrees = _FILL_ANGLE_FIXUPS.get(degrees, degrees)
    return float(-degrees)


def _ramp_stops(focus, waypoints, first, last):
    """The whole ramp: the waypoints, with the colours either side of them.

    `focus` says which end the ramp starts from. At 100 it runs from the
    fill-back colour to the fill colour and the waypoints run backwards
    with it, each at the distance from the *other* end -- which is what
    libmspub does with the ones it reports in full
    (`addColorReverse`), so the two readings of one file agree.

    A focus that means neither end is left to the caller: libmspub folds
    those into a ramp that returns to where it started, and no shape in
    the corpus states one alongside a shade list, so there is nothing to
    check a reconstruction against.
    """
    if focus == 100:
        stops = [(1.0 - position, colour) for position, colour in reversed(waypoints)]
        first, last = last, first
    else:
        stops = list(waypoints)
    # A list that already reaches an end states that end itself.
    if stops[0][0] > 0.01:
        stops.insert(0, (0.0, first))
    if stops[-1][0] < 0.99:
        stops.append((1.0, last))
    return stops


def _read_gradients(data: bytes, palette: List[tuple]) -> List[ShapeGradient]:
    """Every gradient the file states, whole, with the shape it fills.

    libmspub reads the same properties and then builds the ramp from the
    shade list alone whenever there is one (MSPUBParser::getShapeFill), so
    a two-colour Publisher gradient with a waypoint in the middle reaches
    the event stream as that waypoint by itself -- one stop, nothing to
    ramp between, and a flat fill on the page. The colours it dropped are
    the shape's own fill and fill-back, and they sit either side of the
    waypoints.
    """
    escher = _read_stream(data, *_ESCHER_STREAM)
    if not escher:
        return []

    found: List[ShapeGradient] = []
    for body, end in _escher_shapes(escher, 0, len(escher)):
        props: dict = {}
        box = None
        for _version, instance, rec_type, sub_body, sub_end in _escher_records(
            escher, body, end
        ):
            if rec_type in _PROPERTY_RECORDS:
                props.update(_escher_properties(escher, sub_body, sub_end, instance))
            elif rec_type == _CLIENT_ANCHOR:
                anchor = _escher_values(escher, sub_body, sub_end)
                if all(side in anchor for side in _ANCHOR_SIDES):
                    box = [
                        _signed(anchor[side]) / _EMU_PER_POINT
                        for side in _ANCHOR_SIDES
                    ]

        if props.get(_PROP_FILL_TYPE) not in _GRADIENT_FILL_TYPES or box is None:
            continue
        fill = props.get(_PROP_FILL_COLOR)
        if not isinstance(fill, int):
            continue
        focus = _signed(props[_PROP_FILL_FOCUS]) if _PROP_FILL_FOCUS in props else 0
        if focus not in _KNOWN_FILL_FOCUS:
            continue
        back = props.get(_PROP_FILL_BACK)
        first = _resolve_color(fill, fill, palette)
        last = _resolve_color(back, fill, palette) if isinstance(back, int) else None
        waypoints = _shade_stops(props.get(_PROP_FILL_SHADE), palette, fill)
        if first is None or last is None or not waypoints:
            continue

        stops = _ramp_stops(focus, waypoints, first, last)
        found.append(
            ShapeGradient(
                angle=_gradient_angle(props),
                stops=stops,
                centre_x=(box[0] + box[2]) / 2.0,
                centre_y=(box[1] + box[3]) / 2.0,
                width=box[2] - box[0],
                height=box[3] - box[1],
            )
        )
    return found


def _has_field_table(quill: bytes) -> bool:
    """True when the Quill stream carries a TOKN chunk of any kind."""
    return any(name == _TOKEN_CHUNK for name, _offset, _length in _quill_chunks(quill))


def _style_tab_stops(quill: bytes, position: int):
    """The tab stops one paragraph style states, in points.

    A style is a length followed by blocks. The tabs block holds an array
    whose entries are containers of a position in EMU and, where the stop
    is not an ordinary left one, an alignment. Positions are signed: one
    style in the corpus puts three stops to the left of the text.
    """
    if position + 4 > len(quill):
        return ()
    length = struct.unpack_from("<I", quill, position)[0]
    stops = []
    for block in _blocks(quill, position + 4, position + length):
        if block.id != _PARAGRAPH_TABS:
            continue
        for array in _blocks(quill, block.data_offset + 4, block.end):
            if array.id != _TAB_ARRAY:
                continue
            for entry in _blocks(quill, array.data_offset + 4, array.end):
                if entry.type != _GENERAL_CONTAINER:
                    continue
                fields = {
                    sub.id: sub.data
                    for sub in _blocks(quill, entry.data_offset + 4, entry.end)
                }
                if _TAB_POSITION not in fields:
                    continue
                stops.append((
                    _signed(fields[_TAB_POSITION]) / _EMU_PER_POINT,
                    _TAB_ALIGNMENTS.get(fields.get(_TAB_ALIGNMENT, 0) & 0xFF, "left"),
                ))
    return tuple(stops)


def _paragraph_stops(quill: bytes):
    """Every paragraph that carries a tab, with the stops stated for it.

    FDPP is a table of paragraphs in text order: first the offset each one
    ends at, then where in the chunk its style sits. The offsets are
    measured from the start of the stream, so the text a paragraph covers
    is the slice of TEXT between the previous one's end and its own.

    A paragraph with no tab in it is left out: a stop only decides where a
    tab lands, so there is nothing to carry for the rest, and dropping
    them keeps the text this returns to what a caller can act on.
    """
    chunks = _quill_chunks(quill)
    text_chunk = next((c for c in chunks if c[0] == _TEXT_CHUNK), None)
    if text_chunk is None:
        return []
    _name, text_at, text_length = text_chunk
    text = quill[text_at:text_at + text_length].decode("utf-16-le", "replace")

    found, start = [], text_at
    for _name, offset, _length in [c for c in chunks if c[0] == _PARAGRAPHS_CHUNK]:
        if offset + 8 > len(quill):
            continue
        count = struct.unpack_from("<H", quill, offset)[0]
        # Six bytes sit between the count and the offsets; libmspub skips
        # them without saying what they are.
        table = offset + 8
        if table + count * 6 > len(quill):
            continue
        for index in range(count):
            end = struct.unpack_from("<I", quill, table + 4 * index)[0]
            at = struct.unpack_from("<H", quill, table + 4 * count + 2 * index)[0]
            body = text[max(0, (start - text_at) // 2):max(0, (end - text_at) // 2)]
            start = end + 1
            if "\t" in body:
                found.append((body, _style_tab_stops(quill, offset + at)))
    return found


def _default_tab_stop(quill: bytes) -> Optional[float]:
    """The document's default tab interval in points, where it states one.

    Publisher applies this to every tab that has no stop of its own, which
    in this corpus is 200 of the 203 tabs there are. It is out of range of
    what Publisher's own property allows -- 1 to 1584 points -- if the
    block is something other than what it looks like, so a reading outside
    that is refused rather than carried.
    """
    for name, offset, _length in _quill_chunks(quill):
        if name != _SECTION_CHUNK:
            continue
        if offset + 4 > len(quill):
            continue
        stated = struct.unpack_from("<I", quill, offset)[0]
        for block in _blocks(quill, offset + 4, offset + stated):
            if (block.id, block.type) != (_DEFAULT_TAB_STOP, _DEFAULT_TAB_STOP_TYPE):
                continue
            interval = _signed(block.data) / _EMU_PER_POINT
            if 1.0 <= interval <= 1584.0:
                return interval
            log.info("default tab interval out of range (%.2fpt), ignored", interval)
    return None


def read_structure(source: Path) -> Optional[FileStructure]:
    """Masters, field presence, cell insets and tab stops, or None."""
    # Also the document's default tab interval, which is what the great
    # majority of tabs in a Publisher file are actually lined up on.
    try:
        data = Path(source).read_bytes()
        contents = _read_stream(data, *_CONTENTS_STREAM)
        if not contents:
            return None
        quill = _read_stream(data, *_QUILL_STREAM) or b""

        refs = _chunk_references(contents)
        structure = FileStructure(
            has_fields=_has_field_table(quill),
            tables=_read_tables(contents, refs),
            wordart=_read_wordart(data),
            paragraph_stops=_paragraph_stops(quill),
            gradients=_read_gradients(data, _read_palette(contents, refs)),
            default_tab_stop=_default_tab_stop(quill),
            anchors=_read_shape_anchors(data),
            story_chains=_read_story_chains(contents, refs),
        )
        for seq, kind, offset in refs:
            if kind != _PAGE_CHUNK:
                continue
            page = _page_structure(contents, seq, offset)
            # Masters list shapes the same way, and a shape belongs to one
            # page either way, so both go in the one index.
            for shape_seq in page.shape_seqnums:
                structure.shape_pages[shape_seq] = seq
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
