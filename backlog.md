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

Still not carried, because libmspub reports none of it: per-cell fill,
rule weights and colours, and cell insets. A ruled or shaded table arrives
with Affinity's default styling.

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

## Not worth doing

Recorded so they don't get re-investigated:

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
