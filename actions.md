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

## 8. Identify the empty headline frames  ⏰ needs Publisher, before 1 Oct 2026

### Why this matters

Converting `1336 kerkbode.pub` loses most of its headlines. Everything
else about that file now reconciles exactly, so this is the last unexplained
content loss in the sample set, and it is the most visible one — a
newsletter without its headings.

The cause is upstream: libmspub reports the frames but hands over **no
text for them**. They are then dropped, correctly by the rules the
converter has, because an empty frame with no fill and no stroke
contributes nothing (`model._on_endTextObject`). What is not known is
*why* the text is missing, and that determines whether it is recoverable
at all.

### What is already known

Established here; don't re-derive it.

- **36 of 101 `startTextObject` events carry no `insertText` at all.** The
  parser opens the object, sets its geometry, and closes it again.
- **15 of those are headline-shaped**, on a page 421.0 x 595.0 pt (A5).
  Most sit at the very top of a page, the rest between articles:

  ```
  page    x      y      w      h
     3    6.3  418.3  392.8   33.4
     3   37.3   30.6  346.3   32.0
     4   45.4  285.1  336.0   41.8
     5   24.6   34.6  373.4   32.4
     5   14.8  313.9  380.8   32.5
     5   15.1  478.9  372.5   28.9
     7   21.5   34.7  356.2   36.1
     7   21.5  277.8  371.3   32.9
     9   38.6   37.8  347.1   31.1
    11  102.5   -6.0  295.5   91.6
    11   91.8   -1.6  331.2   47.8
    15   38.1   41.9  337.5   40.8
    19   35.2   45.0  350.6   39.7
    24   30.4   27.6  360.2   26.2
    26   36.3   33.7  352.3   34.9
  ```

- **Nothing else in the file is being lost.** The pipeline reconciles:
  132 `drawPolygon` events are 59 real shapes plus 73 carrying pictures,
  and text frames falling 86 -> 63 is entirely the page-number footer
  being lifted onto four masters. The only other loss is 64 WMF ornaments
  of 8.3 x 8.3 pt, all on page 8, which are not headers.
- **No headline text is sitting unread in the file's text streams.** Every
  string in the `.pub`, in both Latin-1 and UTF-16LE, was diffed against
  the 1,427 distinct words the conversion delivers. The only unmatched
  candidates are object names — `randillustratie`, `prlogo`,
  `advertentie`, `websiteformulier` — not headline copy. So the text is
  either somewhere the string scan cannot see it, or it is not stored as
  text at all.

- **There is a second symptom in the same bands, and it may be the same
  object.** Each of these files also carries 15 paths that libmspub reports
  as two disconnected two-point edges with a fill and no stroke. They sit
  in the headline bands, they are headline-sized (110–225pt wide, 20–32pt
  tall), and they enclose no area, so they draw nothing. Until recently
  they were welded into a single outline, which drew a filled bowtie across
  the page — that is fixed, and they are now reported instead.

  On page 4 of `1336 kerkbode.pub` the path is at (172.4, 292.1),
  172.6 x 31.5pt, filled black:

  ```
  M(172.4, 292.1) L(342.9, 292.1) Z    <- top edge
  M(174.4, 323.6) L(345.0, 323.6) Z    <- bottom edge
  ```

  Page 1 has a pair of these at (34.6, 20.4), 338.9 x 163.2pt — one filled
  brown, one stroked darker — which is masthead-sized.

  Two readings fitted, and **one has since been ruled out by experiment.**
  Either these were the *outline* of a filled shape libmspub emitted as
  loose edges — in which case joining top-edge to reversed-bottom-edge
  recovers a band — or they are guide geometry belonging to something else,
  WordArt having exactly this shape in a top and bottom guide, in which
  case they were never meant to be visible.

  The join was implemented and tried. It produced coherent,
  non-self-crossing quads in banner colours, page one's a deliberately
  slanted 318 x 74 pt parallelogram with a stroked twin — so the geometry
  was no help in telling the two apart. Opened in Affinity, the result was
  **shapes that are not in the source document**, so it was reverted
  (commit d43cdfe, reverted immediately after).

  So these are guide geometry, not artwork, and that is a genuine
  narrowing: **whatever the headline objects are, they carry their own
  geometry and their own text, and libmspub hands over neither.** WordArt
  fits that exactly. Step 1 below is now the only way to confirm it, and
  the answer decides whether the text is recoverable from the file or
  whether the honest end state is a warning naming each lost headline.

  Do not re-try the join. It is disproven, and it silently adds filled
  shapes on top of the page.

Regenerate the table with:

```sh
python3 - <<'EOF'
import json, subprocess
src = "files/cgk/1336 kerkbode.pub"
out = subprocess.run(["./bin/pubdump", src], capture_output=True).stdout
page = 0; cur = None; had = False
print("page    x      y      w      h")
for line in out.decode("utf8", "replace").splitlines():
    try: e = json.loads(line)
    except Exception: continue
    if e["e"] == "startPage": page += 1
    elif e["e"] == "startTextObject": cur, had = e.get("p", {}), False
    elif e["e"] == "insertText" and e.get("t", "").strip(): had = True
    elif e["e"] == "endTextObject":
        pt = lambda v: round(float(str(v).replace("in", "")) * 72, 1) if v else 0.0
        if not had and cur and pt(cur.get("svg:width")) > 200:
            print(f'{page:>4} {pt(cur.get("svg:x")):6.1f} {pt(cur.get("svg:y")):6.1f} '
                  f'{pt(cur.get("svg:width")):6.1f} {pt(cur.get("svg:height")):6.1f}')
        cur = None
EOF
```

### Step 1 — look at one of them (on Windows, with Publisher, ~5 min)

Open `1336 kerkbode.pub`. Go to **page 3** and look at the top of the
page: the frame is 346 x 32 pt at 37, 31 — a little over half an inch
down, spanning nearly the full text width. Page 5 has three of them and is
the best page to compare on.

Click the headline and note:

1. **What kind of object is it?** The Publisher status bar and the ribbon
   name it. The candidates that matter:
   - a plain **text box** → the text should have come through, so this is
     a libmspub bug worth reducing to a minimal file and reporting
   - **WordArt** → the text lives in an Escher shape, not the Quill text
     stream, which is why a string scan does not find it
   - a **grouped** object, or a text box with a **fill or outline** → then
     the frame should not have been dropped at all, and the bug is ours
   - a **picture** of the headline → nothing to recover; it should be
     arriving through the image path instead
2. **The exact words**, verbatim including capitalisation. That is what
   makes the next step possible.
3. Whether the text is **rotated or on a path**, and whether the font is
   anything unusual.

### Step 2 — find the words in the file (on the Mac, ~2 min)

With the exact headline text from step 1:

```sh
python3 - <<'EOF'
needle = "PASTE THE HEADLINE HERE"
raw = open("files/cgk/1336 kerkbode.pub", "rb").read()
for enc in ("latin-1", "utf-16-le", "utf-8", "cp1252"):
    hit = raw.find(needle.encode(enc, "ignore"))
    print(f"{enc:<10} {'at 0x%x' % hit if hit >= 0 else 'not found'}")
EOF
```

Then read the result:

- **Found** → the text is in the file and libmspub is walking past it.
  Which stream it lands in says where: compare the offset against the
  `Contents`, `CONTENTS` (Quill) and `Escher/EscherStm` stream extents,
  which `pubfile._read_stream` can already pull out. If it is in Quill,
  the converter can reach it the same way it already reads master
  structure and the field table — that is a real fix, on this machine.
- **Not found in any encoding** → the headline is not stored as text.
  Most likely WordArt, holding its own glyph outline or a compressed
  copy. Recovering it would mean decoding that, which is a much larger
  job; the honest interim is to *warn* rather than silently drop, so the
  operator knows a headline needs retyping.

### Step 3 — wire in whichever answer it is

If the text is reachable, it joins `pubfile.py`, which exists for exactly
this: things the file says that libmspub does not pass on. If it is not,
add a warning where the empty frame is dropped — a wide, short, empty
frame at the top of a page is a specific enough shape to report as a
probable lost headline, and that is strictly better than the current
silence.

Either way, **stop dropping these silently**. That is the part that does
not need Publisher, and it is what turned this into a surprise rather
than a line in the report.
