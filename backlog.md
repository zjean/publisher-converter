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

## 5. Story-threading residuals

Threading landed in `a052d30`. Two known gaps, both documented in the
README:

- **A chain whose text fits its first frame is not detected.** A group is
  only threaded when the story oversets even the roomiest frame in it,
  because identical text alone also describes a repeated label — a
  page-number field repeats a single `#` across 27 frames, and blanking
  those would erase the numbers. `1336 kerkbode.pub` still carries one
  undetected pair: two frames with the same 1905 characters against a
  capacity of roughly 2325. Loosening the guard risks the page numbers,
  so any change here needs the `#` case as a test.
- **Link order is page order.** An article that flowed against the page
  sequence arrives threaded in the wrong order and needs re-linking by
  hand. Publisher's own chain order is not in the event stream; it would
  have to come out of the `.pub` structure directly, the way page numbers
  and masters already do (`pubfile.read_structure`).

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

## 8. Language, and one scaled span

Every span libmspub reports carries a language, and the converter reads
neither `fo:language` nor `fo:country`:

```
1336: nl-NL x1785, en-US x11, fr-FR x4, sv-SE x3, da-DK x2, de-DE x1
1337: nl-NL x1302, en-US x19, fr-FR x3
1338: nl-NL x2371, fr-FR x3
```

This is not visual styling, but it decides hyphenation, and Dutch text
hyphenated as English reflows — in a two-column newsletter that moves
every line after the first bad break. IDML carries it as `AppliedLanguage`
on a `CharacterStyleRange`, against `Language` entries in the resources,
so it needs a language resource per distinct locale plus the attribute.
Affinity has per-text language, so this is likely to land; probe it the
same way.

One span in `1336` also reports `fo:text-scale`. Read it carefully if it
is ever carried: libmspub does `textScale = data / 10` and then hands
librevenge the result as a percentage, so a 90% scale arrives as the
string `9000.0000%` and `units.percent` would return 90 — a 9000%
scaling. The correct reading is a further division by 100, which is a
guess against a single span, which is why it is here and not in the code.

---

## 9. Whose rules is a table drawing?

A converted table names `TableStyle/$ID/[Basic Table]` and the package
never defines it, so the reader supplies its own. Affinity's draws a line
around every cell — visible in `research/probe_cell_insets.py`, where no
stroke was asked for anywhere.

That is wrong in at least one direction and possibly both: the corpus's
tables are layout grids, and a line around every cell of one would show a
grid across an article. But "no ruling recorded" cannot yet be read as "no
lines", because Publisher's own default may supply them, which is exactly
what `actions.md` §9's plain control file settles — look at whether it
prints lines.

Two things to do here, in this order: extend the inset probe with a row
whose four edge strokes are explicitly zero, which says whether an
override beats Affinity's default at all; and, once §9's sample says what
a plain Publisher table looks like, either write those zeros for every
table matched in the .pub or write the real weights and colours.

---

## 10. The WordArt libmspub reports nothing for

WordArt headlines now convert (`actions.md` §8): the words come out of the
Escher stream and replace the guide path libmspub reports for the same
shape. **One shape in `Cantico_dei_Cantici.pub` has no such path** —
libmspub reports nothing at all where the file puts it — so it is named in
the report rather than placed:

```
'I venerdì 2006 di Avvento'      64.4 x  63.5pt, Comic Sans MS, no size stated
```

Everything needed to place it is in the .pub: the anchor gives the band
and the rotation, the properties give the text, font and size. What is
missing is any confirmation from the event stream, and that is the whole
reason to hesitate — it would be the only content the converter puts on a
page without libmspub agreeing that something is there, and the corpus has
one case where the guides were welded into a shape that did not exist
(197368d).

Worth doing, carefully: place it, but only where nothing libmspub *did*
report already overlaps the band, so a WordArt whose text also arrived as
an ordinary frame cannot be written twice. The overlap test is the part to
get right; `_recover_wordart` already has the geometry to do it.

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

**And the reason is missing fonts, not anything the converter does.**
Measured on `1336`'s first table, against the leading and insets the
converter itself writes, with every paragraph on one line:

```
 row  stated   needs   slack
  0    36.04   36.14   -0.10
  1    30.47   30.38   +0.09
  2    37.56   38.06   -0.50
  3    61.65   59.18   +2.47
  4    32.32   30.38   +1.94
  5    32.32   30.38   +1.94
  6    32.32   15.02  +17.30   (empty)
```

Publisher's row heights *are* the height of the text it laid out — two
rows are already fractionally over before anything wraps. One extra
wrapped line costs 7.68pt at 8pt type, and no row has that to spare. So
the geometry has no tolerance at all: a single line breaking differently
grows the table.

Lines do break differently because **7 of the 12 font families these
documents use are not installed**: Calibri, Monotype Corsiva, Pristina,
Aptos, Blackadder ITC, MV Boli, Segoe Script. Calibri is the body font of
these tables. A substitute with even 1–2% wider glyphs turns a line that
just fitted into two.

Nothing to fix in the converter: it writes the file's own numbers, and
they are right. The fix is on the machine opening the package — install
Calibri, or **Carlito**, which is metric-compatible with it and free, so
the wrapping matches exactly. Worth confirming that way before spending
anything on `AutoGrow`; the probe's four treatments are there if it turns
out to be needed for files whose fonts genuinely cannot be had.

Separately, and found while measuring the above: **a cell's bottom inset
is zero in all 974 cells of the corpus**, which no Publisher default
produces. The four inset fields are `0x0A`–`0x0D`, and they are present
354, 351, 336 and **65** times respectively — the count falls away
towards the last, the signature of trailing fields left out when they
match a default. `_table_cells` reads a missing field as zero, so the
bottom inset is being invented rather than read. Where `0x0D` *is*
present it is always 2.88pt, Publisher's 0.04in default, which says the
default those fields are measured against is something else again. Worth
settling; it makes rows want slightly more height, not less, so it is not
the cause of the overflow above.

Also unresolved and probably showing at the same time: **three full-page
layout tables in `1336` never reach the package at all.** libmspub reports
11 tables and 7 are written; the four missing are the 3-column full-page
grids with a 9pt gutter, and one 1-column strip. They are gone before
`_recover_wordart` runs — the parser drops them — and nothing says so in
the report. Worth finding out whether they are dropped for being empty,
which for a layout grid may be right, but it should be a decision rather
than a silence.

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
move text were lined up on Publisher's document-wide default grid, and
**that interval is not recorded anywhere in the file yet found**.

Which leaves the honest position: the 3 are carried exactly, the hanging
indent still implies its own stop, and the remaining 200 are counted in
the report so nobody assumes they landed right.

Two things established while looking, so they don't get re-derived:

- **Document chunk block `0x15` is not the default tab interval.** It
  reads 359410 EMU in *every* file in the corpus — 566 twips, which is
  1 cm truncated, and a tempting fit. But it is the same 359410 in the
  US-Letter `Blank Note Card`, where a metric default has no business
  being, so it is a constant of the format rather than a locale-derived
  interval. Materialising a grid from it would have written a 1 cm ruler
  into every tabbed paragraph on a guess.
- **Style inheritance is deliberately not implemented.** A paragraph can
  name a default style (block `0x19`) and take that style's stops, the
  way every other paragraph property resolves in
  `MSPUBCollector::getParaStyleProps`. **No tabbed paragraph in the
  corpus names a style that states any**, so the path would be dormant
  code written against no sample. `research/tab_stops.py` prints the
  three counts above per file, including which styles states stops and
  which paragraphs name them, so a file that does exercise it announces
  itself.

What would finish this is the default interval, and it needs Publisher:
`actions.md` §10 is the controlled pair that would find it.

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
