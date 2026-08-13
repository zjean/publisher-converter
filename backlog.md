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

Surfaced by a conversion run of `1336 kerkbode.pub` after the metafile
work landed. Everything else now collapses, which leaves the table
warning as the only thing still crying wolf — 11 identical lines out of
14 total:

```
! table flattened to paragraphs: cell structure not preserved     x11
! 4 repeated item(s) moved onto 4 master page(s) ...
! WMF artwork dropped (4 drawing record(s), 64 copies): ...
! 12 linked text frame(s) threaded into 2 story/stories: ...
```

This is exactly the pattern already fixed for metafiles in `6c8d0e5`,
where 419 warnings across the corpus became 20. The table warning is
emitted once per table in `model._on_startTableObject`, with no
aggregation.

### Approach

Two options, and the second is probably right:

1. Aggregate at the source, as the metafile path does — count tables and
   emit one warning when the document finishes.
2. A general collapse step: fold identical warnings into one line with a
   count, applied to every warning just before the report is written.

Option 2 also covers anything added later, and is a few lines. But note
that a purpose-written message reads better than a mechanical suffix —
"11 tables flattened to paragraphs" beats "table flattened … (x11)" — so
the general collapse may be worth pairing with a count placeholder the
producer can fill in.

Check `cli.py`'s report writer too: the CSV joins warnings with `; `, so
whatever is done should shorten both the terminal output and the CSV.

---

## 3. Real IDML tables instead of flattening

`1336 kerkbode.pub` alone contains 11 tables. Cells are currently flowed
into a single frame as paragraphs: the copy survives, the grid does not
(`model._on_startTableObject`, and the comment there explains the original
trade-off).

IDML has a full table model (`Table`, `Row`, `Column`, `Cell` inside the
story), so this is faithful in principle but a genuinely large feature —
worth scoping before starting. Item 2 above reduces the *noise* from this
limitation; it does not fix the limitation.

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
