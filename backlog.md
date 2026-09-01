# Backlog

Work identified and deliberately deferred, all of it doable **on this
machine** — no Publisher, no Windows, no new dependency. Things that need
a human with Publisher live in `actions.md` instead.

Ordered by value. Each item carries the evidence already established, so
none of it has to be re-derived.

---

## 1. Translate WMF drawing records into native IDML paths — **done**

Landed: `pubidml/wmf.py`, wired into `convert._rasterise_items`. The six
drawing records become `model.Polygon` / `Rectangle` / `Ellipse` inside a
`Group`, mapped from the metafile's logical window onto the placed frame.

Recovered across the corpus: 384 shapes in each kerkbode file (64 copies
of one 6-shape emblem) and 3,060 in `Lisa Hoogendijk.pub`, with zero
unsupported records — the vocabulary really was closed. Every artwork
warning in the corpus is gone.

Two approximations remain, both documented in the README: a
`META_POLYPOLYGON` becomes one polygon per ring rather than a single
subpathed shape with holes, and only the anisotropic window mapping is
implemented. Extending `_path_from_anchors` to emit several
`GeometryPathType` entries would fix the first properly.

Watch the file size: repeating one 920-point emblem 64 times took
`1336 kerkbode.idml` from 110KB to 942KB. IDML has no symbol reuse for
this, so it is inherent rather than a bug.

---

## 2. Collapse repeated warnings

### Why this matters

Less pressing than it was: the 11 identical "table flattened" lines that
motivated this are gone, because tables are no longer flattened. What
remains is a latent shape rather than a live problem — any warning emitted
per-item rather than per-document will do the same thing again.

The metafile path shows the pattern that works (`6c8d0e5`, where 419
warnings across the corpus became 20): count by content, report once, and
say how many copies were affected.

### Approach

Either aggregate at the source, as the metafile and threading passes now
do, or add a general collapse step that folds identical warnings into one
line with a count just before the report is written. The second covers
anything added later and is a few lines, but a purpose-written message
reads better than a mechanical suffix — "11 tables flattened" beats
"table flattened … (x11)".

Check `cli.py`'s report writer too: the CSV joins warnings with `; `, so
whatever is done should shorten both the terminal output and the CSV.

---

## 3. Real IDML tables instead of flattening — **done**

Landed in `model.Table` / `model.TableCell` and `idml._emit_table`. Nothing
had to be inferred: libmspub reports a width per column, a height per row,
and a row/column pair plus spans on every cell, and
`insertCoveredTableCell` marks the cells a span hides.

The corpus now carries 18 real tables across the three newsletters — the
largest 41 rows by 5 columns, 182 cells, 19 of them spanning. Every
counted field in the report is unchanged, which took extracting
`convert._count_content`: a Table is not a TextFrame, so cell text had
silently stopped being counted while being present in the package.

Cell insets followed, read straight out of the .pub in `pubfile._read_tables`
and applied by `convert._apply_cell_insets`: libmspub stops at a cell's row
and column, but the file gives four insets per cell, and the grid the table
draws is enough to tie a cells chunk back to the table in the event stream
— 18 tables, 974 cells, no ambiguous signature anywhere in the corpus.

Still not carried: per-cell fill and rule weights and colours. That is no
longer a limit of the reader but of the corpus — **no table in any sample
file records either**, and since a field the file omits is absent rather
than defaulted, there is nothing to decode against. `actions.md` §9 is the
sample that would unblock it, along with the two cell fields that remain
unidentified. A table's own fill and border already arrive, as the
rectangle libmspub draws behind it.

`research/probe_cell_insets.py` is **answered**, on Affinity Publisher for
macOS: all four attributes are honoured, a zero inset survives as zero
rather than being read as unset, and the 9pt gutter column sets text. It
also showed that the cell *rules* in a converted table are Affinity's own
default table style, since we name `$ID/[Basic Table]` without defining
it — see §9 below.

---

## 4. WMF bitmap-blit records

The EMF side is done: `metafile.embedded_bitmap` unwraps
`EMR_STRETCHDIBITS` straight to PNG with no external tool, which is how
both photographs in the sample set now convert losslessly.

The WMF equivalents are **deliberately not implemented**:

```
0x0F43 META_STRETCHDIB      0x0940 META_DIBBITBLT
0x0B41 META_DIBSTRETCHBLT   0x0D33 META_SETDIBTODEV
```

No file in the corpus contains one, and writing speculative record
parsing against zero samples is how plausible-but-wrong code paths get
in. Add this when a real file turns up — the `_dib_to_png` half is
already written and format-agnostic, so only the record layout is
missing.

---

## 5. Story-threading residuals — **done**

Threading landed in `a052d30`, measuring a chain rather than reading one.
Both gaps this item recorded came from that, and **the file states the
answer to both outright.** A shape chunk (type `0x01`) carries the story
it holds in block `0x27` and its own place in that story in `0x28`, so
the frames sharing a story *are* the chain and the index puts them in
order. The head leaves `0x28` out, the way every field in this format is
left out when it has nothing to say — the same writing rule §11's cell
insets turn on.

- **Link order was page order.** Now it is the order the file states.
  Nothing in the corpus moves, because every chain in it happens to flow
  with the page sequence, so this is a latent fix in the sense §14 was:
  what changes is that the order is *read* rather than coincidental.
- **A chain whose text fits its first frame was not detected.** Now it
  is, and the pair this item named is exactly what turned up:
  `1336 kerkbode.pub`'s two frames of 1,905 characters against room for
  about 2,325 are threaded, and `1337` gains an 80-character pair the
  same way. That is 1,905 and 80 characters of duplicated article no
  longer written twice.

**The page-number risk this item warned about does not arise**, and the
reason is worth keeping: a page-number footer is *one* master shape
replayed onto every page, not a run of frames. Its story is held by a
single shape, and a story one shape holds is not a chain. `1336`'s
stories 2 and 3 are exactly that — one shape each, appearing on 14 and 13
pages — so the record tells a repeated label from a chain without the
capacity guess having to.

**The measured guess is kept, for what the record cannot reach.** A
stated chain is used only when the event stream drew *every* link and all
of them agree about the text; threading part of a chain leaves the rest
holding their copy, so the article still arrives twice. Two cases in the
corpus fall back:

- `Cantico_dei_Cantici.pub`, where only two of a three-frame chain's
  shapes can be placed. The guess sees it plainly — the story oversets —
  and threads all three, so the fallback is what keeps it whole.
- `1337`'s chain of shapes 435 and 410, which **libmspub never drew**:
  their page chunks resolve to pages 22 and 23 and there is no frame at
  either centre, anywhere in the document. The file states a chain of two
  frames that are not in the event stream at all, so there is nothing to
  thread. It was not threaded before this change either.

Matching a shape to its frame is by centre, like WordArt and the
gradients — but **one page wide rather than document wide**, which is
what makes it work: a newsletter repeats its two-column layout, so a
column on page 10 sits exactly where the column on page 12 does. §14's
chunk-to-page mapping narrows it to one page before the centre is asked.
Document-wide matching resolves 1 of `Cantico`'s 3 shapes; page-scoped
resolves 2.

The report now says which half a chain came from — "3 stated by the
file", "1 inferred from the text" — since an order the file stated and
one inferred from where the frames sat are worth checking differently.

---

## 6. Drop shadows — **done**

Found by sweeping every property libmspub reports for the newsletters
against every property `model.py` reads: 91 keys reported, 20 read
nowhere. Most of those 20 carry nothing (see *Not worth doing*); four
items lost something real, and these first two are now landed.

**15 shapes in each of the three newsletters** carried a shadow and
arrived flat:

```
draw:shadow          visible
draw:shadow-color    #c0c0c0 (x12)  #b2b2b2 (x2)  #003366 (x1)
draw:shadow-offset-x 0.0278in / 0.0417in
draw:shadow-offset-y 0.0278in
draw:shadow-opacity  100% / 80% / 50%
```

Landed as `model.Shadow`, read in `GraphicStyle.from_props` and written by
`idml._emit_shadow` as a `DropShadowSetting`. Every shadow in the corpus is
on a `drawPath`, but a text frame or a placed picture can carry one just as
well, so all three emitters write it.

Two decisions worth keeping: blur, spread and noise are written as zero,
because Publisher's shadow is a flat offset copy and a reader's defaults
would soften it; and the offset is written both as X/Y and as an angle with
a distance, since IDML carries both and InDesign keeps them in step. The
angle is where the *light* is, not where the shadow falls — 135 degrees
with both offsets positive, which is InDesign's own default.

`research/probe_shadow.py` is unanswered: whether Affinity honours
`DropShadowSetting`, which of the two offset statements it believes, and
whether `Size="0"` really means no blur. If it ignores the element
entirely, the honest fallback is a warning rather than silence.

---

## 7. A gradient's see-through stop — **done**

**14 gradients in each newsletter had a stop at 60% opacity** and every one
converted fully opaque:

```
svg:stop-opacity  100.0000% (x126)  60.0000% (x28)
```

The worry recorded here was that object-level opacity would fade the
opaque stops too. It does not arise: **all 42 are two-stop ramps with both
stops at 60%**, and every one is on a shape with no stroke, so stating the
whole object as 60% opaque is exact rather than an approximation. Landed as
`GradientStop.opacity`, folded into `GraphicStyle.fill_opacity` only when
the stops agree; where they disagree, `uneven_stop_opacity` records that
nothing was carried instead of averaging into a figure the file never
stated. No corpus file hits that branch.

This pulled a second thing with it. `fill_opacity` was written as
`FillTint`, which is not opacity: a tint mixes the colour with the paper,
so the corpus's four 78% fills came out pale rather than see-through and
looked right only over white. Both now write a `BlendingSetting`.

`research/probe_opacity.py` is unanswered, and it matters more than it
sounds: if Affinity ignores `BlendingSetting`, a 60% panel comes out solid
and hides the page background it was laid over. The fallback is to restore
`FillTint` for solid fills and warn on the gradients.

---

## 8. Language, and one scaled span — **done**

Every span libmspub reports carries a locale and neither `fo:language`
nor `fo:country` was read. Nine locales across the corpus, on every run:

```
nl-NL x5516  it-IT x448  pt-PT x346  en-US x71  en-AU x64
fr-FR x10    sv-SE x3    da-DK x2    de-DE x1
```

Landed as `model.Span.language`, written by `idml` as `AppliedLanguage`
on each `CharacterStyleRange` against a `Language` per locale in the
designmap. Not visual styling, but it decides hyphenation, and Dutch
broken by English rules reflows every line after the first bad break. It
is a second influence on the wrapping §11 measured, not the one it found:
there the cause is the missing fonts, and this only narrows the gap.

Three things were settled against real InDesign output rather than
guessed, since none of it is in any specification to hand:

- **Language elements live in the designmap**, not in `Resources/`, and
  the DOM 8.0 schema puts them ahead of the `idPkg:` references. Two real
  InDesign designmaps confirm both, and the second — a Czech document
  declaring Czech and nothing else — settles that a package declares only
  the languages it uses rather than the whole built-in table.
- **`AppliedLanguage` names a display string**, `$ID/English: USA`, not a
  locale tag, and the id form escapes the colon: `Language/$ID/English%3a
  USA`. The ids and quote marks in `idml._LANGUAGES` are copied from that
  same file.
- **A country IDML does not name falls back to the bare language**, which
  is the same hyphenation dictionary: `fr-CA` and `fr-FR` are both
  French. Where there is no fallback either the run is left alone and
  counted in the report — `en-AU` has no plain "English" behind it, and
  naming a neighbouring dictionary would be choosing one the file never
  did. That is 57 runs in `Bus Meeting Zones & Luggage JLW.pub`.

**The scaled span is carried too**, and it was in `1337` rather than
`1336`. The arithmetic is no longer a guess: `MSPUBParser.cpp` reads the
raw field as tenths of a percent and divides by ten, and the collector
then hands that percentage to librevenge as a *fraction*, which
multiplies by a hundred again. So `9000.0000%` in the event stream is
Publisher's 90%, and `units.percent` — which divides by a hundred — is
exactly the correction. It writes `HorizontalScale="90"`, and a figure
outside what IDML can state (1–1000) is dropped rather than written,
because a misread here stretches a run off the page.

**Unverified on the Affinity side**, and worth a minute the next time the
package is open: click into a Dutch paragraph and read the Character
panel's language. If it says Dutch, hyphenation is following the file. If
it says whatever the install defaults to, `AppliedLanguage` is being
ignored and the honest fallback is to say so in the README rather than
imply the text is hyphenating correctly.

---

## 9. Whose rules is a table drawing? — **ours now**

A converted table named `TableStyle/$ID/[Basic Table]` and the package
never defined it, so the reader supplied its own. Affinity's draws a line
around every cell — visible in `research/probe_cell_insets.py`, where no
stroke was asked for anywhere — and the corpus's tables are layout grids,
so that printed a grid across every article.

**Every cell whose record we read now states all four edges off**, as both
a zero weight and a `Swatch/None` colour: `model.TableCell.unruled`,
`convert._apply_cell_insets`, `idml._table_story_part`. 18 tables, 974
cells, all four edges on each, and a cell whose record was never read is
still left to the reader — the distinction `insets` draws with None. The
reason it can be read that way at all is the format's own rule, that a
field the file leaves out is absent rather than defaulted: a cell record
states padding and nothing else in all 1,260 of them.

`research/probe_cell_rules.py` states cell edge strokes six ways over the
same 3 x 3 table, one page each. It is its own probe rather than a row on
the inset one because that one is answered and this needs a loud control:
a 4pt magenta page no default could produce, without which "unchanged"
cannot be told from "zero read as unset". Nothing in the model was widened
for it — the attributes are patched onto the package the real writer
produced, which keeps the speculation in the probe.

**Two things it settles, on Affinity Publisher for macOS:**

- **A per-cell override wins.** Page E asks for 4pt magenta on all four
  edges of all nine cells and gets it. So Affinity's default is not
  something we are stuck with; what we write about a cell edge is what
  gets drawn.
- **Granularity is per edge, and a stated edge stands alone.** Page F
  states a top edge on the middle row and nothing anywhere else, and
  exactly that one line — the edge the first and second rows share —
  comes out magenta. The cell above it says nothing and does not override
  it. So `actions.md` §9's per-edge sample, one cell bordered on four
  sides and its neighbour on its top alone, maps straight across.

- **A rule can be removed, three ways.** Pages B, C and D ask for no line
  as a weight of zero, as a stroke colour of `Swatch/None`, and as both,
  and none of the three draws anything. A zero is not read as unset, the
  same answer the insets gave. Page A, stating nothing, draws the line
  around every cell — so the difference is ours to make.

Defining `[Basic Table]` ourselves is therefore not needed, and neither
was any new IDML machinery.

**The question that was left here is answered.** It was whether a plain
Publisher table prints lines at all — and if it does, where the weight
and colour of them live. `table-plain.pub` states no rule anywhere, so
the zeros are right for it; `table-styled.pub` states seven shapes in
`EscherStm`, and those are now read onto the cells through the very
attributes this probe validated (`actions.md` §11). A zero is written
only for a side the drawing does not name, so this section's finding —
that a per-cell, per-edge override beats the reader's own grid — is what
carries Publisher's real lines rather than only silencing Affinity's.

---

## 10. The WordArt libmspub reports nothing for — **done**

WordArt headlines convert (`actions.md` §8): the words come out of the
Escher stream and replace the guide path libmspub reports for the same
shape. **One shape in `Cantico_dei_Cantici.pub` had no such path** —
libmspub reports nothing at all where the file puts it — so it used to be
named in the report rather than placed:

```
'I venerdì 2006 di Avvento'      64.4 x  63.5pt, Comic Sans MS, no size stated
```

It is now placed, and the hesitation this item recorded is answered rather
than overruled. Everything but the page was already in the .pub: the anchor
gives the band and the rotation, the properties the words, the font and the
size. **The page was the missing piece, and the file states that too.**

Every Escher shape carries a `CLIENT_DATA` record (`0xF011`) holding its own
seqnum at `0x6801`, and every page chunk lists the seqnums of the shapes on
it — 519 shapes across the corpus, no shape listed by two pages. So the file
says which page the orphan belongs to. What it does **not** say is which
page libmspub emitted for that chunk, and the chunk order is not the answer
(§14). That mapping is measured instead: a shape whose anchor matches an
item libmspub drew on exactly one page says its whole page chunk is that
page, and a chunk whose shapes disagree says nothing — which is also how a
master, replayed onto every page, keeps out of it.

So placement now rests on two confirmations from the event stream, and
needs both:

- **another shape of the same page chunk was reported**, which says which
  page this is. Without it the shape stays unplaced and named, as before.
- **nothing libmspub reported is drawn across the band**, which says the
  headline is not already there by another route — the overlap test this
  item asked for. A page background does not count as something in the way,
  since it covers every band on the page by definition.

The orphan's band on Cantico's first page turns out to be empty — nothing
libmspub drew comes near it — so the guard passes on its own terms rather
than being written around it. Its size also had to be fixed to make this
sane: it is a three-line headline stating no point size, and sizing it from
the whole band instead of a line's share of it made it 48pt rather than
16pt.

**This item used to name two shapes, and was wrong about the second.**
`'Il Cantico dei Cantici'` did have a path — at exactly its centre, in
exactly its band — and the reason it looked absent was that the match
tested the fill. Its glyphs are filled with a texture, so libmspub reports
the shape's fill as a bitmap and its only colour as the outline, and a
test reading `fill is not None` never saw it. The guides drew themselves
across the title instead. Matching on the geometry and letting the paint
say only *how* it was drawn placed it, with the confirmation this item
wanted already in hand. Worth remembering when the remaining one is
attempted: check what libmspub reports before concluding it reports
nothing.

**The shape now names itself, which makes the remaining one easier to
judge.** Its type is 147, a *button curve* — the only bent shape in the
corpus, and the report names it as such. A bent headline is the one kind
straight text cannot stand in for, so if it is ever placed, it is placed
knowing it needs redrawing rather than reviewing.

**Two flags in the same property are read past.** WordArt's booleans
(`0x00FF`) carry small caps at bit 1 and its own shadow at bit 2 beside the
bold and italic now carried. Neither is set on any shape in the corpus, so
neither has a case to check against, and small caps has nowhere to go in
`model.Span` yet — IDML would take it as `Capitalization="SmallCaps"`. A
file that sets either loses it silently, which is the one thing about this
worth fixing when a sample turns up.

---

## 11. Where a table lands

Reported from Affinity: a converted table is placed wrong on the page.
The numbers in the package are not the problem — for all 11 tables
libmspub reports in `1336 kerkbode.pub`, the row heights sum to
`svg:height`, the column widths to `svg:width`, and the frame is written
at the reported `svg:x`/`svg:y` to the point. So it is what a reader does
with a frame whose whole content is a table.

Two things are now measured rather than assumed, both by
`research/probe_table_placement.py` against Affinity:

- **A synthetic table places correctly.** 288 x 144pt, three equal rows,
  drawn over a magenta rectangle at the same coordinates: it lands square
  on it. Frames are positioned right, row heights are honoured, nothing
  grows. So there is no general table placement bug, and whatever is wrong
  is something the real tables have that a made-up one does not.
- **Pinning the first baseline is harmful.** `FirstBaselineOffset =
  "FixedHeight"` with `MinimumFirstBaselineOffset = "0"` reads like the
  right thing for a table, which has no baseline to offset. Affinity
  answers it by lifting the whole table *a full frame height* off its
  position. It was tried, measured, and taken back out; `idml.py` carries
  a comment saying why so it does not get re-invented.

- **Position is not the problem; height is.** The real tables land on
  their rectangles horizontally and at the top, and render *taller* than
  the height Publisher states. On `1336`'s first table — 3 columns, 7
  rows, 262.68pt — the six rows carrying text absorb about 32pt between
  them and push the empty seventh row clean out of the magenta. The three
  tables with substantial wrapped text overflow; the two with short cell
  text sit exactly right. So `SingleRowHeight` is being read as a
  minimum, and the text is wrapping to more lines than Publisher laid out.

### It was the converter after all — **fixed**

This was written up as missing fonts: 7 of the 12 families these documents
use were not installed, Calibri is the body font of these tables,
Publisher's row heights have no slack, and a substitute 1–2% wider turns a
line that just fitted into two. Every part of that is true and **none of it
was the cause.** Calibri and Carlito are both installed now and the tables
still sank. Measured again with **every paragraph forced onto one line**,
so wrapping cannot contribute at all, the drift was still 150 to 620pt a
table. Wrapping was a garnish on a 620pt error.

Two things the converter wrote were:

- **Paragraph space after, inside a cell.** libmspub reports
  `fo:margin-bottom: 0.1944in` — 13.9968pt — on 351 cell paragraphs of the
  corpus, and `_emit_paragraph` wrote it out as `SpaceAfter`. The rows it
  lands in are 9 to 11pt tall. Publisher laid out none of it: those rows
  measure the leading plus under a point (9.7272 against 0.75 × 1.2 ×
  10.0008 = 9.0007), and the row heights sum to the frame's stated height
  to the point, so there is nowhere for 14pt a paragraph to go. A reader
  that does honour it grows every row — and a row only ever grows
  downwards, so the grid sinks out of the frame it was placed in.
- **Nothing at all, in a cell with nothing in it.** Publisher records no
  run — often no paragraph either — in an empty cell, and what states no
  size and no leading is set in the reader's own: 12pt on Auto, against
  rows these documents build at 9.

Both are now written the other way. Space before is dropped on the first
paragraph of a cell and space after on the last, the two edges with
nothing to space away from; space *between* two paragraphs of one cell is
left alone, since that is the job it was set for. An empty cell is set the
way the body of its own table is set — the most used stated size, and the
leading that goes with it, tie-broken to the tighter setting, which cannot
make a row taller than the file states.

Measured over the 18 tables the corpus writes, as the height every row
needs against the height the file states:

```
                    worst table   every table
1336  7 tables    621.5 -> 0.0    all 7 exact
1337  5 tables    112.3 -> 0.0    all 5 exact
1338  6 tables    216.0 -> 26.5   4 of 6 exact
```

Pinned by `CellEdgeSpacingTest`, `EmptyCellTypeSizeTest` and
`EmptyCellLeadingTest` in `tests/test_idml.py`, each of which asserts the
guard as well as the fix: an ordinary text frame keeps the spacing it
states, and a table that states no size or no leading anywhere still
leaves the reader its own.

### The two `1338` tables this left — **both answered, from the PDF**

They were written up here as needing their own measurement. The
measurement turned out to be sitting in `files/experiments`: Publisher's
own PDF export of `1338`, which is a **booklet imposition** — 14 sheets of
914 x 681pt, sheet *n* holding two non-consecutive pages, so page 7 is the
right half of sheet 7. Reading a table off it is reading what Publisher
laid out.

**Spacing above single does not raise the first line.** `1338`'s page-7
agenda states `fo:line-height: 150.0000%` on its date and time cells — 6
paragraphs, the only ones in the file — and 1.5 x 1.2 x 10.0008 is 18pt of
leading in a row the grid gives 9.16pt. Publisher's PDF puts the three
rows **12.12 and 12.24pt** apart: Calibri's natural line, neither the
150% nor the 9.16. So Publisher opens spacing above single *between*
lines and not above the first one, and a cell holding one line is as tall
as that line however wide the spacing is set. IDML cannot say that — its
leading is every line — so a cell paragraph led above single is now
written at the natural line instead, and the table renders 36.0pt in the
35.81pt frame Publisher gives it, where it rendered 54pt before and ran
into the block beneath. Spacing *below* single is left alone: Publisher
does compress a single line, and that is exactly what `1336`'s 9.7pt rows
on 75% cells are. `CellLeadingAboveSingleTest` pins all three cases.

The 101 paragraphs in the corpus that are led above single **outside** a
cell — 95 of them in `MISSAL MARIANA E PEDRO`, at 1.5, 2 and 2.5 spaces —
are untouched. They run to many lines, where the spacing is the layout,
and nothing measures their box the way a row measures a cell's.

## 12. How much room a wrap leaves — **read from the file now**

Reported from Affinity, and measured against Publisher's own PDF: on page
11 of `1337` the copy runs down the side of a portrait, and it ran too
close to it.

Publisher starts those nine wrapped lines at **x=125**; a zero offset
starts them at 117.3. The picture is two Escher shapes, `457` the frame
at (14.33, 19.55)-(120.64, 162.88) and `458` the image inside it at
(15.89, 21.92)-(117.32, 161.25), and **both state a wrap distance of
2.88pt** — 0.04in, Publisher's own default gap. `_emit_text_wrap` was
writing `TextWrapOffset` as zero on all four sides and throwing that away.

The bottom edge mattered more than the extra air, because it decides *how
many* lines are narrow. The wrap ended at 161.25 against the last wrapped
line's box at 163.0, so the column widened one line before Publisher
widens it. With the stated 2.88 the wrap reaches **164.13** and that line
stays in, which is what the PDF shows.

Read per shape (`pubfile._wrap_distances` → `ShapeAnchor.wrap`), matched
to an item by where it sits (`wrap_near`, on the same centre-and-size
handle `gradient_for` uses), and left at zero where no shape matches —
a gap the file does not state is one this would be inventing. 762 of the
corpus's 1,353 items match.

**Still ~4.8pt short, and worth a probe rather than a guess.** With the
distance applied the right edge is 120.20 against Publisher's ≥125. The
best model of the rest is that Publisher wraps the **union** of the two
shapes: `max(120.64 + 2.88, 117.32 + 2.88)` = 123.52, which also keeps the
bottom at 164.13 and lands within 1.5pt. Making it so means the picture's
*frame* wrapping as well as the image inside it, and the two boxes differ
by about a point in each direction because an Escher anchor measures a
shape with its outline while libmspub reports the path inside — so the
match is not clean enough to do on inference. `actions.md` §1 is the
experiment that would settle it.

### A table cannot flow around a picture — **fixed**

Reported from Affinity: the agenda on page 7 of `1337` sits at the wrong
height. Its numbers were not the problem, and this time neither were the
rows. Every item on the page lands within a point of Publisher's own PDF
— the six-row table's frame at 67.34pt against ink at 66.96, the nine-row
table at 122.13 against 122.96 — and the rows sum to 55.008pt in a
55.17pt frame with leading that fits every one of them.

What moves it is the **recycling bin**. Images carry a bounding-box text
wrap (`idml._emit_image`, on a size heuristic, because libmspub reports
no wrap and an image drawn after the copy would otherwise hide it), and
the bin stands 18.4pt into the right edge of that table's frame, over its
whole 55.2pt height. Ordinary copy answers a wrap by narrowing its lines.
**A row has no way to narrow**, so a reader clears the entire table past
the obstruction, and a table pushed off Publisher's coordinates is far
worse than one a picture overlaps — which is exactly what Publisher
itself draws.

`TextFramePreference IgnoreWrap="true"`, on the table frame only. The
switch that turns image wraps on is untouched for text, where reflowing
is the right answer and the reason it exists. The corpus has **83 such
overlaps** across the three newsletters, including page 7 of both `1337`
and `1338` — the same 1338 table the leading fix above was chasing, which
had a second reason to sit low all along. Pinned by
`TablePlacementTest.test_the_frame_ignores_text_wrap`.

**And the 7 x 3 rota was never wrong.** It was recorded here as 10.2pt of
growth in the one cell holding three paragraphs against rows built for
two. Its frame is 155.4pt against a grid of 144.4, and it renders 154.6 —
Publisher grew that row itself and stated the grown frame, so the output
was already right and the 10.2 was **the yardstick being wrong, not the
table**. Growth is only a defect measured against the *frame*; measured
against the grid it also counts the growth Publisher did on purpose.
`research/probe_table_placement.py` no longer has to settle it.

Still short of its frame, and the opposite complaint: `1338`'s two
41-row and 39-row lists render 21.9 and 21.8pt **less** than the frames
they are given, about 0.53pt a row. Nothing overflows and nothing moves,
so it reads as slightly tight text rather than a misplacement — but it is
the same question from the other side, and the PDF can answer it the same
way when it is worth the time.

Separately, and found while measuring the above: **a cell's bottom inset
is zero in almost every cell of the corpus** — which was written up here
as an invention rather than a reading, on the grounds that the four inset
fields `0x0A`–`0x0D` are present in falling numbers and that no Publisher
default produces a zero bottom inset. **Both halves of that were wrong,
and the reading was right.** Settled, so it does not get re-opened:

- **The counts fall away because fewer cells have a bottom inset, not
  because the field is truncated.** Counted over all 1,260 cells rather
  than the 974 matched ones, the four sides are stated 1056, 1037, 1001
  and 195 times — but the omission is per-side, not trailing: 17 cells
  state `0x0B` and leave `0x0A` out entirely. Each side is written on its
  own.
- **A cell states an inset exactly when it has one.** 3,289 sides are
  stated across the corpus and **not one of them is zero**, against 1,751
  left out. A writer that never writes a zero is a writer whose omission
  means zero.
- **The geometry says so independently.** `0x09` on each cell caches the
  height of its laid-out text. On the 454 rows grown to fit that text,
  the row height less the top inset and `0x09` leaves a residual of about
  zero — spread ±1.3pt, clustered on 0.18 and −0.74 — where a 0.04in
  bottom inset would leave a clean 2.88pt.
- **The one table that states `0x0D` is not a counter-example.** It is
  `1338`'s 9x3 full-page layout grid, whose rows are fixed at 103pt and
  9.4pt rather than grown, so the arithmetic above does not apply to it —
  and it states all four sides as 2.88pt, the 0.04in default, because
  that grid's margins were never touched. A table that means the default
  writes the default.

So `_table_cells` reads it correctly and there is nothing to fix. Pinned
by `test_a_cell_states_an_inset_exactly_when_it_has_one` and
`test_a_cell_leaves_sides_out_sparsely_rather_than_truncating`, which
fail the moment a file turns up whose writer states a zero.

Also recorded here, and now **answered rather than open**: tables libmspub
reports that never reach the package. It is 13 across the three
newsletters, not the four in `1336` this item counted — 31 reported
against 18 written, 4 in `1336`, 6 in `1337` and 3 in `1338`.

They are dropped for being empty, by `_on_endTableObject`'s own rule: a
grid whose every cell is empty and which has no fill and no stroke
contributes nothing, exactly as an empty text frame does. Every one of the
13 measures zero characters, no fill and no stroke, so the rule is doing
what it says. The 3-column full-page grids with a 9pt gutter are the bulk
of them, and dropping a layout grid that draws nothing is right — a
converted page does not need the scaffolding the original was built on.

What was wrong was the silence, and that is what changed. The count now
reaches the report as one line per document rather than one per table,
the `finish()` aggregation §2 asks for:

```
1336 kerkbode.pub   4 empty table(s) dropped: no text, no fill, no stroke
1337 kerkbode.pub   6 empty table(s) dropped: no text, no fill, no stroke
1338 kerkbode.pub   3 empty table(s) dropped: no text, no fill, no stroke
```

---

## 12. Tab stops — **read, and mostly not there to read**

The record is found and carried. It was never missing from the file and
barely even hidden: libmspub *parses* tab stops, into
`ParagraphStyle::m_tabStopsInEmu`, and `MSPUBCollector` never reads that
member again — the only member of the struct it drops — so they are gone
before librevenge sees anything. `pubfile` reads them out of the Quill
stream (`Quill/QuillSub/CONTENTS`), whose layout is now in that module's
docstring: FDPP is a table of paragraphs in text order, each naming a
style, and block `0x32` inside a style holds an array of stops, each a
signed EMU position and optionally an alignment byte.

Matching is by paragraph text, because nothing in the event stream
identifies the paragraph libmspub is reporting. One text stated two ways
applies to neither — the rule two tables drawing one grid already get.

**The item's premise turned out to be wrong, and this is the finding
worth keeping.** It assumed 554 tabs were waiting on stops the file
holds. It does not hold them:

```
stops stated anywhere in the corpus     335   (313 left, 11 centre, 11 right)
paragraphs containing a tab             203
       ... of which state a stop          3
```

The stops that exist are almost all somewhere else — 313 of them in two
paragraph *styles* of the kerkbode files, and the 22 alignment-bearing
ones are the centre-and-right pair of a header or footer, on paragraphs
holding a page-number field and no tab at all. The tabs that actually
move text were lined up on Publisher's document-wide default grid.

**That interval has since been found**, in the Quill stream's `SGP `
chunk: a bare U32 length and then at most one block, id `0x00` and type
`0x22`, holding the interval in EMU. The 3 stated stops are still
carried exactly, the hanging indent still implies its own stop, and the
other 200 are now given an explicit ruler at the document's own spacing
instead of being counted and left on the reader's grid.

Three things established while looking, so they don't get re-derived:

- **Publisher names the setting.** `Document.DefaultTabStop` in its own
  VBA — per publication, in points, range 1 to 1584 — so the value has
  to be in the file. Its default is 0.5in, which is InDesign's default
  too, so a document that never touched the dialog needs nothing.
- **Document chunk block `0x15` is not the default tab interval.** It
  reads 359410 EMU — 566 twips, 1 cm truncated, a tempting fit — but it
  reads the same 359410 in every corpus file that carries it, including
  the three `kerkbode` issues whose `SGP ` chunk states 8.0787pt, and it
  is absent from the two files that carry no tab. A field that never
  varies cannot be a per-document setting. (An earlier note here ruled it
  out for the wrong reason, saying the US-Letter `Blank Note Card` reads
  359410 as well; that file does not carry the block at all.)
- **Style inheritance is deliberately not implemented.** A paragraph can
  name a default style (block `0x19`) and take that style's stops, the
  way every other paragraph property resolves in
  `MSPUBCollector::getParaStyleProps`. **No tabbed paragraph in the
  corpus names a style that states any**, so the path would be dormant
  code written against no sample. `research/tab_stops.py` prints the
  three counts above per file, including which styles states stops and
  which paragraphs name them, so a file that does exercise it announces
  itself.

What is left is confirmation. The `SGP ` block behaves exactly as the
setting would — absent from both corpus files carrying no tab, present in
all seven that carry one, and reading three different values where block
`0x15` reads one — but nothing has read it back in Publisher, and a
document-wide length could be a hyphenation zone as easily as a tab
interval. `actions.md` §10 is now one line of VBA on the files we already
have. Until it is answered, every document given a ruler says so in the
report. `research/default_tab.py` prints what each file states.

---

## 13. The ramp libmspub reports only the middle of — **done**

Found by comparing a converted page against Publisher's own PDF of it:
the cream banner behind every section heading came out flat grey.

Publisher states a gradient as two colours — the shape's fill and
fill-back — with waypoints between them. `MSPUBParser::getShapeFill`
reads all three and then, **whenever a shade list exists at all, builds
the ramp from that list alone**, dropping both end colours. A two-colour
gradient with a single waypoint therefore reaches librevenge as one stop,
which is nothing to ramp between, and the converter flattens it:

```
                        gradients   flattened
1336 kerkbode.pub              24          10
1337 kerkbode.pub              25          11
1338 kerkbode.pub              25          11
```

What the file says for one of them — the banner — is `#ffffff →
(#e1e1e1 at 52%) → #663300`, white to brown through grey. What arrived
was `#e1e1e1`, and nothing else.

Landed as `pubfile._read_gradients`, with `_resolve_color` following
`ColorReference::getFinalColor`: a reference is a BGR triple unless its
top byte is `0x08`, which indexes the document palette (chunk type
`0x5C`), or `0x10`, which is an intensity change of the colour underneath.

**A first pass at this got three things wrong, all found by checking the
reconstruction against libmspub instead of against the eye.** Worth
keeping, because each one looks right until it is measured:

- **The ramp runs the other way.** Publisher's *fillFocus* says which end
  it starts from, and at 100 — which is every shape in the corpus that
  states a waypoint list — it starts at the fill-back colour, with the
  waypoints reversed and each at its distance from the other end. That is
  `addColorReverse` in libmspub, and the first pass ignored focus, so
  every restored ramp ran backwards.
- **The angle was dropped.** A flattened fill never became a `Gradient`,
  so its angle went with the ramp and the replacement got zero. Six of
  the 32 state 180. Reading it back needs all three of libmspub's own
  transformations, the last of which explains a note in `model.py`: the
  file's `-45` becomes `225` by its quirk table and then `-225` by the
  negation ODF's clockwise angles need, which is the `-225` the corpus
  was already known to carry.
- **Only the flattened ramps were replaced.** libmspub drops the end
  colours from *every* ramp with a waypoint list, so the 32 that survived
  were missing them too — the heading bars run navy to white and arrived
  light blue to pale blue. The rule is now: replace wherever the file
  states a ramp, leave a fill with no waypoint list alone.

And one plain bug: `_resolve_color` resolved an intensity change against
a base it resolved in turn, which recursed until the stack ran out on a
colour stated against itself. libmspub reads the base directly, and so
does this now. It would have cost a file its masters, its WordArt and its
tab stops, since `read_structure` answers any difficulty with None.

**What makes the reading safe** is that libmspub reports 32 of these
ramps in full: on every one, the waypoints reconstructed here are
identical to its own stop for stop, and so are the angles. That is the
regression test — not a fixed expectation, but the two readings agreeing
on the shapes where both can be had.

Matching is by centre, the same handle WordArt uses; where a banner and
its backing panel share one to within half a point, the nearer size
decides, and two equally near is an ambiguity rather than a guess. The
size cannot be *required* to match — the anchor measures the shape with
its outline while libmspub reports the path inside it, 16pt apart on one
shape in the corpus — so it only ever breaks a tie.

---

## 14. Page chunk order is not libmspub's page order — **done**

Found while placing the WordArt of §10, and it is a bug in something else.
`pubfile.read_structure` builds `FileStructure.pages` by walking the chunk
references in order, and `convert._attribute_masters` reads that list
**index by index against `document.pages`** — page *i* of the file taken to
be page *i* of the event stream. The two are not in the same order.

The evidence is the mapping §10 measures, which needs no assumption about
order at all: match every Escher anchor against the items libmspub actually
drew, and each page chunk identifies its own page. Across the corpus **519
shapes matched and every page chunk resolved to exactly one page, with no
chunk split across two.** For the three newsletters the result is a
permutation rather than the identity:

```
1336 kerkbode.pub   chunk 266 -> page 2      chunk 335 -> page 0
                    chunk 327 -> page 1      chunk 6150 -> page 3
```

Every single-page file in the corpus comes out as the identity, and so does
`Cantico_dei_Cantici.pub`, which is why nothing ever looked wrong. The
multi-page files are all mis-ordered: 25 of 26 settled chunks in `1336`, 29
of 31 in `1337`, 28 of 28 in `1338`, and 4 of 5 in
`MISSAL MARIANA E PEDRO.pub`.

**What it costs.** Master attribution assigns each page the master its
*wrongly indexed* page chunk applies. Where a document has one master that
changes nothing — MISSAL is mis-ordered on 4 pages and gets the right master
on all of them — but the newsletters alternate two, 263 and 294, and there
the master really is wrong: **14 of 26 pages in `1336`, 14 of 31 in `1337`,
16 of 28 in `1338`.** Each master holds a single shape, so the damage is one
lifted item per page rather than a whole layout, and `_attribute_masters`'s
own consistency check cannot catch it: with one shape per master, "pages
sharing a master got the same shapes" is true either way.

**Landed.** `_attribute_masters` now asks `_page_by_chunk` which chunk a
page is and takes the master from that chunk, through the new
`FileStructure.master_of_chunk`; `master_for(page_index)`, which was the
bug, is gone. The ordering worry came to nothing: the mapping is built at
the top of the master pass, before anything moves.

**Pages the mapping cannot settle turned out to be the whole of the
work,** and "fall back to no attribution" would have been a regression.
MISSAL cannot settle 10 of its 15 pages — every page holds one full-page
frame at the same spot, so no shape tells them apart — and dropping their
attribution would have left 10 footers reading `#` instead of their page
number. So a page no chunk identifies now falls back to whatever the
chunks still *going spare* agree on, which is a fact about it rather than
a guess, in two degrees:

- **Which master, where they name the same one.** One spare chunk and one
  spare page is the strongest case: it settles `1336`'s remaining page.
- **How many shapes it holds, where they only agree on that.** Enough to
  know how much of the page came from a master, and so to resolve the page
  number — but not enough to lift, because two shapes alike enough to
  share a signature can still belong to two different masters. Those items
  stay flattened and are counted in a warning. This is MISSAL's case: both
  its masters hold one shape, so all 15 pages keep their number and
  nothing is attributed to the wrong master.

`_pages_by_chunk` was split out of `_page_by_chunk` to hold the candidate
sets the fallback needs, and it *intersects* each shape's matched pages
rather than counting unanimous single matches — a shape libmspub drew
nowhere constrains nothing and is passed over. Strictly stronger: it
settles a chunk two of whose shapes each match several pages but only one
page in common.

**What it changed in the output: nothing, on this corpus.** Worth stating
plainly, because it is not what §14 predicted. Every master in every
corpus file holds exactly one shape, and in the newsletters that shape is
the page-number footer — which stays on its page by design and is never
lifted. **No file in the corpus lifts a single item onto a master**, so
the wrong master identity had nothing to spend itself on: 14/14/16 pages
now take a different master than before and every conversion is
byte-for-byte what it was. This was a latent fix, and the only reason to
have made it is the one §14 gave — that the next thing to trust the index
would have inherited the bug.

---

## 15. Where master content lands, which was never stated — **done**

§14 fixed *which* master a page applies. This is the other half: what the
package says about where that master's content goes once a page applies it.
It said nothing.

**`MasterPageTransform` was missing entirely.** It is the matrix that
carries a master page's items onto a page applying it, and an
InDesign-written package states it on *every* `Page`, in `Spreads/` and
`MasterSpreads/` alike — checked against a real InDesign 17.0 export, where
it reads `1 0 0 1 0 0` throughout. We wrote no such attribute anywhere, so
a reader had nothing to resolve master placement against and whatever it
defaults to was the answer.

**And the identity would not have been the true matrix anyway.** The master
page was written centred on the spread origin, `1 0 0 1 -w/2 -h/2`. That is
right for a single-page document, where every page is centred too. Laid out
facing it is wrong: a recto sits at `0` and a verso at `-w`, so the master
sat a **half page** from the pages taking their content from it — 210.5pt
on A5, and in opposite directions on the two sides of a spread. In the
reference export the two pages of a facing master spread are at
`-566.93` and `0`, byte-identical to the verso and recto offsets of the
pages applying them, which is *why* the identity is correct there.

**Landed.** `idml.IdmlWriter._master_sides` reads the offsets of the pages
that actually apply a master and lays the master spread out on those:
centred where the document is single-page, one page on the side of the
spine its pages are on where it is facing, and — where pages of both sides
apply it — a page on each, which is InDesign's own two-page master spread.
The content is written once per side, since a running head on a facing
master really is two frames. `MasterPageTransform` is now written as the
identity on every `Page`, and the identity is true because of the above.

Two smaller things went with it:

- **A 27th master silently ate the first.** The name came from
  `names[len(masters) % 26]`, and the name is the master's identity out to
  the `Self` id and the part filename, so masters 1 and 27 claimed one part
  — 27 masters in, 26 parts out, and the pages naming it got the other's
  content. `convert._master_name` now counts A…Z, AA, AB.
- **Two warning passes could not see a master.** `_check_unrenderable_paths`
  and `_check_overset_text` walked `document.pages` only, so a path that
  draws nothing or a frame collapsed to nothing went unreported the moment
  the master pass moved it — and on a master it is wrong on every page
  applying it. Both walk `document.all_items()` now, which is what the
  rest of the checks already did.

**What it changes on the corpus: nothing, and that is expected.** Every
master in every sample holds exactly one shape, a page-number footer, and
those deliberately stay on their page — so no file here emits a
`MasterSpread` at all (0 in all 32 packages in `converted/`). Reconverting
`1338 kerkbode.pub` gives a package byte-identical to the old one apart
from the added `MasterPageTransform`. The path is still exercised only by
`research/probe_masterspread.py`, which now builds the facing case too and
says what to read off it in Affinity.

---

## Not worth doing

Recorded so they don't get re-investigated:

- **The other 16 unread properties in the event stream.** Swept and
  checked against the corpus, and each one either restates a default or
  has no IDML representation: `svg:fill-rule` is always `nonzero`, which
  is IDML's own behaviour; `style:repeat` is always `stretch` and
  `draw:fill-image-ref-point` always `top-left`, which is how a bitmap
  fill is already placed; `libmspub:shade` is always `normal`;
  `style:text-underline-style/-width/-mode` are always
  `solid`/`auto`/`continuous`, so the underline we do carry loses nothing.
  Document metadata (`dc:creator`, `dc:date`, `meta:creation-date`,
  `meta:initial-creator`) would need an XMP packet to carry an author name
  the layout never shows.

- **`ActualPpi` of an unwrapped photograph reads 72.** The DIB declares no
  pels-per-metre, so the documented fallback applies. The old value of 300
  came from our own rasterisation density, not from the source, and
  `EffectivePpi` — the figure that decides print quality — is correct and
  no longer resampled. `_encode_png` already writes `pHYs` when the DIB
  does declare a resolution.
- **An external WMF converter.** `libwmf` is in Homebrew and Inkscape and
  LibreOffice are casks, but the Python half is deliberately stdlib-only
  and the shipped artefact is a Windows executable. None of them would be
  present where the tool actually runs.
