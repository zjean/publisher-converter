# Actions requiring a human

Things that cannot be finished from this machine, with everything else
already prepared so each one is short to execute.

Ordered by deadline, then value.

> **A Publisher session has happened** (28 August 2026), and it settled
> §2, §9 and §10 — page margins, cell vertical alignment and the default
> tab interval are all read from the file now. §1 and §3 came back
> partially and are still open, and §11 is new: the table ruling that
> session found, which needs no Publisher at all.
>
> [`windows-session.md`](windows-session.md) is the running order for a
> sitting at a Windows machine, with what came back and what did not.
> Publisher retires on 1 October 2026, so what is still marked ⏰ has to
> happen before then. This file stays the reference for *why* each one
> matters and how to wire the answer in.

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

## 2. Recover the page margins — **answered, and wired in**

Publisher was asked directly, with a controlled pair of documents, and
the guides are now read out of every `.pub` and written into the IDML.

### Where they live

`Contents` chunk **`0x4C`**, block **`0x02`** (type `0xA0`), an array of
one container per guide:

| field | meaning |
|---|---|
| `0x01` | its position in EMU, from the top-left of the page |
| `0x02` | present when a band **ends** here |
| `0x03` | present when a band **begins** here |
| `0x04` | present when it is a **margin** guide rather than an interior column or row guide |

The array runs every vertical guide in ascending order and then every
horizontal one. So the four entries carrying `0x04` are the **left,
right, top and bottom margins in that order** — and they are *positions*,
not insets: a right margin of 2cm on a 21cm page is stored as 19cm.

An unflagged entry between the first pair is a **column guide**, which is
the other half of this item and came free with it.

### How it was settled

Two documents, A4, differing only in their margins:

| | `margins-a.pub` | `margins-b.pub` |
|---|---|---|
| left | 360000 EMU = 1.00 cm | 90000 = 0.25 cm |
| right guide | 6840000 = 19.00 cm | 7110000 = 19.75 cm |
| top | 540000 = 1.50 cm | 270000 = 0.75 cm |
| bottom guide | 9792000 = 27.20 cm | 10062000 = 27.95 cm |

Back-computed against A4 that is **1 / 1.5 / 2 / 2.5 cm** for A and
0.25 / 0.75 / 1.25 / 1.75 for B — exactly what was set in the dialog.

Then checked against all nine corpus files, none of which was used to
derive it. Six come back **symmetric** — 0.63cm on `Blank Note Card`,
1.04 on `Cantico`, 1.27 on `MISSAL` and `Lisa Hoogendijk`, 2.5 on
`rotated_text` — which is the shape a margin actually has and not a shape
an arbitrary pair of numbers falls into. The three `kerkbode` issues read
1.4 / 1.5 / 1.6 / 1.7 cm **plus one column guide at 7.33cm**, the middle
of a 14.85cm A5 page: a two-column newsletter, which is what they are.

### What was wired in

- `pubfile._read_guides` reads the array into `PageGuides`, which holds
  the positions and resolves them against a page size on request. A file
  that does not hold this shape — margins not bounding the list, the two
  axes interleaved, guides crossing — reads as **no guides at all**
  rather than as four numbers that might be anything.
- `convert._apply_page_margins` puts them on every page and master.
- `idml._emit_margins` writes a `MarginPreference` on each `Page`.
  Publisher draws a guide, not a gutter, so a column guide comes out as
  `ColumnGutter="0"` and columns that meet.

Pinned by `GuideReadingTest`, `GuideMarginTest`, `RealGuideTest` and
`NewsletterGuideTest` in `tests/test_pubfile.py`, and
`MarginPreferenceTest` in `tests/test_idml.py`.

### What is still open

Affinity's own handling of `MarginPreference` has **not** been observed —
the attribute is written to the IDML spec and the numbers are verified in
the XML, not on screen. Worth one look the next time a converted file is
opened.

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

## 4. Confirm the rotation sign — **answered against libmspub**

Rotation **magnitude and pivot** were already verified (a 300 dpi render
measured 274.6 × 175.9 pt against a predicted 274.8 × 176.0 for a 300×30
bar at 30°, rotated about its centre). The **direction** is now verified
too, and no rendering was needed.

### How it was settled

libmspub reports a rotated shape **twice**: once as `librevenge:rotate`
on the object, and once as a `drawPolygon` giving that shape's outline in
absolute page coordinates, which libmspub computes from the property
itself. So the polygon is libmspub stating how it reads its own field,
and the question is arithmetic — does the `ItemTransform` `idml.py`
writes put the frame's corners on that outline?

```sh
python3 research/rotation_sign.py files
```

Across the corpus every object whose outline can be matched lands within
**0.5pt**, and the shapes that can tell the two signs apart are decisive:

| file | angle | size | as written | sign negated |
|---|---|---|---|---|
| `rotated_text.pub` | −46° | 755.8 × 1154.6pt | **0.458pt** | 305.7pt |
| `1336 kerkbode.pub` | −179° | 380.8 × 32.5pt | **0.005pt** | 6.7pt |

A shape at ±90° or ±180° reads small either way — the two signs give the
same footprint — so the probe prints both columns and those rows are
simply not evidence.

Pinned by `RotationSignTest` in `tests/test_idml.py`, which carries the
corpus's own numbers so the check survives without the sample files.

The LibreOffice render was done as well and agrees: the text in
`rotated_text.pub` runs **up** and to the right, which is what −46° means
in a y-down system.

### What is still open, and where it moved

This settles us against **libmspub** — which is also all the LibreOffice
comparison could ever have settled, since LibreOffice drives the same
library. Whether *libmspub* matches *Publisher* is a different question
and needs Publisher. It costs nothing extra: any PDF exported from
Publisher for actions §1–§3 answers it, so it is one line on the
Publisher checklist rather than an action of its own.

### An aside, checked and clear

The probe first reported a 386pt error on `Blank Note Card` and 592pt on
one `1336` shape. Both were the probe pairing an object with a polygon
that was not its own — the note card draws a small logo box immediately
before its rotated credit block. Confirmed correct against LibreOffice:
that sheet is laid out with the credits on the left and the photographs
on the right, exactly as libmspub's numbers say. The probe now requires a
polygon to measure the same box before it will pair with an object.

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
- **The one shape libmspub reports nothing for is now placed too, and the
  page came out of the file.** `'I venerdì 2006 di Avvento'` in
  `Cantico_dei_Cantici.pub` has no guide path at all, so there was nothing
  to replace and no confirmation of where the words went. The confirmation
  turned out to be one record away: every Escher shape carries its own
  seqnum in `CLIENT_DATA` (`0xF011`, field `0x6801`), and every page chunk
  lists the seqnums of the shapes on it — 519 shapes across the corpus, none
  listed by two pages. So the file says which page the orphan is on.

  Which page *libmspub* made of that chunk is a separate question, and the
  chunk order is not the answer — it is a permutation of the event stream's
  page order in every multi-page file in the corpus, which is a bug in the
  master pass and is written up as `backlog.md` §14. So the mapping is
  measured: match each shape's anchor to the items libmspub drew, and a
  shape landing on exactly one page settles its whole page chunk. A chunk
  whose shapes disagree settles nothing, which is how a master — replayed
  onto every page — stays out of it.

  Placing then needs both halves of the event stream's agreement: another
  shape of the same page chunk reported, so the page is known, and nothing
  drawn across the band, so a headline that also arrived as an ordinary
  frame cannot be written twice. Missing either, the shape is named in the
  report as before, with which of the two was missing. 48 of the corpus's
  48 WordArt shapes now convert.
- **WordArt is not WordArt any more — but ask the file how much of it was
  WordArt.** The shape record's `instance` is the shape type, and 136
  (`msosptTextPlainText`) is unbent type that is only fitted to its band.
  **47 of the corpus's 48 shapes read 136.** The one that does not is 147,
  a *button curve*, and it is the shape libmspub reports nothing for
  anyway. So the report no longer tells every reader that fifteen
  headlines may need restyling: it says none of them is bent where none
  is, and names the preset where one is. Bending is still the one part
  that cannot be carried — IDML has no warped type — and a file with a
  bent shape is still flagged `review`.

  Straight text is placed centred in the band both ways, since fitting the
  glyphs to the shape is what WordArt does and the band therefore *is* the
  words. The frame stays exactly the band; a taller one with room for a
  wrapped headline was tried and taken back out, because it depends on the
  reader centring vertically and misplaces the headline by half a band if
  it does not.
- **The character formatting was on the shape, and was being dropped with
  it.** WordArt states bold, italic, underline, strikethrough, small caps
  and the rest as sixteen booleans packed into property `0x00FF`: the low
  half their values, the high half which of them the file states at all. A
  bit the file leaves out is unstated, not false. MS-ODRAW writes a boolean
  set with the *highest* id in the group in the low bit, so bit *n* is
  property `0xFF - n` — italic `0xFB`, bold `0xFA`. Reading it the other
  way round makes every headline in the corpus bold *and* small caps
  *and* tight while `gtextSpacing` says loose, which is how the order was
  settled. 40 of the 48 shapes are italic and one is bold; all of them
  used to come out regular.

  Spacing (`0x00C4`) is a multiple of normal — 1.2 is the gallery's Loose,
  and 36 shapes state it — where IDML's tracking is the space *added*, in
  thousandths of an em. The multiple scales each glyph's advance and an
  advance is not an em, so it is converted against an average of half an em
  per glyph and the report says the tracking is close rather than exact.
  Small caps and WordArt's own shadow flag sit two bits away in the same
  word and are deliberately left alone: no shape in the corpus sets either,
  so there is nothing to check a reading of them against, and the model has
  nowhere to put small caps yet (`backlog.md` §10).

---

## 9. The styled table — **answered; alignment wired in, ruling moved to §11**

The three sample files were made in Publisher and they answered both
halves of this item, in opposite directions.

### Cell field `0x07` is vertical alignment

`table-valign.pub` — one 3 x 3 table on tall rows, top down column 1,
centre down column 2, bottom down column 3 — reads exactly that:

| column | as set in Publisher | `0x07` |
|---|---|---|
| 1 | top | *absent* |
| 2 | centre | 1 |
| 3 | bottom | 2 |

So the enumeration is top(0), middle(1), bottom(2), and Publisher leaves
the field out for top the way it leaves out an inset of zero. That was
the reading this item most needed: `0x07` sits on 869 of the 974 cells
converted, so it moves text in nearly all of them.

Wired in: `pubfile._table_cells` reads it into `TableStructure.alignments`,
`convert._apply_cell_insets` puts it on `model.TableCell.vertical_align`,
and `idml` writes `VerticalJustification` — `TopAlign`, `CenterAlign`,
`BottomAlign`. Pinned by `CellAlignmentReadingTest`,
`CellAlignmentApplicationTest` and `RealCellAlignmentTest` in
`tests/test_pubfile.py` and `CellVerticalAlignmentTest` in
`tests/test_idml.py`.

### The control table is unruled, so what already ships is right

`table-plain.pub` prints no lines, which is what this item asked to check
first: the four zero-weight edges the converter writes for every cell it
has read are not deleting a Publisher default.

### Ruling and shading are real, and they are not in the cell records

`table-styled.pub` settles the other half, and refutes the reasoning this
item rested on. The absence of any fill or line field across all 1,260
cell records does **not** mean those tables are unstyled: a styled table
and a plain one have an identical `Contents` chunk inventory, and the
styling is in `EscherStm` instead — a shape per shaded cell and a shape
per ruled edge.

That is now **§11**, which needs no Publisher and no new samples.

### Still open, and cheap

`0x0E` holds exactly an eighth or a quarter of an inch on every cell in
the corpus and correlates with nothing in the grid. Unidentified, and
nothing depends on it.

### What was already established, and still holds

- libmspub reads four fields of a cell record — the first and last row
  and column — and skips the rest, its own source marking them
  `// TODO: 0x09 - 0x0e: width/height of content + margins?`. Upstream
  master is identical to the 0.1.5 release here.
- A table chunk (type `0x10`) carries the row and column counts, the
  total size, the seqnum of its cells chunk and the row/column size
  array; a cells chunk (type `0x63`) carries one record per cell holding
  `0x01`-`0x04` bounds, `0x07` alignment, `0x0A`-`0x0D` insets, `0x09` a
  cached text height and `0x0E`.
- **Omitted means absent, not defaulted** — one table writes Publisher's
  own 0.04in default explicitly on all four sides of all 21 cells. That
  reasoning is still sound; what was wrong was applying it to fields that
  were never going to be in this record at all.
- A table's own fill and border are **not** affected: libmspub draws
  those as an ordinary shape behind the table, and they already convert.
- The reader end needs nothing proved. `research/probe_cell_rules.py`
  has been opened on Affinity Publisher for macOS and shows a per-cell
  edge stroke beating the reader's default, landing per edge, and
  standing alone against a neighbour that states nothing;
  `research/probe_cell_insets.py` is answered on the same machine, all
  four insets honoured and a zero surviving as zero.

---

## 10. The default tab interval — **answered, and the warning is gone**

Publisher was asked, and the field the converter already used is the
right one.

### Why it mattered

Tab stops are carried (backlog §12), but hardly any tab has one: across
the corpus **203 paragraphs contain a tab and 3 state a stop**. The other
200 were lined up on Publisher's document-wide default grid — "Default
tab stops" in its Format → Tabs dialog, `Document.DefaultTabStop` in its
VBA. Left to itself InDesign puts them on its own grid at half an inch,
so a run of eight tabs, which is how these authors push a signature to
the right, ends up 61pt further along than Publisher put it — and in the
three `kerkbode` issues 223pt further, which is wider than the page.

### How it was settled

`? ActiveDocument.DefaultTabStop`, typed into Publisher's Immediate
window, against what the Quill stream's `SGP ` chunk states:

| file | Publisher read back | `SGP ` states | block `0x15` states |
|---|---|---|---|
| `1336 kerkbode.pub` | **8.07874** | 8.0787 | 28.3 |
| `Lisa Hoogendijk.pub` | **28.28976** | 28.2898 | 28.3 |

`Lisa` is the reading that settles it. 28.2898 is a value no other
candidate predicts, and `SGP ` produces it to four decimals; block
`0x15`, the alternative, states a flat 359410 EMU — exactly 28.3 pt — in
every file that carries it, the `kerkbode` issues included. So `0x15` is
a template default and `SGP ` is the value in force.

The `kerkbode` reading is the second half of it: 8.08 was the outcome
that meant "`SGP ` is the field", and it is what came back.

### Where it lives

Quill stream, **`SGP ` chunk**: a bare U32 length and then at most one
block — id `0x00`, type `0x22`, a U32 of EMU. Length 4 means the block is
absent and the document is on Publisher's own half-inch default, which is
also InDesign's, so nothing needs writing for it. `research/default_tab.py`
prints it beside `0x15` for every file in a folder.

Nothing needed building: `pubfile.read_structure` already carried the
interval as `default_tab_stop`, `convert._apply_tab_stops` already ruled
every tabbed paragraph at that spacing, and `idml` already wrote it out.
What changed is the warning, which no longer says the field is unconfirmed.

### One oddity, still unexplained

Neither number is one a person could have typed. 8.0787pt is 2.85mm
exactly and 28.2898pt is 9.98mm — not values anybody enters in the
Format → Tabs box. Publisher reads them back as the setting, so they
*are* the setting; how they got there is a separate question, and not one
that changes what the converter should do.

### Still open, and cheap

**Which alignment byte is which.** The reading — `1` right, `2` centre —
comes from geometry alone: the 22 stops in the corpus that state one come
in pairs, at the middle of a frame and at its right edge, which is a
footer's centre-and-right pair. A single file with one left, one centre,
one right and one decimal tab in one paragraph, read back with
`research/tab_stops.py`, confirms it outright and says whether a decimal
tab has a code at all. ⏰ needs Publisher, before 1 Oct 2026.

---

## 11. Read table rules and shading out of the Escher stream

### Why this matters

18 tables in the corpus arrive with **every cell rule off**, and the
converter says so in each report. That was the right call while the cell
records were the only place looked at — they state padding and alignment
and nothing else, and a cell edge left unstated is one Affinity rules
itself, in a colour and a weight the `.pub` never mentions. But it means
a ruled table arrives unruled and the lines have to be redrawn by hand.

### Where they turned out to be

**Not in `Contents` at all.** A control table and the same table with two
shaded cells and five ruled edges have an *identical* chunk inventory,
and the only difference in their cell records is a cached text extent.

They are in **`EscherStm`**, which grew 1834 → 3494 bytes between the two
saves, gaining a second `DgContainer` holding **seven shapes that the
plain table does not have** — one per shaded cell and one per ruled edge:

| shape | properties | what was set in Publisher |
|---|---|---|
| `spid 2051` | `fillColor=255` → RGB(255,0,0) | R1C1 shaded solid red |
| `spid 2052` | `fillColor=65535` → RGB(255,255,0) | R1C2 shaded solid yellow |
| `spid 2053–2057` | `lineWidth=38100`, `lineJoinStyle=2` | the four edges of R2C1 and the top edge of R2C2 |

Two shades and five edges, which is exactly what was drawn — so the
counting is right even though the individual colours are not yet all
decoded (`2053` reads RGB(0,120,192) where `2054–2057` read 255, and
which shape is which edge needs the anchor rectangles, whose length here
is 4/10/16/28/34/40 bytes rather than OfficeArt's fixed 16).

Escher colours are `0x00BBGGRR`, which is already how `pubfile` reads a
WordArt fill.

### What is already known

Established here, so don't re-derive it:

- `pubfile` **already walks this stream** — `_escher_records`,
  `_read_wordart`, `_read_gradients`, `_read_shape_anchors` — including
  Publisher's two departures from OfficeArt (a `DGG`/`DG` container is
  followed by four bytes of tail; `CLIENT_ANCHOR`/`CLIENT_DATA` repeat
  their own length). What is new is only that a *table's cells* are in
  there as shapes.
- The corpus has plenty to read: ~500 `SpContainer`s per `kerkbode`
  issue, 32 in `Lisa Hoogendijk`.
- `model.TableCell.unruled` and the `idml` code that writes
  `*EdgeStrokeWeight`/`*EdgeStrokeColor` per edge are already in place,
  so a decoded rule has somewhere to go on arrival.
- The samples are `files/experiments/table-plain.pub` and
  `table-styled.pub`, and they do **not** need Publisher again.

### Steps

1. Decode the `0xF010` record Publisher writes in a table's drawing — it
   is not a fixed-size OfficeArt anchor — and place each of the seven
   shapes on the grid.
2. With the shapes placed, read `fillColor` for a shaded cell and
   `lineColor`/`lineWidth` for a ruled edge, and check the five border
   shapes against the four-plus-one that was drawn.
3. Tie the drawing back to its table. Every other Escher reader here
   matches by where a shape sits, and a table already has a grid
   signature; the `CLIENT_DATA` seqnum is the more direct route if the
   table's drawing carries one.
4. Then `convert._apply_cell_insets` sets the rules it has and leaves
   `unruled` set only for cells with none, and the warning shrinks to the
   tables that really state nothing.

**Not urgent, and not blocked.** Everything needed is already on this
Mac.
