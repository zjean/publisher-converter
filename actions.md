# Actions requiring a human

Things that cannot be finished from this machine, with everything else
already prepared so each one is short to execute.

Ordered by deadline, then value.

---

## 1. Identify Publisher's text-wrap field  ⏰ needs Publisher, before 1 Oct 2026

### Why this matters

libmspub exposes **no text-wrap information at all**. Since Publisher
emits images *after* text in z-order, images would paint over and hide
the copy. The converter currently compensates by giving every image a
bounding-box wrap (skipping page-sized backgrounds), which is a guess —
right for typical text-with-pictures documents, wrong wherever the
source deliberately layered artwork behind text.

Reading the real setting removes the guess. IDML supports every mode
Publisher has, including contour wrap, so once the field is known the
conversion becomes faithful rather than plausible.

### What is already known

Established here, so don't re-derive it:

- **Shapes are not stored in Escher.** All five sample files contain
  **zero `Sp` records** in `Escher/EscherStm`; that stream holds only the
  drawing-group defaults and an image store, and `EscherDelayStm` holds
  only image blips. Shapes live in Publisher's own **`Contents`** stream.
- The Escher wrap distances (`dxWrapDistLeft` `0x0384` … `dyWrapDistBottom`
  `0x0387`) *are* present but appear **once each** — they are document
  defaults, not per-shape values.
- libmspub's shape-chunk loop (`MSPUBParser.cpp`, the `else` branch
  around line 874) reads every block in the chunk but acts on only
  **seven** IDs: `SHAPE_WIDTH 0xAA`, `SHAPE_HEIGHT 0xAB`,
  `SHAPE_BORDER_IMAGE_ID 0x09`, `SHAPE_DONT_STRETCH_BA 0x07`,
  `SHAPE_TEXT_ID 0x27`, `SHAPE_VALIGN 0x35`, `SHAPE_CROP 0xB7`.
  **Everything else is silently discarded.** The wrap field is almost
  certainly among the discarded blocks.
- A correlation sweep over the sample corpus (`research/find_wrap.py`)
  could not pin it down, because **every picture in the corpus has the
  same wrap setting** — with no variation there is nothing to correlate.
  That is precisely why this controlled experiment is needed.

**Shortlist — check these first.** Discarded blocks that appear on
picture shapes with enum-like values:

| block | appears on | values seen | note |
|---|---|---|---|
| **`0x34`** | 14 of 16 pictures, and placeholder shapes; **never on text** | always `0` | best candidate: picture-only, single small value |
| `0x0C` | all 16 pictures | always `0` | also on text frames |
| `0x11` | 10 pictures | `0`, `1` | also on text |
| `0x10` | 10 pictures | `0`, `1`, `5` | also on text |
| `0x2C` | 4 pictures | `0`, `5` | also on text |
| `0x0F` | 9 pictures | always `0` | also on text |

Note `0x34` is listed in `MSPUBBlockID.h` as `PARAGRAPH_LINE_SPACING`,
but that header warns IDs are context-dependent — in a shape chunk it is
something else, and libmspub does not handle it there.

### Step 1 — build the instrumented parser (on this Mac, ~5 min)

Only needed if `/tmp` has been cleared; the binary is disposable.

```sh
cd /tmp && curl -sLO https://dev-www.libreoffice.org/src/libmspub-0.1.5.tar.xz
tar xf libmspub-0.1.5.tar.xz && cd libmspub-0.1.5

export PKG_CONFIG_PATH="$(brew --prefix icu4c@78)/lib/pkgconfig:$PKG_CONFIG_PATH"
export CXXFLAGS="-I/opt/homebrew/include -std=c++17 -O0 -g"
export LDFLAGS="-L/opt/homebrew/lib"
./configure --enable-debug --disable-shared --enable-static --without-docs --disable-werror
make -j8          # the bundled pub2raw tool fails to link; that is fine and expected

# link our dumper against the debug static library
cd ~/prive/tools/affinity-converter
ICU=$(brew --prefix icu4c@78)
c++ -std=c++17 -O0 -g -o /tmp/pubdump_debug src/pubdump.cpp \
  -I/tmp/libmspub-0.1.5/inc -I/opt/homebrew/include \
  -I/opt/homebrew/Cellar/librevenge/0.0.6/include/librevenge-0.0 -I$ICU/include \
  /tmp/libmspub-0.1.5/src/lib/.libs/libmspub-0.1.a \
  -L/opt/homebrew/Cellar/librevenge/0.0.6/lib -lrevenge-0.0 -lrevenge-stream-0.0 \
  -L$ICU/lib -licuuc -licui18n -licudata -lz
```

`--enable-debug` turns on `MSPUB_DEBUG_MSG`, which logs every block read
to stderr. That is the whole instrumentation — no patching needed.

### Step 2 — produce the sample files (on Windows, with Publisher, ~10 min)

This is the only part that needs Publisher. **Change exactly one thing
between saves — nothing else, not even scrolling the picture.**

1. New blank document. Add a text box, paste in a few paragraphs of
   filler so text would visibly reflow.
2. Insert any picture, overlapping the text.
3. Select the picture → **Picture Format → Wrap Text → Square**.
   Save as `wrap-square.pub`.
4. Without moving or resizing anything, change only the wrap setting and
   *Save As* each time:
   - **None** (In Front of Text) → `wrap-none.pub`
   - **Tight** → `wrap-tight.pub`
   - **Through** → `wrap-through.pub`
   - **Top and Bottom** → `wrap-topbottom.pub`
   - **Behind Text** → `wrap-behind.pub`
5. Copy all six files to `files/wrap-samples/` on the Mac.

Worth capturing while you are there: a **PDF export of each**
(`File → Export → Create PDF/XPS`, print quality). Those double as
ground truth for action 3 below.

### Step 3 — run the diff (on the Mac, ~1 min)

```sh
cd ~/prive/tools/affinity-converter
python3 research/diff_wrap.py /tmp/pubdump_debug \
  square=files/wrap-samples/wrap-square.pub \
  none=files/wrap-samples/wrap-none.pub \
  tight=files/wrap-samples/wrap-tight.pub \
  through=files/wrap-samples/wrap-through.pub \
  topbottom=files/wrap-samples/wrap-topbottom.pub \
  behind=files/wrap-samples/wrap-behind.pub
```

The tool keys shapes across files by `SHAPE_WIDTH`/`SHAPE_HEIGHT` (stable
when only wrap changes) and prints every block whose value differs, with
the value under each variant.

Its two control behaviours are verified: identical inputs report "no
block changed", unrelated inputs report "no shapes matched".

### Step 4 — read the result

You are looking for a block that:

- changes **only** on the picture you edited, not on other shapes;
- takes **one distinct small value per wrap mode** (so six variants →
  up to six values, likely `0`–`5`);
- is not a coordinate or an identifier (those show large, scattered
  values).

If several blocks change, the wrap field is the one whose values form a
clean enum. If **nothing** changes, wrap is not stored per shape in the
`Contents` chunk — in that case diff the raw streams instead:

```sh
for f in files/wrap-samples/*.pub; do
  gsf cat "$f" Contents > "/tmp/$(basename "$f").contents"
done
# then compare any two, e.g.
cmp -l /tmp/wrap-square.pub.contents /tmp/wrap-none.pub.contents | head -40
```

A one-variable change should differ in only a handful of bytes; their
offsets point straight at the field.

### Step 5 — wire it in

Once the block ID and its value mapping are known:

1. Read it in `src/pubdump.cpp`. Shapes are keyed by sequence number, and
   the existing property-list serialisation already carries anything
   added to the shape's property list.
2. Map values to IDML in `pubidml/idml.py`, `_emit_image`, replacing the
   current unconditional `TextWrapPreference`:

   | Publisher | IDML `TextWrapMode` |
   |---|---|
   | None / In Front of Text | `None` |
   | Square | `BoundingBoxTextWrap` |
   | Tight / Through | `ContourTextWrap` |
   | Top and Bottom | `JumpObjectTextWrap` |
   | Behind Text | `None`, and emit the image before the text frames |

3. Drop the `--no-image-wrap` heuristic flag, or keep it as an override.
4. Also pick up the per-shape wrap distances if they turn out to be
   stored alongside — they map to IDML's `TextWrapOffset`.

---

## 2. Recover the page margins  ⏰ needs Publisher, before 1 Oct 2026

### Why this matters

Margin and column guides are lost entirely. Every converted document
arrives in Affinity with default margins, so anyone continuing to lay out
a page has to measure the original by eye to match it.

### What is already known

Established here, so don't re-derive it:

- **libmspub cannot help.** Its `Margins` struct is *shape* margins — the
  text-frame insets already exposed as `fo:padding-*`. There is no
  page-margin block id anywhere in `MSPUBBlockID.h`, and `startPage`
  carries exactly two properties, `svg:width` and `svg:height`.
- **The DOCUMENT chunk is accounted for** and contains no margin. Block
  `0x12` decodes as page size and matches the known dimensions, `0x2a` is
  a GUID, and the rest are sequence-number references and small enums.
- **The master page chunk holds the master's name** — `0x0e` a short
  `"A"`, `0x0f` the full `"Master Page"` — plus two coordinate pairs at
  roughly 25in and 120in, far too large to be page margins.
- Publisher measures in **EMU, 914400 per inch**, so 0.5in reads 457200.

So the field exists somewhere not yet looked at, and one controlled pair
will find it.

### Step 1 — produce the sample files (on Windows, with Publisher, ~10 min)

**Change only the margins between saves.** Use *asymmetric* values, so
each edge is individually identifiable rather than four copies of one
number.

1. New blank document, letter size. Add a text box with a few paragraphs
   so the page is not empty.
2. **Page Design → Margins → Custom Margins**, set
   **left 0.5", top 1.0", right 1.5", bottom 2.0"**. Save as
   `margins-a.pub`.
3. Change *only* the margins to **left 0.25", top 0.75", right 1.25",
   bottom 1.75"** and *Save As* `margins-b.pub`.
4. Copy both to `files/margin-samples/` on the Mac.

The eight values are all distinct, which is the point: whichever block
holds a margin will read one of them.

### Step 2 — run the diff (on the Mac, ~1 min)

```sh
cd ~/prive/tools/affinity-converter
python3 research/diff_blocks.py \
  a=files/margin-samples/margins-a.pub \
  b=files/margin-samples/margins-b.pub
```

Unlike `diff_wrap.py`, this parses the file directly rather than a
libmspub trace, so it also sees blocks libmspub never reads — which is
where the margin has to be. Both control behaviours are verified:
identical inputs report "no block changed", unrelated files report
differences.

The tool already prints any plausible length in inches beside the raw
value, so a margin should be readable at a glance.

### Step 3 — read the result

You are looking for four blocks in the same chunk reading
`457200 / 914400 / 1371600 / 1828800` under `a` and
`228600 / 685800 / 1143000 / 1600200` under `b`. They may be four
separate blocks or one 16-byte container holding four `U32`s — the
DOCUMENT chunk already uses that shape for page size at block `0x12`.

If nothing matches, the margins are held per master page rather than per
document: re-run against the master chunk by noting its offset from
`python3 research/master_pages.py files/margin-samples/margins-a.pub`.

### Step 4 — wire it in

Emit `MarginPreference` on each `Page` element in `idml.py`, beside the
existing `GeometricBounds`. IDML takes it directly:

```xml
<MarginPreference Top="72" Left="36" Bottom="144" Right="108"
                  ColumnCount="1" ColumnGutter="12"/>
```

Column guides very likely sit next to the margins in the same chunk; if
the diff turns them up too, `ColumnCount` is the same one-line change.

---

## 3. Pin down Publisher's field table  ⏰ needs Publisher, before 1 Oct 2026

### Why this matters

A page-number field is stored as a bare `#` in the text, so a footer
reading `#` converts to a literal `#` on every page. The converter can
already tell *that* a document has fields and can locate master content,
which is enough to substitute page numbers safely. What it cannot yet do
is say **which** `#` belongs to **which** field — so a document mixing a
page number with a typed `#` needs a human.

### What is already known

- libmspub reads only seven Quill chunk types — `TEXT`, `STRS`, `SYID`,
  `PL  `, `FDPC`, `FDPP`, `STSH` — and **skips `TOKN`**, the field table.
- Across the sample set **no file contains a `#` without a `TOKN` chunk**,
  which is what makes `TOKN` usable as a gate.
- `TOKN` holds field names: one sample ends with a counted UTF-16 string,
  `07 00 "orgname"`, Publisher's Organization Name field.
- Byte 16 reads **3** where the only field is a page number and **28**
  where it is `orgname` — consistent with a field-type code, but two
  samples cannot establish that.
- **The missing link is token → position.** Both `TOKN` chunks in the
  page-numbered sample are byte-identical 52-byte blobs carrying no
  character offsets, so the association lives elsewhere — probably the
  chunk id, which is 6 and 7 there.

### Step 1 — produce the sample files (on Windows, with Publisher, ~10 min)

Four small files. Keep them otherwise identical.

1. New document. **View → Master Page**, add a text box in the footer.
   **Insert → Page Number**. Back to **View → Normal**. Add two more
   pages so numbering is visible. Save as `field-pagenum.pub`.
2. Same document, but in the footer box type a literal `#` instead of
   inserting the field. Save as `field-literal-hash.pub`.
   **This is the control**: it must produce no `TOKN`, or a clearly
   different one.
3. Same as 1, but put the page number at the *end* of a longer line —
   type `Page ` before it. Save as `field-pagenum-offset.pub`.
   **This is the one that pins the position link**: the `#` moves to a
   known, different character index while everything else stays put.
4. Same as 1, plus **Insert → Date & Time** in a second footer box.
   Save as `field-pagenum-plus-date.pub`.
5. Copy all four to `files/field-samples/` on the Mac.

### Step 2 — read the tokens (on the Mac, ~1 min)

```sh
cd ~/prive/tools/affinity-converter
python3 research/quill_tokens.py files/field-samples/*.pub
```

It prints each file's `TOKN` chunks as raw hex beside the character
indices of every `#` in the text, which is exactly the comparison needed.

### Step 3 — read the result

- `field-literal-hash.pub` **must** show `0` TOKN chunks. If it shows
  one, the gate is unsound and page-number substitution must not ship.
- Diff `field-pagenum.pub` against `field-pagenum-offset.pub`: the `#`
  moves by exactly the length of `"Page "`, so whichever bytes in `TOKN`
  change by 5 are the character offset.
- `field-pagenum-plus-date.pub` should show two tokens with different
  type codes at byte 16, confirming 3 = page number.

### Step 4 — wire it in

Once the offset is known, `#` substitution stops being an inference and
becomes a lookup, and the `review` flag proposed for the heuristic
version can be dropped.

---

## 4. Confirm the rotation sign  ⏰ quick, no Publisher needed

Rotation **magnitude and pivot are verified correct** (a 300 dpi render
measured 274.6 × 175.9 pt against a predicted 274.8 × 176.0 for a 300×30
bar at 30°, rotated about its centre). What is unverified is the
**direction**: we render `+30` clockwise, but nothing confirms that
matches Publisher's sign convention.

Cheapest check — LibreOffice drives the *same* libmspub, so it shows how
the reference consumer reads the property:

```sh
brew install --cask libreoffice
soffice --headless --convert-to pdf --outdir /tmp/ref files/rotated_text.pub
```

Compare `/tmp/ref/rotated_text.pdf` against the Affinity render of
`converted/rotated_text.idml`. If the text tilts the same way, the sign
is right. If mirrored, negate the return of `_rotation()` in
`pubidml/model.py` — a one-line change.

This also gives a free reference renderer for action 3.

---

## 5. Measure fidelity on your real collection

Before trusting the tool at scale, quantify it on ~30 files spanning the
variety of your archive (newsletters, flyers, multi-page, heavy imagery).

For each: export a PDF from Publisher (ground truth), convert with
`pub2idml`, export a PDF from Affinity, then compare page count,
extracted text, and a per-page visual diff score.

That answers "is libmspub good enough" **for your corpus**, which is the
only version of the question that matters. If it scores badly, the
fallback is Markzware OmniMarkz (~€200) benchmarked on the same 30 files
— cheaper than any amount of further engineering.

---

## 6. Build the Windows executable

Everything is prepared; nothing here needs a decision.

Push the repo and let `.github/workflows/build-windows.yml` run — it
builds `pubdump.exe` under MSYS2 UCRT64, bundles it with PyInstaller, runs
the result against the sample files, and uploads `pub2idml.exe` as an
artifact. You need Windows only to *run* the tool, not to build it.

The workflow now proves self-containment rather than assuming it: it runs
the bundled exe with `PATH` stripped to `C:\Windows\system32`, so a
missing DLL fails the build instead of failing on your machine. DLL
collection is transitive via `objdump` (`tools/collect-dlls.sh`), which
replaced an unreliable `ldd`-based step.

The bundle logic itself is verified — a one-file build on macOS resolves
its parser through `sys._MEIPASS` and converts all five samples with a
stripped `PATH`. What remains untested until the workflow runs is
Windows-specific: the MSYS2 build and DLL collection, and the fact that
MSYS2 ships libmspub **0.1.4** where development used **0.1.5**.

Note the artifact is **unsigned**, so first run shows a SmartScreen
warning ("More info" → "Run anyway"). Signing needs a code signing
certificate and is only worth it for wider distribution.

---

## 7. Report the clip-path bug upstream

libmspub reads `pWrapPolygonVertices` (Escher `0xC383`) and passes it to
`setShapeClipPath` (`MSPUBParser.cpp` ~line 1946). That property is the
text-wrap outline, not a clipping path. It does not affect our files —
they carry no Escher shapes — but it will silently distort any Publisher
document that does.

Worth filing at <https://bugs.documentfoundation.org/> against the
libmspub component.

---

## 8. The empty headline frames — **answered, and fixed**

They were WordArt, and the words were in the file all along.

### What they turned out to be

`1336 kerkbode.pub` lost most of its headlines because a WordArt object
arrives as two things, neither of them the headline:

- an **empty text frame** — libmspub opens the text object, sets its
  geometry and closes it again with no `insertText` at all, so it is
  dropped for having no text, no fill and no stroke;
- a **path of two disconnected two-point edges**, which outlines no area.
  These are the guides the glyphs are stretched between, which is why
  joining them into a filled quad invented shapes (commit d43cdfe,
  reverted in 197368d). Filled, it draws nothing at all; stroked, it draws
  the guides themselves across the words.

The two are the same object: in `1336` the 15 empty frames, the 15
degenerate paths and the 15 WordArt shapes are one set of 15, and each
guide path sits inside the frame that reported it.

### Where the words were

In the **Escher stream**, as shape properties libmspub has no constants
for and never reads:

```
0x00C0  gtextUNICODE   the text, UTF-16LE
0x00C3  gtextSize      point size, 16.16 fixed point
0x00C5  gtextFont      font name, UTF-16LE
0x0004  rotation       degrees, 16.16 fixed point
```

The earlier string scan missed them for a reason worth remembering: it
diffed *words* against the 1,427 the conversion already delivered, and
headline words like `Kerkdiensten` also appear in body copy, so they were
filtered as matches. Two further details had to be right before the
records could even be walked — a DGG or DG container is followed by four
bytes of tail, and a property id is the low fourteen bits of its entry, the
top two being a complex flag and a blip flag. Without the tail the walk
desyncs after the first container and finds nothing at all, which is
exactly what the first attempt here did.

### What now happens

`pubfile._read_wordart` reads every WordArt shape and
`convert._recover_wordart` replaces its guide path with a text frame
carrying the words, in the band the anchor gives, at the stated size, and
painted the way libmspub reported the same shape painted. 47 headlines
across the corpus, every one of them previously lost.

One shape can arrive as **several** guide paths, because libmspub makes a
draw call per paint: a headline that is filled *and* outlined reports the
same two edges twice. All of a shape's paths are collected before any are
replaced, merged into one frame — fill for the glyph colour, ramp for
their ramp, stroke for their outline — and the repeats dropped. Taken one
at a time they left a pair of rules drawn across all four gradient-filled
mastheads in the corpus.

The match is on the **geometry**, not the paint. Reading `fill is not
None` was what hid `'Il Cantico dei Cantici'`: its glyphs are filled with
a texture, so libmspub reports the fill as a bitmap and the only colour on
the shape is the outline. What the paint says is *how* the guides were
drawn, which is a separate question from whether they are guides.

### What is left

- **Two shapes state no point size** (a drop cap and the masthead), because
  WordArt fits glyphs to the shape rather than setting a size. They are
  sized from the band using the ratio the 39 sized shapes measure — 1.33,
  spread 1.02 to 1.59 — and the report says so. Nothing more precise is
  available without laying out the font.
- **One shape in `Cantico_dei_Cantici.pub` cannot be placed.** libmspub
  reports no shape at all where the file puts it, so there is no guide
  path to replace and no confirmation of where the words went. It is named
  in the report instead: `'I venerdì 2006 di Avvento'`. Placing it on the
  strength of the .pub alone is possible — the anchor gives a band — but it
  would be the only content in the converter put on the page without the
  event stream agreeing, and the first version of this feature is not the
  place to start.
- **WordArt is not WordArt any more.** Arched, stretched and outlined type
  has no IDML equivalent; the headline arrives as straight text and the
  file is flagged `review`. Straight text is placed centred in the band
  both ways, since fitting the glyphs to the shape is what WordArt does
  and the band therefore *is* the words. The frame stays exactly the
  band; a taller one with room for a wrapped headline was tried and taken
  back out, because it depends on the reader centring vertically and
  misplaces the headline by half a band if it does not.

---

## 9. Make a styled table  ⏰ needs Publisher, before 1 Oct 2026

### Why this matters

A Publisher table now converts as a real IDML table, with its grid, its
spans and its per-cell insets. What still cannot be carried is per-cell
**fill and ruling** — and, unusually for this list, not because the file
is unreadable. There is simply nothing to read: **no table in any of the
nine sample files records either**, so there is no case to decode against.
One deliberately formatted table would settle it, and the reader it plugs
into is already written.

### What is already known

- libmspub reads four fields of a cell record — the first and last row and
  column — and skips the rest, its own source marking them
  `// TODO: 0x09 - 0x0e: width/height of content + margins?`. Upstream
  master is identical to the 0.1.5 release here, so nobody has taken it
  further.
- The whole vocabulary the corpus uses is now known: a table chunk (type
  `0x10`) carries the row and column counts, the total size, the seqnum of
  its cells chunk and the row/column size array; a cells chunk (type
  `0x63`) carries one record per cell holding `0x01`-`0x04` bounds,
  `0x0A`-`0x0D` insets, and `0x07`, `0x09` and `0x0E`.
- **Omitted means absent, not defaulted.** One table writes its insets as
  36576 EMU — Publisher's own 0.04in default — explicitly on all four
  sides of all 21 cells, so the writer states the value it means.
  Therefore the absence of any fill or line field across all 1,260 cell
  records in the corpus says those tables are genuinely unstyled, rather
  than styled somewhere this does not look.
- A table's own fill and border are **not** affected: libmspub draws those
  as an ordinary shape behind the table, and they already convert.
- Two fields remain unidentified. `0x07` holds 1 or 2, is uniform across
  every table that has it, and is absent from the rest — consistent with
  vertical alignment, whose enumeration in this format is top(0),
  middle(1), bottom(2), but consistent with other per-table switches too.
  It sits on 869 of the 974 cells converted, so a wrong guess would move
  text in nearly all of them; that is why it is not converted. `0x0E`
  holds exactly an eighth or a quarter of an inch and correlates with
  nothing in the grid.

### Step 1 — produce the sample files (on Windows, with Publisher, ~10 min)

Three small files, each a single 3 x 3 table on one page, otherwise
identical.

1. **The control.** Insert a 3 x 3 table, type `1` to `9` in the cells,
   change nothing else. Save as `table-plain.pub`.
2. **The styled one.** Same table, then, using whichever fill and border
   controls that Publisher version offers — the ribbon's table Design tab
   in 2010 and later, **Format → Borders and Shading** before that:
   - shade cell R1C1 solid red and R1C2 solid yellow, leaving R1C3
     unshaded;
   - give cell R2C1 a 4pt blue border on **all** sides, and R2C2 a 4pt
     blue border on its **top edge only**;
   - leave row 3 untouched.
   Save as `table-styled.pub`.
   Two shades and two border shapes, because one of each cannot tell a
   colour apart from a weight, nor a per-cell record from a per-edge one.
3. **The alignment one.** Same table as the control, but make every row
   1 inch tall (drag the row edges) and set the cells' *vertical*
   alignment — the control lives on the ribbon's table Layout tab in 2010
   and later, and under **Format → Align Text Vertically** before that —
   to top down column 1, centre down column 2 and bottom down column 3.
   Save as `table-valign.pub`.
   This is the file that identifies `0x07`, and it needs the tall rows or
   the difference is invisible.
4. Copy all three to `files/table-samples/` on the Mac.

### Step 2 — read the records (on the Mac, ~1 min)

```sh
cd ~/prive/tools/affinity-converter
python3 research/table_cells.py files/table-samples/*.pub
```

It prints every table's grid and every field of every cell record, named
where the meaning is settled and as a bare id where it is not, which is
exactly the comparison needed.

### Step 3 — read the result

- Diff `table-plain.pub` against `table-styled.pub`. The fields that
  appear only in the styled one are fill and ruling. Expect the fill to be
  an index into the document palette rather than an RGB triple — that is
  how shape fills are stored, and `model` already resolves palette indices
  for those.
- R1C1 against R1C2 separates the *colour* from the fact of being filled.
  R2C1 against R2C2 says whether ruling is one field per cell or one per
  edge: if R2C2 carries a single field where R2C1 carries four, it is per
  edge, which is also what IDML wants — `Cell` takes
  `TopEdgeStrokeWeight` and `TopEdgeStrokeColor` and a set for each side.
- In `table-valign.pub`, if `0x07` reads 0-or-absent, 1 and 2 down the
  three columns, it is vertical alignment and maps straight onto IDML's
  `VerticalJustification` (`TopAlign`, `CenterAlign`, `BottomAlign`). If
  it does not move at all, it is something else and stays unconverted.

### Step 4 — wire it in

`pubfile._table_cells` already walks every field of every record and
returns a per-cell structure keyed to the table; carrying fill and ruling
means adding fields to `TableStructure`, widening `model.TableCell`
alongside `insets`, and writing the matching attributes in
`idml._table_story_part`, next to the inset attributes. The matching, the
plumbing and the tests are all in place — the sample is the only missing
piece.

While in Publisher, also answer `research/probe_cell_insets.py` on the
Affinity side: it builds a table whose rows differ only in their insets,
and no one has yet confirmed that Affinity honours them on import.

---

## 10. Find the default tab interval  ⏰ needs Publisher, before 1 Oct 2026

### Why this matters

Tab stops are now carried (backlog §12), but hardly any tab has one:
across the corpus **203 paragraphs contain a tab and 3 state a stop**.
The other 200 were lined up on Publisher's document-wide default grid —
"Default tab stops" in its Format → Tabs dialog — and that interval has
not been found in the file. So they land on the reader's grid instead,
half an inch in InDesign, and every tabbed column in the document sits
somewhere other than where it was typed. A run of eight tabs, which is
how these authors push a signature to the right, ends up 61pt further
along than Publisher put it, or wraps — and in the three `kerkbode`
issues, if the candidate below is the setting, 223pt further, which is
wider than the page they are set on.

One number would fix all 200: with the interval known, a tabbed
paragraph can be written with an explicit ruler of stops at that spacing
and its tabs land exactly where they did.

### What is already known

Established here, so don't re-derive it:

- **libmspub never reads it.** It reads per-paragraph stops (and drops
  them); there is no default-interval block id in `MSPUBBlockID.h` at all.
- **Publisher names the setting.** It is `DefaultTabStop` on the Document
  object in Publisher's own VBA — "the default tab stop for all text in
  the active publication", valid range 1 to 1584 points, always returned
  in points. So it is a per-publication value, not an application
  preference, and it must be saved with the file. Factory default is
  0.5in / 36pt, which is also what InDesign assumes, so a document that
  never touched the dialog needs nothing done to it.
- **Document chunk block `0x15` is not it.** It reads 359410 EMU in every
  corpus file that carries it and is absent from the two that do not,
  and the three `kerkbode` issues carry it while stating something else
  entirely in the Quill stream. (The reason given here before was wrong:
  it said the US-Letter `Blank Note Card` reads 359410 as well, and that
  file does not carry the block at all. The conclusion still holds, for
  the better reason that the block never varies.)
- **The `SGP ` chunk of the Quill stream is the candidate.** It is a bare
  U32 length and then at most one block — id `0x00`, the same id a tab
  position carries inside a paragraph's stops, type `0x22`, holding a
  U32 of EMU. Length 4 means the block is absent, length 10 means the
  document states one. `research/default_tab.py` prints it. Across the
  corpus it is **absent from both files that contain no tab** and
  **present in all seven that contain one**, and unlike block `0x15` it
  varies: 28.3000pt in `Cantico_dei_Cantici`, `MISSAL` and
  `rotated_text`, 28.2898pt in `Lisa Hoogendijk`, 8.0787pt in all three
  `kerkbode` issues. 8.0787pt against InDesign's 36pt is a
  four-and-a-half-fold error on every tab in the three biggest files in
  the corpus, which is the size of mistake the warning describes.
  It is not proven, though: a document-wide length could be a
  hyphenation zone as easily as a tab interval.
- **The stops themselves are decoded**, so whatever holds the interval is
  a plain length, and Publisher measures in **EMU, 914400 per inch**:
  0.5in reads 457200, 1cm reads 360000, 0.25in reads 228600.
- It need not be in the Quill stream. `research/diff_blocks.py` parses
  the `Contents` stream directly, which is where a per-document setting
  is more likely to live.

### Step 1 — print what the corpus states (on the Mac, ~1 min)

```sh
cd ~/prive/tools/affinity-converter
python3 research/default_tab.py files
```

Nine files, nine numbers, three distinct values. Keep the output; the
`SGP ` column is what Step 2 is checking.

### Step 2 — read the property in Publisher (on Windows, ~5 min)

No sample file needs authoring. `DefaultTabStop` is a document property,
so it can be read straight off the files we already have. Copy `files/`
to the Windows box, and for each one: open it, `Alt+F11` for the VBA
editor, `Ctrl+G` for the immediate window, and type

```vba
? ActiveDocument.DefaultTabStop
```

Points come back either way. Write the number next to the file name.
The three `kerkbode` issues and `Lisa Hoogendijk` are the ones that
matter — they are the files whose `SGP ` value differs.

### Step 3 — read the result

Nine pairs against nine `SGP ` readings:

- **They match** — 8.0787 for the `kerkbode` issues, 28.30 for
  `Cantico`, `MISSAL` and `rotated_text`, 28.2898 for `Lisa Hoogendijk`,
  and 36 for the two files with no `SGP ` block. The field is identified,
  and Step 4 can be wired against it.
- **They don't** — the true numbers are now known per file, which is a
  far better starting point than a blind diff: grep each file for its own
  number as a length in EMU (`value_in_points × 12700`), and if nothing
  turns up, fall back to authoring two files that differ only in the
  setting (0.5" and 2.0", four times apart so no block holding one can be
  confused with a block holding the other) and running
  `research/diff_blocks.py a=… b=…` over the pair.

While reading these, also note whether the number is one the author could
have typed. 8.0787pt is 2.85mm exactly and 28.2898pt is 9.98mm exactly —
neither is a value anybody types into a dialog, so if these *are* default
tab stops they arrived by some route other than the Format → Tabs box,
and that is worth understanding before trusting them.

### Step 4 — wire it in

`pubfile.read_structure` gains the interval; `convert._apply_tab_stops`
gives every tabbed paragraph that states no stops of its own a ruler of
them at that spacing, out to the width of the frame it sits in; and the
warning that currently counts those paragraphs goes away, because they
are no longer landing anywhere unknown. The emitter needs no change:
`idml._emit_tab_stops` already writes a list of stops.

While in Publisher, also settle **which alignment byte is which**. The
reading — `1` right, `2` centre — comes from geometry alone: the 22
stops in the corpus that state one come in pairs, at the middle of a
frame and at its right edge, which is a footer's centre-and-right pair.
A single file with one left, one centre, one right and one decimal tab
in one paragraph, read back with `research/tab_stops.py`, confirms it
outright and says whether a decimal tab has a code at all.
