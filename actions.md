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

## 2. Confirm the rotation sign  ⏰ quick, no Publisher needed

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

## 3. Measure fidelity on your real collection

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

## 4. Build the Windows executable

Everything is prepared; nothing here needs a decision.

Push the repo and let `.github/workflows/build-windows.yml` run — it
builds `pubdump.exe` under MSYS2 UCRT64, bundles it with PyInstaller, runs
the result against the sample files, and uploads `pub2idml.exe` as an
artifact. You need Windows only to *run* the tool, not to build it.

Two things that will only surface on a real run: MSYS2 ships libmspub
**0.1.4** where development used **0.1.5**, and `make dlls` may miss a
transitive ICU DLL. The workflow's smoke-test step catches both.

---

## 5. Report the clip-path bug upstream

libmspub reads `pWrapPolygonVertices` (Escher `0xC383`) and passes it to
`setShapeClipPath` (`MSPUBParser.cpp` ~line 1946). That property is the
text-wrap outline, not a clipping path. It does not affect our files —
they carry no Escher shapes — but it will silently distort any Publisher
document that does.

Worth filing at <https://bugs.documentfoundation.org/> against the
libmspub component.
