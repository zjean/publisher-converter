# Backlog

Work identified and deliberately deferred, all of it doable **on this
machine** — no Publisher, no Windows, no new dependency. Things that need
a human with Publisher live in `actions.md` instead.

Ordered by value. Each item carries the evidence already established, so
none of it has to be re-derived.

---

## 1. Translate WMF drawing records into native IDML paths

### Why this matters

This is the largest remaining loss in the converter. It is also the one
place where the honest reporting added in `6c8d0e5` makes the size of the
gap visible:

```
1336 kerkbode.pub    WMF artwork dropped (4 drawing record(s), 64 copies)
1337 kerkbode.pub    WMF artwork dropped (4 drawing record(s), 64 copies)
1338 kerkbode.pub    WMF artwork dropped (4 drawing record(s), 64 copies)
Lisa Hoogendijk.pub  17 distinct pieces of artwork, across 216 frames
```

`emf2svg-conv` reads EMF only, ImageMagick delegates WMF to a LibreOffice
nobody has installed, and the documented deployment is `pub2idml.exe` on
Windows where none of those exist anyway. So no external tool will ever
fix this in the real workflow.

### Why vectors, not a raster

The output is opened in Affinity's layout mode, where the goal is an
editable layout. Emitting paths is also the *cheaper* option: the writer
already turns `model.Polygon` and `model.Path` into IDML `PathGeometry`,
whereas rasterising would mean writing a scanline rasteriser from
scratch. Photographs are a different case and are already handled — see
`metafile.embedded_bitmap`.

### What is already known

Established by decoding the real artwork; don't re-derive it.

- The newsletter logo is 4056 bytes, byte-identical across all 64 frames,
  a **standard (non-placeable) WMF**: `mtType=1`, `mtHeaderSize=9` words,
  version `0x0300`, 37 records.
- Its drawing content, decoded:
  ```
  BRUSH #ffffcc -> POLYGON 130 pts  x[-1141..755] y[-947..917]
  BRUSH #000000 -> POLYGON 260 pts  x[-621..270]  y[-623..656]
  plus 2 POLYPOLYGON records with solid black/white brushes
  ```
- **The record vocabulary is closed.** Every WMF record in every WMF in
  the corpus, counted — 22 types, nothing else:

  | Record | Uses | Role |
  |---|---|---|
  | `0x0324` META_POLYGON | 1237 | **draws** |
  | `0x0325` META_POLYLINE | 807 | **draws** |
  | `0x041B` META_RECTANGLE | 766 | **draws** |
  | `0x0418` META_ELLIPSE | 480 | **draws** |
  | `0x0538` META_POLYPOLYGON | 384 | **draws** |
  | `0x0213` META_LINETO | 154 | **draws** |
  | `0x012D` META_SELECTOBJECT | 7200 | object table |
  | `0x02FA` META_CREATEPENINDIRECT | 1923 | object table |
  | `0x01F0` META_DELETEOBJECT | 1844 | object table |
  | `0x02FC` META_CREATEBRUSHINDIRECT | 1467 | object table |
  | `0x0106` META_SETPOLYFILLMODE | 2108 | state |
  | `0x001E` META_SAVEDC | 1056 | state |
  | `0x0127` META_RESTOREDC | 1056 | state |
  | `0x020C` META_SETWINDOWEXT | 473 | mapping |
  | `0x020B` META_SETWINDOWORG | 323 | mapping |
  | `0x0104` META_SETROP2 | 240 | state |
  | `0x0102` META_SETBKMODE | 308 | state |
  | `0x0201` META_SETBKCOLOR | 308 | state |
  | `0x0209` META_SETTEXTCOLOR | 308 | state |
  | `0x0214` META_MOVETO | 154 | state |
  | `0x0103` META_SETMAPMODE | 47 | state |
  | `0x0000` META_EOF | 408 | terminator |

  So the whole job is **six drawing primitives**, and each already has a
  model class waiting for it:

  ```
  META_POLYGON      -> model.Polygon
  META_POLYLINE     -> model.Polygon(closed=False)
  META_POLYPOLYGON  -> model.Path        (one subpath per polygon)
  META_RECTANGLE    -> model.Rectangle
  META_ELLIPSE      -> model.Ellipse
  META_LINETO       -> model.Path        (with MOVETO as current position)
  ```

  Note what is *absent*: no text records at all (no `META_TEXTOUT` or
  `META_EXTTEXTOUT`, no `META_CREATEFONTINDIRECT`), so no font handling is
  needed; no arcs, chords or pies; no regions or clipping; and no bitmap
  blits, which is what leaves item 4 below untestable.
- **The coordinate mapping data is in the first two records.**
  `SetWindowOrg` and `SetWindowExt` lead every one of these files, which
  is why the polygon coordinates come out negative. Scale that logical
  window onto the frame's placed rect.
- `metafile._WMF_NON_DRAWING_RECORDS` already classifies state vs drawing
  records, and `_inspect_wmf` already walks the record list correctly for
  both the placeable and standard spellings. The walker is done; only the
  interpretation is missing.

### Approach

Translate into the existing model rather than inventing anything, using
the mapping table above. Fills and strokes come from the object table:
`CreatePenIndirect` and `CreateBrushIndirect` append to it,
`SelectObject` picks by index, `DeleteObject` clears a slot — so a small
list plus a "currently selected pen/brush" pair is the whole of it.
`SaveDC`/`RestoreDC` push and pop that state. A `Group` holds the result
so the artwork stays one object in Affinity.

Keep a counter of drawing records that were *not* translated, so the
warning can say "converted 4 of 6 drawing records" rather than implying
completeness. No silent caps.

### Risks worth testing explicitly

- Mapping modes: `META_SETMAPMODE` appears 47 times, so the mapping is not
  always the default. `MM_ANISOTROPIC` and `MM_TEXT` need different
  window/viewport arithmetic.
- Fill modes: `ALTERNATE` against `WINDING` for self-intersecting polygons.
  `META_SETPOLYFILLMODE` is the single most common state record here
  (2108 uses), so it is clearly being changed deliberately.
- `META_POLYPOLYGON` holds *all* the polygon point-counts before *any*
  coordinates. Reading them interleaved is the obvious way to get this
  silently wrong, and it will still produce plausible-looking output.
- `META_SETROP2` (240 uses) selects a raster operation. Anything other
  than `R2_COPYPEN` has no IDML equivalent and should be counted as
  unsupported rather than quietly drawn as opaque.

TDD it against the real 4056-byte blob plus synthetic records. The same
machinery later covers EMF vector records, which are a cleaner format.

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
