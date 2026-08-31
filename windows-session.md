# The Publisher session

**One sitting at a Windows machine with Microsoft Publisher on it.
About 75 minutes. After 1 October 2026 none of it is possible.**

---

## What came back — 28 August 2026

The sitting happened. Files are in `files/experiments/`. What it settled,
and what it did not:

| | outcome |
|---|---|
| **A** default tab interval | ✅ **settled.** 8.07874 on `1336 kerkbode`, 28.28976 on `Lisa Hoogendijk` — both match the `SGP ` chunk to four decimals. `actions.md` §10, wired in, warning removed. |
| **B** text wrap | ◐ **located, not named.** The field is a 4-byte record on the picture shape, and block `0x34` is refuted. But `wrap-square` has a differently sized picture, `wrap-throug` has an extra shape, and `wrap-transparent` was saved where "Behind Text" was asked for — so which value means which setting is not pinned. `actions.md` §1. |
| **C** page margins | ✅ **settled.** `margins-a` reads 1 / 1.5 / 2 / 2.5 cm and `margins-b` 0.25 / 0.75 / 1.25 / 1.75, exactly as set. Checked against all nine corpus files. `actions.md` §2, wired in. |
| **D** field table | ◐ **half.** `field-pagenum` and `field-pagenum-plus-date` arrived; the control (`field-literal-hash`) and the one that pins token to position (`field-pagenum-offset`) did not. Byte 16 of a TOKN is the field-type code — 2 for a page number, 10 for a date. `actions.md` §3. |
| **E** table rules and alignment | ✅ **settled, both halves, and both wired in.** `0x07` is vertical alignment (absent/1/2 = top/centre/bottom). And the rules turned out to live in `EscherStm`, not in the cell records — a shape per shaded cell and per ruled run. That became `actions.md` §11, needed no Publisher, and is now read. ⚠️ The note below says the styled sample was given a 4pt blue border on both cells; the file says 3pt, red on R2C1 and blue only on R2C2's top, and the reading agrees with itself on that. Treat the file as the ground truth here, not this page. |
| **F** PDF exports | ◐ **3 of 15.** `1336`, `1338` and `Lisa Hoogendijk` only. **`rotated_text.pdf` is missing**, so the rotation-sign question against Publisher is still open. |
| **G** fonts | ❌ **not copied.** `~/Library/Fonts` still has only Monotype Corsiva and Pristina from an earlier trip. |
| **H** the executable | ❓ unrecorded. |

### If there is another sitting

Much shorter than the first: **`rotated_text.pdf` and the six remaining
corpus PDFs** (irreplaceable), **the five fonts** (a drag-and-drop),
**two field files**, and **six clean wrap saves** — one variable at a
time, nothing else touched, including the picture's size.

---

Publisher retires on 1 October 2026 (13 October for perpetual Office
2021), and after that `.pub` files cannot be opened in Publisher at all.
Five things in `actions.md` are waiting on a reading that only Publisher
can give, and each one is already prepared down to the click: the sample
files are specified, the diff scripts are written, and the code that
consumes the answer is in place. What is missing is ten minutes of
clicking, five times over.

This is the running order. Every step says where it happens — **[W]** on
the Windows machine, **[M]** back on the Mac.

---

## Before you start

**[W] Copy two things onto the Windows machine:**

1. The `files/` folder from this repo (all nine `.pub` files, including
   `files/cgk/`). Task A reads a property off them directly.
2. Nothing else. Everything that parses is run on the Mac afterwards.

**[W] Make one folder to save into**, say `Desktop\pub-samples\`. You will
come back with about fifteen small files.

---

## A. Read the default tab interval  · 5 min · [W]

**The single highest-value reading, and the cheapest.** 200 of the
corpus's 203 tabbed paragraphs state no stop of their own. They were
lined up on Publisher's document-wide default; InDesign assumes half an
inch, and on the `kerkbode` issues that is out by a factor of four and a
half — a run of eight tabs lands wider than the page.

The converter already writes an explicit ruler at the interval it thinks
it has found. This reading is what tells us whether it found the right
field.

**Open `1336 kerkbode.pub` first — it is the only file where the two
candidate fields predict different answers.**

1. Open the file.
2. `Alt+F11` → the VBA editor. `Ctrl+G` → the Immediate window.
3. Type, and press Enter:

   ```vba
   ? ActiveDocument.DefaultTabStop
   ```

4. Write the number down. It comes back in points either way.

| what it reads | what it means |
|---|---|
| **≈ 8.08** | the `SGP ` chunk is the field. Done — delete the warning. |
| **≈ 28.3** | `SGP ` is something else; block `0x15` is the candidate. |
| **36** | neither; the interval is not in either place. |

Repeat for the other eight files if there is time — `Lisa Hoogendijk.pub`
is the next most useful, as the only file stating 28.2898 rather than a
flat 28.3. Expected, if the current reading is right: 8.0787 for the
three `kerkbode` issues, 28.30 for `Cantico`, `MISSAL` and
`rotated_text`, 28.2898 for `Lisa Hoogendijk`, and 36 for the two files
with no `SGP ` block at all.

> While you are in there, also note whether the number is one a person
> could have typed. 8.0787 pt is 2.85 mm exactly and 28.2898 pt is
> 9.98 mm — neither is a value anybody enters in a dialog, so if these
> *are* tab stops they arrived by some route other than the Format → Tabs
> box, and that is worth understanding before trusting them.

*(actions.md §10)*

---

## B. Text wrap · 10 min · [W]

libmspub exposes **no wrap information at all**. Every image currently
gets a guessed bounding-box wrap, which is right for most documents and
wrong wherever artwork was deliberately layered behind text.

**Change exactly one thing between saves — nothing else, not even
scrolling the picture.**

1. New blank document. Add a text box, paste in a few paragraphs of
   filler, enough that text would visibly reflow.
2. Insert any picture, overlapping the text.
3. Picture Format → Wrap Text → **Square**. Save as `wrap-square.pub`.
4. Now change *only* the wrap setting, and *Save As* each time:

   - **None** (In Front of Text) → `wrap-none.pub`
   - **Tight** → `wrap-tight.pub`
   - **Through** → `wrap-through.pub`
   - **Top and Bottom** → `wrap-topbottom.pub`
   - **Behind Text** → `wrap-behind.pub`

Six files. The shortlist of candidate blocks is already narrowed to
`0x34` (on 14 of 16 pictures, never on text) — the diff will confirm or
refute it.

*(actions.md §1)*

---

## C. Page margins · 10 min · [W]

Margin and column guides are lost entirely; every converted document
opens in Affinity with default margins.

**Use asymmetric values, so each edge is individually identifiable.**

1. New blank document, letter size. Add a text box with a few paragraphs
   so the page is not empty.
2. Page Design → Margins → Custom Margins:
   **left 0.5" · top 1.0" · right 1.5" · bottom 2.0"**.
   Save as `margins-a.pub`.
3. Change *only* the margins to
   **left 0.25" · top 0.75" · right 1.25" · bottom 1.75"**.
   Save As `margins-b.pub`.

Two files. All eight values are distinct, which is the point — whichever
block holds a margin will read one of them.

*(actions.md §2)*

---

## D. The field table · 10 min · [W]

A page-number field is stored as a bare `#`. The converter can tell that
a document *has* fields, but not which `#` is one — so a document mixing
a page number with a typed `#` needs a human.

Four small files, otherwise identical.

1. New document. View → Master Page, add a text box in the footer.
   **Insert → Page Number**. Back to View → Normal. Add two more pages so
   numbering is visible. Save as `field-pagenum.pub`.
2. Same document, but type a literal `#` in the footer box instead of
   inserting the field. Save as `field-literal-hash.pub`.
   **This is the control** — it must produce no `TOKN`, or a clearly
   different one.
3. Same as 1, but type `Page ` before the field so the `#` sits at a
   known, different character index. Save as `field-pagenum-offset.pub`.
   **This is the one that pins token → position**, which is the missing
   link.
4. Same as 1, plus **Insert → Date & Time** in a second footer box.
   Save as `field-pagenum-plus-date.pub`.

*(actions.md §3)*

---

## E. Table rules and vertical alignment · 10 min · [W]

18 tables in the corpus are currently written with **every cell rule
off**, because their cell records state padding and nothing else. If
Publisher rules them from somewhere outside those records, those tables
arrive unruled and the lines have to be redrawn by hand.

Three small files, each a single 3 × 3 table on one page.

1. **The control.** Insert a 3 × 3 table, type `1` to `9` in the cells,
   change nothing else. Save as `table-plain.pub`.
2. **The styled one.** Same table, then — ribbon's table *Design* tab on
   2010 and later, Format → Borders and Shading before that:
   - shade R1C1 solid **red** and R1C2 solid **yellow**, leave R1C3
     unshaded;
   - give R2C1 a **4pt blue border on all sides**, and R2C2 a **4pt blue
     border on its top edge only**;
   - leave row 3 untouched.

   Save as `table-styled.pub`. Two shades and two border shapes, because
   one of each cannot tell a colour from a weight, nor a per-cell record
   from a per-edge one.
3. **The alignment one.** Same table as the control, but make every row
   **1 inch tall** (drag the row edges) and set the cells' *vertical*
   alignment — ribbon's table *Layout* tab on 2010 and later,
   Format → Align Text Vertically before that — to **top** down column 1,
   **centre** down column 2, **bottom** down column 3.
   Save as `table-valign.pub`.
   The tall rows matter: without them the difference is invisible.

*(actions.md §9)*

---

## F. PDF exports — the ground truth · 10 min · [W]

**Do not skip this. It is the only reference rendering that will ever
exist**, and it answers questions nobody has asked yet.

`File → Export → Create PDF/XPS`, print quality, for:

- **all nine files in `files/`** (including the three `kerkbode` issues
  and `files/cgk/Lisa Hoogendijk.pub`);
- each of the six wrap samples from B.

These are what a fidelity measurement compares against (`actions.md` §5),
and they settle one open question outright: **whether libmspub's rotation
sign matches Publisher's.** Ours is now verified against libmspub to
within half a point, and LibreOffice agrees — but LibreOffice drives the
same library, so only Publisher's own rendering can close it. Open
`rotated_text.pdf` from Publisher next to
`converted/rotated_text.idml` in Affinity: if the text tilts the same
way, it is settled.

---

## G. Fonts · 5 min · [W]

Five faces the corpus uses are not installed on the Mac. A substituted
face changes glyph widths, which reflows lines — and that is measurably
the cause of the converted tables overflowing their rows.

Copy from `C:\Windows\Fonts`:

| font | sets | in |
|---|---|---|
| Maiandra GD | 294 spans | MISSAL MARIANA E PEDRO |
| Gill Sans MT | 40 | Bus Meeting Zones |
| Mystical Woods Rough Script | 9 | the Psalm 119 quote in 1336 kerkbode |
| Gabriola | 7 | MISSAL |
| Calisto MT | 3 | Bus Meeting |

Skip Aptos and EYInterstate — each appears in exactly one span whose only
content is a space.

Also worth grabbing while you are there, even though the Mac has them:
**Monotype Corsiva** and **Pristina**. They set 46 of the corpus's 48
WordArt headlines and ship with Office rather than with an operating
system, so any *other* machine running the converter will want them.

---

## H. Try the executable · 5 min · [W]

The Windows build is green and published, and has never been run on a
real Windows machine.

Download and run:

```
https://github.com/zjean/publisher-converter/releases/download/latest/pub2idml.exe
```

It is unsigned, so SmartScreen shows a warning on first run:
**More info → Run anyway**.

```
pub2idml.exe files -o converted
```

No Python, no MSYS2, no Publisher needed. Confirm it converts all nine
and writes `conversion-report.csv`. That is the last untested link in the
chain that actually delivers this tool to where the `.pub` files live.

---

## I. The booklet flag · 10 min · [W]

`--facing-pages` has to be typed on the command line, because nothing
libmspub passes on says whether the publication was set up as a booklet.
Without it a newsletter converts as single pages and no print order
Affinity produces can be right.

**One document, saved three ways, changing only the page setup.**

1. New blank document, A5 portrait. Add a text box on the page and
   **insert four pages** so there is a fold to describe.
2. Page Design → Page Setup → **One page per sheet**.
   Save as `layout-single.pub`.
3. Page Setup → **Booklet** (book fold). Save As `layout-booklet.pub`.
4. Page Setup → **Multiple pages per sheet**, two up. Save As
   `layout-2up.pub`.

Three files. The third one matters as much as the second: a booklet and a
2-up flyer both put two pages on a sheet and only one of them is facing,
so a field that only counts pages per sheet is not the flag.

While you are here, note what Publisher says the **page count** of `1336
kerkbode.pub` is, on screen. It should read 28.

*(actions.md §12)*

---

## Coming home · [M]

Copy back:

| from Windows | to |
|---|---|
| the six wrap files | `files/wrap-samples/` |
| the two margin files | `files/margin-samples/` |
| the four field files | `files/field-samples/` |
| the three table files | `files/table-samples/` |
| the three layout files | `files/layout-samples/` |
| every PDF | `files/reference-pdfs/` |
| the fonts | `~/Library/Fonts` |

Then, on the Mac — each takes about a minute:

```sh
cd ~/prive/tools/affinity-converter

# A — nothing to run; the numbers you wrote down are the answer.
#     Compare them against what the files state:
python3 research/default_tab.py files

# B — build the debug-instrumented pubdump first (actions.md §1 Step 1),
#     then:
python3 research/diff_wrap.py /tmp/pubdump_debug \
    square=files/wrap-samples/wrap-square.pub \
    none=files/wrap-samples/wrap-none.pub \
    tight=files/wrap-samples/wrap-tight.pub \
    through=files/wrap-samples/wrap-through.pub \
    topbottom=files/wrap-samples/wrap-topbottom.pub \
    behind=files/wrap-samples/wrap-behind.pub

# C
python3 research/diff_blocks.py \
    a=files/margin-samples/margins-a.pub \
    b=files/margin-samples/margins-b.pub

# D
python3 research/quill_tokens.py files/field-samples/*.pub

# E
python3 research/table_cells.py files/table-samples/*.pub

# I — chunk 0x8F block 0x0A is the one to watch (actions.md §12)
python3 research/diff_blocks.py \
  single=files/layout-samples/layout-single.pub \
  booklet=files/layout-samples/layout-booklet.pub \
  twoup=files/layout-samples/layout-2up.pub

# G — confirm the fonts took
python3 -m research.font_metrics "Maiandra GD"
```

Each script's own docstring says how to read what it prints, and
`actions.md` carries the "wire it in" step for each — none of them is
more than a few lines, because the code that consumes the answer is
already written.

---

## If you only have twenty minutes

Do **A** (5 min), **F** (10 min) and **G** (5 min).

A settles a field that is currently wrong on 200 paragraphs across the
three biggest documents in the collection. F is irreplaceable: it is the
only Publisher rendering that will ever exist, and every later question
about fidelity is answered against it. G costs nothing and removes a
whole class of false conversion defect.

B, C, D and E each improve the conversion. A and F are the ones that
cannot be reconstructed afterwards by any amount of work.
