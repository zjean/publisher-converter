# Measuring the font a headline is set in

## The problem

WordArt recovery works: all 48 shapes in the corpus convert, the words come
out of the file, and the report says so at length. What the report also says
is that three of its numbers are guesses:

- `convert._EM_PER_ADVANCE = 0.5` — the em-per-advance used to turn WordArt's
  spacing multiple into IDML tracking.
- `pubfile._BAND_INK_PER_EM = 0.70` — how much of an em a headline line inks,
  used to work a point size back from a band.
- `pubfile._BAND_EM_PER_GLYPH = 0.55` — how wide an average headline glyph is,
  used so a band-sized headline does not overflow and wrap.

Each stands in for a measurement of the actual font, averaged from two
rendered headlines. Each is named in the conversion report, which is why the
report is a paragraph long for a conversion that mostly went right.

Both halves are addressed here, in this order: measure the fonts, then write
the report against what is left over.

## Scope

Full stretch, for every WordArt shape that states the stretch flag — stated
size or not. README currently defers this ("a stated size is a floor rather
than the truth ... waits for its own verification pass"); this is that pass.
It applies to all 15 headlines in `1336 kerkbode.pub`, not only the 2 the file
leaves unsized.

The unlock is that IDML can state the one thing README says it cannot:
`model.Span.horizontal_scale` already exists and `idml.py:1619` already writes
`HorizontalScale`. Size from the band's height and condensation from its width
is exactly what "stretches the glyphs to the shape" means, so no part of
WordArt's fitting has to be approximated away any more.

## 1. What gets measured

A new module, `pubidml/fontmetrics.py`, owns one job: given a family name, a
bold and italic flag, and a string, return that string's metrics in ems. It
knows nothing about .pub files. Everything below is stated per em, so a point
size is a multiplication.

- **ink per em** — the real vertical extent of the glyphs in the string, from
  the `glyf` bounding boxes of exactly those characters: `max yMax - min yMin`
  over the glyphs the string uses, divided by `unitsPerEm`. This is the
  quantity README measured by hand as "inks 39.7 pt". Replaces
  `_BAND_INK_PER_EM`, and fixes the descender error README notes, because a
  headline with descenders now measures as one.
- **natural width per em** — the sum of the real `hmtx` advances for the
  string, with WordArt's spacing multiple applied. Replaces
  `_BAND_EM_PER_GLYPH x glyph count`.
- **mean advance per em** — the real average advance over the string.
  Replaces `_EM_PER_ADVANCE`, so tracking becomes
  `(spacing - 1) x mean_advance x 1000` against a measured mean.

Tables read: `head` (unitsPerEm, indexToLocFormat), `name` (family and
subfamily), `cmap` (character to glyph id), `hhea` and `hmtx` (advances),
`loca` and `glyf` (bounding boxes). Composite glyphs still carry their bbox in
the glyph header, so they need no recursion.

OpenType/CFF fonts have no `glyf`. Those fall back to `OS/2` cap height and
ascender — a per-font measurement rather than a global one — and the report
records that they were measured that way.

## 2. The sizing rule

One size and one horizontal scale per shape, not per line: WordArt stretches
the whole block to the shape, and per-line sizes would step visibly.

**Size** comes from the height alone:

    size = (band_height / line_count) / ink_per_em

using the tallest line's ink per em, so no line overflows. Today's
`min(height rule, width rule)` goes away — the width no longer binds the size,
which is what forced the current "slightly small beats overset" compromise.

**Horizontal scale** takes the width instead: the longest line's natural width
at that size, against the band width, written as `HorizontalScale`. Clamped to
25–400% so a wildly substituted font cannot produce an unreadable smear; a
clamp that fires is reported.

**Authorised by the stretch flag.** The override applies only where the shape
states the flag — the file saying its glyphs are fitted to the shape. A shape
without it keeps its stated size and takes no scale. Every WordArt shape in
the corpus states it, so in practice this is all of them, but the flag stays
the thing that permits the override rather than an assumption about it.

**The stated size changes job** from ignored to sanity bound. Computing 38
where the file states 20 matches the *Meditatie* evidence and is fine;
computing 200 means the metrics are wrong, and the report names that headline.

**Where sizing lives moves.** `pubfile._band_size` currently decides a point
size while parsing, so `WordArt.size` already carries a guess by the time
`convert` sees it. Under full stretch, sizing needs the font, the band and the
line count together, which makes it a conversion decision. `pubfile` goes back
to reporting what the file states — `size=None` when the file states none, with
`fitted` saying so — and `convert` does the fitting. `_band_size`,
`_BAND_INK_PER_EM` and `_BAND_EM_PER_GLYPH` move out of `pubfile` and behind
`fontmetrics`.

## 3. Finding the font, and the three tiers

An index built once per run, lazily on the first WordArt shape: walk the
platform font directories and read only each file's table directory and `name`
table to learn family (nameID 1) and subfamily (nameID 2).

- macOS: `/System/Library/Fonts`, `/System/Library/Fonts/Supplemental`,
  `/Library/Fonts`, `~/Library/Fonts`
- Windows: `C:\Windows\Fonts`, `%LOCALAPPDATA%\Microsoft\Windows\Fonts`
- Linux: `/usr/share/fonts`, `~/.fonts`, `~/.local/share/fonts`

`.ttc` collections are unpacked through their `ttcf` header, which matters
because macOS ships most of its faces that way. A file that does not parse is
skipped; a directory that does not exist is not an error.

Matching: exact family name, then the bold/italic subfamily. If the file asks
for bold and only the regular face is present, regular is measured and the
existing `bold=True` span attribute does the rest — the metrics come out
slightly narrow, which errs toward a headline that fits.

Three tiers, in order:

1. **Font file found** — exact per-string measurement. Full stretch with
   `HorizontalScale`.
2. **Baked table** — a dict in the source of per-font summary metrics (mean
   advance per em, ink per em, ascender, descender), measured once on a machine
   that has the face, for the fonts the corpus uses. Better than a global
   average and identical on every machine. It takes today's rule with better
   numbers: `min(height, width)` using the font's own ink and mean advance in
   place of 0.70 and 0.55, and no horizontal scale, because only real
   per-string advances earn a scale.
3. **Neither** — today's constants and today's behaviour exactly:
   `min(height, width)`, no horizontal scale. A scale computed from guessed
   widths is a confident-looking wrong answer; unscaled straight text is the
   honest one.

**The trade-off, named.** Tier 1 beats tier 2, so the same .pub converted on a
machine that has Pristina produces different numbers than on one that does
not. That is the right way round — exact should beat approximate — and the
baked table is what keeps the difference small. The report says which tier each
headline used, so the difference is never silent.

Populating the table needs a machine that has the fonts, so `research/font_metrics.py`
measures an installed face and prints a table entry, in the shape of the
existing `research/` probes. That covers Monotype Corsiva on this laptop;
Pristina's entry comes from a Windows box or stays empty.

## 4. The report

Today's line is one sentence carrying five clauses under a `!` marker, where
four of the five say nothing is wrong. The rewrite splits it by whether a
person has to act.

**Nothing enters `warnings` unless a person must look.** A headline measured
exactly, set straight because the file never bent it, with its duplicate paint
dropped, is a converted headline. The default case emits no warning at all, and
the count moves into the per-file detail line beside `15 frames` as
`15 wordart` — visible, quiet, no flag.

What stays a warning, one short line each, only when non-zero:

- **Bent headlines.** The only genuine loss, and today it is buried mid-sentence.
  One bent shape exists in the corpus, so the usual form is:
  `1 WordArt headline(s) bent into a shape IDML cannot state (button curve);
  straight text in the band is all that comes across — redraw '<the words>'`
- **Estimated rather than measured.** Names the font, because installing it is
  the fix: `3 headline(s) sized from averages: 'Pristina' is not installed, so
  their size and letter-spacing are close rather than measured`
- **Sanity failures.** A horizontal scale that hit the clamp, or a computed
  size wildly off the size the file states, naming the headline.
- The existing "placed from the file alone" and "not placed" warnings are
  unchanged; they already name specific headlines to check.

The explanation of *why* WordArt arrives as guide paths belongs in README,
where it already is, not printed on every conversion. This deletes `_sentence`
from the report path and most of `convert.py:1233-1271`.

## 5. Testing

- **`tests/test_fontmetrics.py`** — synthetic sfnt bytes built in the test, the
  way `test_pubfile.py` builds Escher records: a minimal font of known
  `unitsPerEm` with two glyphs of known advance and bbox, a `cmap`, and a
  `.ttc` wrapper around it. Asserts exact ink, width and advance; asserts a
  truncated file raises rather than crashes or hangs.
- **Sizing tests in `test_convert.py`** — driven by a fake metrics source, so
  the rule is tested with no font installed: size from per-line height, tallest
  line binds, scale from the longest line, clamp fires, stretch flag absent
  keeps the stated size, and tier 3 reproduces today's numbers exactly. The
  existing sizing tests become the tier 3 tests.
- **README's two measured figures as regression cases**, with tolerance: a
  dropped initial at 39.7 pt of a 40.1 pt band, and *Meditatie* stated at 20
  and drawn at about 38.
- **The corpus conversion**, opened in Publisher/Affinity by hand. This is the
  only ground truth that exists for the stretch, and it is the real check on
  the whole change.

## Delivery

Two commits, in the order the report depends on:

1. `fontmetrics` plus the sizing rule, with `pubfile` handing sizing to
   `convert`.
2. The report rewritten against what is actually left approximate.

README's WordArt section is updated with the change — in particular the
paragraph that defers the stated-size override, which this replaces.

## Out of scope

- Rendering warped text. IDML cannot state it; bent headlines are still
  reported for redrawing by hand.
- Font substitution or fallback face selection beyond the bold/italic match
  above. A missing font falls back to averages, not to another font's shapes.
- Anything outside WordArt. Body text sizing is untouched.
