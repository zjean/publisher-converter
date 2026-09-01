# pub2idml

Batch-converts Microsoft Publisher `.pub` files into Adobe IDML packages
that Affinity can open as editable layouts — text in real text frames,
placed images, shapes and colours, not a flattened picture of the page.

Built because Affinity cannot read `.pub` and never will, and Microsoft
retires Publisher on **1 October 2026** (13 October for perpetual Office
2021), after which `.pub` files can no longer be opened in Publisher
itself.

## How it works

Publisher's format is closed and undocumented, so the binary parsing is
delegated to **libmspub** (the Document Liberation Project library that
LibreOffice uses). Everything above that is ours:

```
   .pub
     │
     ▼
  bin/pubdump          C++ shim. Implements librevenge's RVNGDrawingInterface
     │                 and serialises every libmspub callback to JSON lines.
     ▼
  JSON event stream
     │
     ▼
  pubidml/model.py     Replays the flat event stream into a document tree:
     │                 pages, frames, shapes, images, styled text runs.
     ▼
  pubidml/idml.py      Emits the IDML package (ZIP of XML parts).
     │
     ▼
   .idml  +  <name>_images/
```

Splitting at the JSON boundary keeps all binary-format handling in the
one library that already solves it, and leaves document reconstruction
in Python where it is easy to test and extend.

## Why IDML and not Affinity's own format

Affinity's native documents — `.afpub`, and the unified `.af` of the
single Affinity app — are closed binary serialisations of Affinity's
internal document model. There is no published specification and no
supported way to author one from outside the app, and the format moves
with each release, so anything reverse-engineered would need proving
again every time Affinity updates.

IDML is Adobe's *interchange* format, and interchange is the whole point
of it: a ZIP of plain XML parts (`designmap.xml`, `Spreads/`, `Stories/`,
`Resources/Styles.xml`), publicly specified, stable across versions, and
imported natively by Affinity Publisher. `pubidml/idml.py` writes it with
`zipfile` and string templates — which is why this tool needs no
third-party Python packages at all.

Two things follow, and both are visible elsewhere in this README. One
manual step survives: open the `.idml` in Affinity and save as its own
format, which is also what turns the `_images` sidecar into embedded
artwork. And everything travels through InDesign's document model, so
whatever Publisher expresses that IDML cannot — or that Affinity's IDML
importer does not honour — is lost in the crossing. That is what
*Known limitations* is a list of. Since Affinity's importer cannot be
unit-tested, the answers come from probe packages built by the scripts
in `research/` and read off by hand in Affinity.

## Install

### macOS

Requires Homebrew and the Xcode command line tools.

```sh
brew install libmspub pkg-config
make
```

That produces `bin/pubdump`. No Python dependencies beyond the standard
library, and **Microsoft Publisher is not required** — the files are
parsed directly.

### Windows

The Python half is standard library only and already portable; the only
Windows-specific work is building `pubdump.exe`.

Note that **vcpkg packages neither libmspub nor librevenge**, so an MSVC
build would mean compiling both from source. MSYS2 packages both — but
`libmspub` is only in the `ucrt64` and `clang64` repos, *not* `mingw64`.
Use a **UCRT64** shell.

```sh
pacman -S make mingw-w64-ucrt-x86_64-gcc \
          mingw-w64-ucrt-x86_64-pkgconf \
          mingw-w64-ucrt-x86_64-libmspub

make           # builds bin/pubdump.exe
make dlls      # copies the MinGW DLLs it links against into bin/
```

`bin/` is then self-contained and `python pub2idml.py …` works. To ship a
single executable to people without a build environment:

```sh
pip install pyinstaller
pyinstaller pub2idml.spec     # -> dist/pub2idml.exe
```

`dist/pub2idml.exe` embeds Python, `pubdump.exe` and the native DLLs.
Nothing needs installing on the target machine — no Python, no MSYS2, no
Publisher. It runs on Windows 10 and 11 x64.

**Don't want to set up a Windows toolchain?** `.github/workflows/build-windows.yml`
does all of the above on a GitHub runner and uploads `pub2idml.exe` as a
build artifact. Push the repo and download the result; you only need
Windows to *run* it, not to build it.

The workflow does not merely build — it proves the result is standalone.
After bundling it copies the executable to a bare directory, **strips
`PATH` down to `C:\Windows\system32`** so neither MSYS2 nor Python is
reachable, converts the sample files there, and then checks that five
`.idml` packages exist, that each is a valid ZIP whose first entry is
`mimetype`, and that a log was written. A missing DLL fails the build
instead of failing on your machine.

Two Windows notes:

- The executable is **unsigned**, so SmartScreen shows "Windows protected
  your PC" on first run. *More info → Run anyway*, or sign it with a code
  signing certificate if it is going to be distributed widely.
- One-file bundles unpack to `%TEMP%` on each launch, costing roughly two
  seconds of startup. Irrelevant for a batch of hundreds; noticeable if
  you invoke it per file in a loop.

### Getting pub2idml.exe

Every green build of `main` publishes a **rolling prerelease** tagged
`latest`, so the newest executable always sits at a stable URL:

> **Releases → Latest build (main) → `pub2idml.exe`**

Or from the command line:

```sh
gh release download latest --repo zjean/publisher-converter --pattern 'pub2idml.exe'
```

Each release also carries `pub2idml.exe.sha256`; check the download with:

```powershell
Get-FileHash pub2idml.exe -Algorithm SHA256
```

That confirms the file arrived intact — it is **not** proof of
authenticity. The checksum is generated by the same job that builds the
executable and published to the same release page, so anyone able to
alter one could alter the other. The binary is unsigned; trust it exactly
as far as you trust this repository and its CI.

For a pinned version rather than a moving target, push a tag and that
build gets its own permanent release:

```sh
git tag v1.0.0 && git push origin v1.0.0
```

Build artifacts are also attached to each Actions run, but they expire
after 90 days and need a GitHub login — the release assets do not.

### Running pub2idml.exe on Windows

Put `pub2idml.exe` anywhere — there is nothing to install. Open **PowerShell** or **Command Prompt** in
the folder containing it. It is a command line tool: double-clicking it
just prints the usage and closes.

```powershell
# convert one file
.\pub2idml.exe "C:\Archive\March 2019.pub" -o C:\Converted

# convert a whole collection, recursively, mirroring the folder structure
.\pub2idml.exe C:\Archive -o C:\Converted

# eight at a time, redoing files already converted
.\pub2idml.exe C:\Archive -o C:\Converted --force -j 8

# maximum diagnostics, log next to the output
.\pub2idml.exe C:\Archive -o C:\Converted -v --log-file C:\Converted\run.log
```

Quote any path containing spaces. Paths may be absolute or relative, and
UNC network paths (`\\server\share\...`) work.

#### Options

The same flags apply on macOS; only the default paths differ.

| Option | Meaning |
|---|---|
| `source` | a `.pub` file, or a folder to search (required) |
| `-o`, `--output PATH` | output folder; created if absent. Default `converted` beside the working directory |
| `-j`, `--jobs N` | parallel conversions. Default: one per CPU core |
| `--no-recursive` | only the given folder, do not descend into subfolders |
| `--force` | reconvert files whose `.idml` already exists. Without it, those are skipped, so an interrupted run resumes cheaply |
| `--report PATH` | where to write the CSV. Default `conversion-report.csv` inside the output folder |
| `--codepage MODE` | `auto` (default) detects and repairs non-Latin text, `none` disables repair, or force a codec such as `cp1251`, `cp932` |
| `--no-image-wrap` | keep the source's exact stacking instead of flowing text around images and around the headlines the file says it flowed around. Both will then cover text |
| `--facing-pages` | lay the pages out as reader's spreads — `1 \| 2-3 \| 4-5` — instead of singly. Only needed where the file does not describe a booklet itself: a print sheet that reaches two pages side by side and a page count that is a multiple of four are read as one, and a document nobody has printed states no sheet |
| `--no-facing-pages` | lay every page out singly, overriding what the file says. For a document read as a booklet that is not one |
| `--log-file PATH` | write the log here instead of the per-user log folder (`%LOCALAPPDATA%\pub2idml\logs` on Windows, `~/Library/Logs/pub2idml` on macOS) |
| `--no-log` | do not write a log file |
| `-v`, `--verbose` | debug-level detail in the log (not the console) |
| `-q`, `--quiet` | print only the final summary, not each file |
| `-h`, `--help` | full usage |

**Exit code** is `0` when everything converted and `1` if any file
failed, so it can be used in a script:

```powershell
.\pub2idml.exe C:\Archive -o C:\Converted -q
if ($LASTEXITCODE -ne 0) { Write-Host "some files failed - check the report" }
```

#### What you get back

```
C:\Converted\
  Newsletters\
    March 2019.idml
    March 2019_images\        <- keep this next to the .idml
      image1.jpg
  conversion-report.csv
```

Open the `.idml` in Affinity and **save as `.afpub`**. Only then is the
`_images` folder no longer needed — until that point the `.idml` links to
it, and moving one without the other loses the pictures.

Start with the report, not the files: sort by `status`, deal with
`failed` and `review` first, and trust the `ok` rows.

**A `.idml` that exists is a `.idml` that finished.** The package is
assembled beside its destination and moved onto it only once the archive
and its images are both whole, so a run stopped by a full disk or a
killed process leaves nothing behind — rather than a truncated file that
every later run would skip as already converted.

## Logging

Every run writes a diagnostic log, so a batch that misbehaves on another
machine can be diagnosed without reproducing it:

```
Windows   %LOCALAPPDATA%\pub2idml\logs\pub2idml-<timestamp>.log
macOS     ~/Library/Logs/pub2idml/
Linux     ~/.local/state/pub2idml/logs/
```

The path is printed at the end of every run. The log records the version,
platform, whether it is a frozen bundle, the resolved parser path, each
file with its timing and counts, **libmspub's own stderr** (the best clue
when a file converts badly), every warning, and full tracebacks for
unexpected failures — including crashes, via an installed `excepthook`.

The console stays a short summary; detail goes to the file. Options:
`--log-file PATH` to choose the location, `-v` for debug-level detail,
`--no-log` to disable. The 30 most recent logs are kept.

## Use

Run `make` once to build `bin/pubdump`, then:

```sh
# one file
./pub2idml "files/Cantico_dei_Cantici.pub" -o converted

# a whole collection, recursively, mirroring the folder structure
./pub2idml ~/Documents/publisher-archive -o ~/Documents/converted

# reconvert everything, 8 at a time
./pub2idml ~/Documents/publisher-archive -o ~/converted --force -j 8

# summary only, no log file
./pub2idml ~/Documents/publisher-archive -o ~/converted -q --no-log
```

`./pub2idml` is a one-line shim for `python3 -m pubidml.cli`, so these
are the same thing:

```sh
./pub2idml files -o converted
python3 pub2idml.py files -o converted
python3 -m pubidml.cli files -o converted      # from the repo root only
```

**It works from any directory.** The parser is located relative to the
package rather than the working directory, so an absolute path is enough
and nothing needs installing or adding to `PATH`:

```sh
python3 ~/prive/tools/affinity-converter/pub2idml.py \
  ~/Documents/publisher-archive -o ~/Desktop/converted
```

The `python3 -m` form is the one exception — it needs the repo root as
the working directory, because the package is not on `sys.path` from
anywhere else. Use `pub2idml.py` from outside the repo.

There is nothing to install beyond `make`: the Python half is standard
library only, and **Microsoft Publisher is not required** on any
platform. Every option is listed under
[Options](#options) — the same flags apply here and on Windows.

Output:

```
converted/
  Newsletters/
    March 2019.idml
    March 2019_images/
      image1.jpg
  conversion-report.csv
```

Images are written to a sidecar folder next to each `.idml` and
referenced by relative link, which is how InDesign packages normally
carry placed artwork. **Keep the `_images` folder next to the `.idml`**
until you have opened it in Affinity and saved as `.afpub`.

### Optional: EMF vector artwork

Publisher embeds clip-art as Windows metafiles. Most are empty stubs left
where a picture placeholder used to be — those are detected and dropped
silently, because there is no artwork in them to lose.

A **pasted photograph** is also stored as a metafile, one whose only
drawing record wraps an uncompressed bitmap. Those are unwrapped directly
to PNG with no external tool, at their exact pixel dimensions — the two
in the sample set are 1348x894 and 262x198, and both are 100% bitmap.
This is the common case and it needs nothing installed.

What is left is genuine *vector* clip-art, and the two formats take
different routes.

**WMF is translated into IDML paths** — no external tool, and the result
is editable vector artwork rather than a picture. That is tractable
because the vocabulary is closed: every WMF record in the whole sample
corpus is one of 22 types and only six of them draw (`META_POLYGON`,
`META_POLYLINE`, `META_POLYPOLYGON`, `META_RECTANGLE`, `META_ELLIPSE`,
`META_LINETO`), each mapping onto a shape the writer already emits. The
rest set up the object table, the coordinate window, or device state.
Every object a metafile creates is numbered in one shared table -- fonts
and regions alongside the pens and brushes -- so all of them take a slot,
or a later `META_SELECTOBJECT` paints a shape in another shape's colours.
Records outside that set are counted and reported, so a partial
conversion says so instead of looking complete.

**EMF line art is rasterised** to PNG if two optional tools are present:

```sh
brew install libemf2svg imagemagick
```

Without them it is dropped and the report says so.

Losses are reported per distinct artwork, not per frame. Publisher repeats
one logo across every page, so a newsletter that used to produce 64
identical warnings now produces one, naming the drawing-record count and
how many copies were affected.

### Text in non-Latin scripts

libmspub ignores the document code page and decodes every byte as
Latin-1, so Cyrillic, Greek and similar text arrives as mojibake —
`Ðóññêèé òåêñò` instead of `Русский текст`. The original bytes survive,
so this is reversible, and the converter repairs it automatically.

Guessing the code page wrongly would destroy correct text, so detection
is deliberately conservative and needs two independent signals to agree:
a high share of non-ASCII letters, *and* those letters appearing in long
unbroken runs. Accents in Western European text are isolated — `être`,
`Zoë`, `Grüße` all give runs of one — so Dutch, French, German, Italian
and Portuguese are never touched, even when heavily accented. Among the
candidate code pages, the winner must also beat the runner-up by a clear
margin on letter-frequency plausibility; a coin flip is declined and the
text left alone.

Override with `--codepage cp1251` to force one, or `--codepage none` to
disable repair entirely.

### The report

`conversion-report.csv` is the point of the tool at collection scale.
Each row carries page/frame/image/character counts, the fonts the file
needs, and a status:

| status | meaning |
|---|---|
| `ok` | converted, nothing suspicious |
| `review` | converted, but something was approximated or dropped — see `warnings` |
| `failed` | not converted — see `error` |
| `skipped` | passed over: the `.idml` was already there. Use `--force` to redo |

Sort by `status`, then by `characters` descending, and you have a triage
queue: the files worth a human's attention first.

**A skipped file still gets a row**, and that is what makes the report
survive being run twice. The CSV is rewritten from scratch on every run,
so a file left out of the results is a file left out of the report — and
a collection converted once and then run again used to come back
described by a header row and nothing else, with the only way to
regenerate it a full `--force` re-conversion.

Two more things the report is careful about, both of which used to pass
as `ok`:

- **A `.pub` whose own structure cannot be read says so.** Every pass that
  reads the file directly returns quietly in that case, which is the
  contract — recovery is an improvement on the output, never a
  prerequisite. But five of them going quiet together is not nothing: the
  page-number fields stay `#`, the master content stays copied onto every
  page, and the cell insets, tab stops, gradient ramps and WordArt are all
  left as libmspub reported them. That is now one warning naming all of
  it, so the file lands in `review` rather than in the bucket that means
  "nothing suspicious".
- **Nothing in a cell can be read as a formula.** Font names and locale
  tags come out of the `.pub` verbatim and the report is meant to be
  opened in a spreadsheet, so a cell starting `=`, `+`, `-`, `@`, a tab or
  a carriage return is prefixed with an apostrophe. CSV quoting does not
  cover this: it keeps the file parseable, and the spreadsheet still
  evaluates what it parses.

## Known limitations

These are real and deliberate, not bugs to be surprised by later.

- **WMF clip-art is translated, not rasterised, and two things are
  approximated.** The drawing records become real IDML paths, so the
  artwork arrives as editable vectors with no external tool — see below.
  A `META_POLYPOLYGON` becomes one polygon per ring in paint order, since
  the writer emits a single subpath per shape; WMF clip-art composites by
  overpainting rather than by even-odd holes, so this reproduces the usual
  case, but a genuine knockout hole will fill. And only the anisotropic
  window mapping is implemented, which is what the corpus uses; a metafile
  declaring no window extent is fitted to its frame from its own bounds.
  Anything not translated is counted and named in the report rather than
  quietly skipped.
- **EMF vector artwork still needs the optional tools.** Only the WMF
  records are translated. An EMF that is a wrapped bitmap is unwrapped
  losslessly with no dependency, but genuine EMF line art still goes
  through `emf2svg-conv` and ImageMagick, and is dropped with a warning
  when they are absent. The same translator could be pointed at EMF
  records, which are a cleaner format.
- **WordArt headlines are recovered from the file.** A Publisher headline
  set in WordArt used to vanish twice over. libmspub reports the object as
  an *empty* text frame, which is dropped for having no text, no fill and
  no stroke; and it reports the two guide edges the glyphs are stretched
  between as a path that outlines no area. The words themselves are in the
  Escher stream, in properties libmspub has no constants for, and that is
  where `pubfile` now reads them: the text, the font, the point size, the
  rotation, the character formatting and the shape the glyphs were bent
  into.

  The guide path is then replaced by a text frame carrying the words. The
  band and the rotation come from the shape's own anchor, the paint from
  what libmspub already reported for the same shape, and the two halves
  are tied together by the only thing they share — the centre of that
  band, which both sides put in the same place to a fraction of a point.
  Across the corpus that is 47 headlines, every one of which was
  previously lost: *Meditatie*, *Kerkdiensten*, *Dankbetuigingen*, *Il
  Cantico dei Cantici*, and the slanted *Kerkbode* masthead at its own
  −12.19°.

  One shape can arrive as several paths, because libmspub makes a draw
  call per paint: a headline that is both filled and outlined reports the
  same two guides twice. They are collected and merged into one frame —
  the fill becomes the colour of the glyphs, a ramp stays a ramp on them,
  the outline becomes their stroke — and the repeats are dropped. Taken
  singly they used to leave a pair of rules drawn across every masthead in
  the corpus. Where a shape has no fill at all, which is what WordArt
  filled with a texture reports, the outline is what colours the words.

  **WordArt keeps its character formatting on the shape, not on the text**,
  so bold, italic, underline, strikethrough and character spacing were
  being lost with the shape even though the file states them plainly —
  sixteen booleans packed into one property, whose top half says which of
  them the file states at all. They are read and put back on the run: 40
  of the corpus's 48 headlines are italic and one is bold, which used to
  come out uniformly regular. Spacing is stated as a multiple of normal —
  1.2 is what Publisher's gallery calls Loose, and 36 shapes state it —
  where IDML states the space *added*, in thousandths of an em, so it is
  converted against an average advance of half an em and the report says
  the tracking is close rather than exact.

  **What cannot come across is the bending — but that is rarer than it
  sounds.** The file names the shape it asked for, in the shape record, and
  47 of the corpus's 48 WordArt shapes ask for plain unbent type. For
  those, straight text in the band is not an approximation of the
  headline, it *is* the headline, and the report now says so instead of
  telling every reader that fifteen headlines "may need restyling". The
  one bent shape in the corpus — a *button curve* — is named by the shape
  Publisher asked for, so whoever redraws it knows what to draw.

  **A headline is stretched to its band, measured against the font it is
  actually set in.** Every WordArt shape in the corpus states the stretch
  flag — the file saying the glyphs are fitted to the shape rather than set
  at a size and left there — so the band is not a box the words sit inside.
  It *is* the words, and two things follow from that.

  The **height decides the size**. Each line takes its share of the band,
  since a band holds as many lines as the words are set on and sizing a
  three-line headline from the whole band trebles it; the line inking most
  of its em is the one that has to fit. The **width decides the
  condensation**, not the size, because IDML states that directly as
  `HorizontalScale` — which is exactly what WordArt's stretch does, and the
  reason a stated point size is a floor rather than the truth.

  How much of an em a face inks is read from the font file itself
  (`pubidml/fontmetrics.py`, standard library only: `head` for the em,
  `cmap` for the glyphs, `hmtx` for their advances, `glyf` for the box each
  one inks). Until now it was three hand-fitted averages applied to every
  font — 0.70 of an em inked, 0.55 per glyph, half an em per advance —
  measured off two rendered headlines. Real faces are nowhere near that
  uniform: Monotype Corsiva averages 0.38 of an em per glyph and Arial
  Black 0.60, and Pristina inks 1.10 ems across a word with a descender in
  it. What that changes, on `1336 kerkbode.pub`:

  | headline | states | set at | condensed to |
  |---|---|---|---|
  | Meditatie | 20 pt | 39.8 pt | 73% |
  | Kerkdiensten | 20 pt | 34.1 pt | 86% |
  | Kerkbode *(masthead)* | — | 98.3 pt | 104% |
  | D *(dropped initial)* | — | 57.9 pt | 121% |
  | Schoonmaakrooster | 20 pt | 25.6 pt | 98% |

  *Meditatie* is the check: Publisher draws it at about 38 pt against the
  20 it states, and this puts it at 39.8. The dropped initial inks 39.7 pt
  of its 40.1 pt band on Publisher's own page, which a cap set at 57.9 pt
  in Pristina does.

  **A font this machine cannot read falls back**, in two steps rather than
  one. Monotype Corsiva and Pristina set 46 of the corpus's 48 headlines
  and both ship with Office rather than with an operating system, so a
  small table of faces measured once carries their proportions to a machine
  that lacks them; a family in neither the system nor the table takes the
  old global averages. Neither of those earns a horizontal scale —
  condensing by a ratio worked out from a guessed width would state a
  precision that is not there — so those headlines keep the older rule: the
  smaller of what the height and the width allow, and no condensation. The
  report names the font, because installing it is the fix.

  Because WordArt fits its glyphs to the shape, the band is not a box the
  words sit somewhere inside — it *is* the words. So the text is centred
  across it rather than hung off a corner, and the frame stays exactly the
  band: making it taller, so a headline wrapped by a substituted font had
  somewhere to go, was tried and taken back out, because the extra height
  only holds the words in place if the reader centres them vertically and
  hangs the headline half a band high if it does not.

  **A recovered headline states its own first baseline**, and without that
  it is not drawn at all. Affinity hangs a frame's first baseline one
  `usWinAscent` below the frame's top and hides the line — draws nothing,
  keeps the frame — when that baseline would fall past the frame's bottom.
  A band is by definition shorter than that: it is what the glyphs *ink*,
  0.69 of an em for the corpus's dropped initial, against a win ascent of
  0.86. Every headline whose band is tighter than its face's ascent
  therefore vanished, which on `1337 kerkbode.pub` was the dropped *D*,
  *Meditatie* and *Financiën*.

  Sizing the type down until the ascent fits would set that *D* at 46.4pt
  where Publisher draws it at 57.9, and growing the frame moves the wrap
  the body copy flows around. So the baseline is stated instead, as IDML's
  `FirstBaselineOffset="LeadingOffset"` with the leading set to where it
  belongs: a headline is sized so its ink fills the band, so the share of
  that ink sitting above the baseline is the share of the band above it —
  39.2pt of that *D*'s 40.1pt band, which is where Publisher's own PDF
  export draws it, to a fifth of a point. On a headline set on several
  lines the first line states the baseline and every line after it steps
  down by the band's own share.

  `LeadingOffset` and not `FixedHeight`: the two were measured side by
  side and Affinity draws the headline *above* its box for `FixedHeight`,
  the same lifting-off-position answer a table gets from it. The frame is
  top-aligned rather than centred for the same measured reason — a reader
  centres nothing it has decided will not fit. All of it is
  `research/probe_wordart_initial.py`, `probe_wordart_fit.py` and
  `probe_wordart_baseline.py`, in that order: twelve cells each, one thing
  changed per cell, read off in Affinity.

  **A headline of one glyph is not stretched to its band.** The band is
  the bounding box of the *slanted* text — WordArt italic, on faces like
  Pristina that ship no italic and are therefore slanted by whoever draws
  them — so part of its width is slant overhang rather than room for
  glyphs. On a long headline that overhang is a few percent of a wide
  band; on a single dropped initial it is a fifth of a narrow one.
  `research/wordart_stretch.py` measures it by fitting the outline
  Publisher's PDF export draws against the outline in the font file, which
  closes to 0.017pt across 41 points:

  | file | glyph | drawn | fitting advances to the band | stating no scale |
  |---|---|---|---|---|
  | 1336 | D | 95.7% | 121.2% | 104.6% |
  | 1338 | L | 97.0% | 151.9% | 103.4% |

  **What that measurement does not settle is the stretch on a longer
  headline.** The overhang the fit recovers — 9.5pt of a 44.9pt band —
  predicts one of those two initials to within 0.25% and the other not at
  all, and the two share a band exactly, so the corpus holds one geometry
  to fit against rather than two. The long headlines cannot supply another
  from this machine either: the Monotype Corsiva installed here is a
  different cut from the one Publisher drew with (34 outline points
  against 49 for a capital *M*), so only the Pristina shapes can be
  measured at all. Until a `.pub` supplies an italic band of another
  shape, a headline of more than one glyph keeps the rule it has, and is
  wide by however much of its band is slant — a few percent on the
  corpus's headlines, and more the shorter the headline.

  **A WordArt shape libmspub reported *nothing* for is placed from the file
  instead, once the file has been made to prove where it goes.** There is no
  guide path to replace, so the words, the band and the rotation all come
  from the .pub — and the page does too: every Escher shape carries its own
  seqnum, every page chunk lists the shapes on it, and no shape in the
  corpus is listed by two pages. Which page libmspub *emitted* for a given
  chunk is then measured rather than assumed, by matching the shapes both
  sides describe: the file's page order and libmspub's turn out to be a
  permutation of one another in every multi-page file here.

  Placement waits for two confirmations from the event stream and declines
  without either — another shape of the same page chunk, which says which
  page this is, and an empty band, which says the headline has not already
  arrived by another route. The one shape in `Cantico_dei_Cantici.pub` that
  needed this satisfies both, so all 48 WordArt shapes in the corpus now
  convert; a shape that cannot satisfy them is still named in the report so
  its words can be retyped, along with which confirmation was missing.
- **A filled path of disconnected edges that is *not* WordArt still cannot
  be filled.** libmspub reports most paths as several subpaths — 50 of the
  56 in the sample corpus — and where each is a bare two-point segment, a
  fill has no area to cover. Each subpath is written as its own outline,
  which is what the path data says; joining them would invent geometry, and
  doing so used to draw a filled bowtie across the page. Every case in the
  corpus turned out to be WordArt guides and is now recovered as text
  above; anything left is counted and flagged `review`. Only a *filled*
  one is reported that way: the same two edges with a stroke on them do
  draw, so "encloses no area and draws nothing" would be the wrong
  complaint, and WordArt recovery claims both shapes either way.
- **CJK text is not repaired.** The code page detector (see below) works
  on alphabetic scripts, where letter frequency is a usable signal. For
  Chinese, Japanese and Korean it declines to guess rather than risk
  corrupting text, so those documents still need `--codepage cp932` or
  similar passed explicitly.
- **Master pages are reconstructed where the file allows it.** libmspub
  never announces masters; it resolves them internally and replays their
  shapes onto each page. The converter reads the master structure out of
  the `.pub` itself and lifts that repeated content back onto real IDML
  master spreads, so a running header is stored once rather than copied
  onto every page. A Publisher master covering a facing pair becomes two
  masters, which looks identical and differs only in editing structure.
  Which master a page applies is measured rather than assumed: the file
  lists its pages in an order that is not libmspub's, so each page is
  matched to the chunk describing it by the shapes both halves state the
  position of. Anything that does not line up cleanly stays flattened,
  exactly as before — nothing is ever moved on a guess.
- **Page numbers are resolved, not live.** Publisher stores a page-number
  field as a literal `#` and libmspub has no field handling at all, so
  the converter reads the master-page structure out of the `.pub` itself
  and substitutes the real number per page. The result is correct but
  static: reordering pages in Affinity will not renumber them. Files
  where this fires are flagged `review`. A `#` is only ever replaced when
  the document carries a field table *and* the text came from a master,
  so a typed `#` is left alone.
- **Margin and column guides are carried**, read out of the `.pub`
  rather than from libmspub, which reports exactly two properties for a
  page: `svg:width` and `svg:height`. The file keeps one set of guides
  per publication, as *positions* on the page rather than insets from its
  edges, each flagged for whether it is a margin or an interior column
  guide. Confirmed against Publisher with a controlled pair of documents
  and then checked against the whole corpus, where six files of nine come
  back symmetric and the three newsletters come back with a column guide
  down the middle of an A5 page, which is what they are.

  Each page gets a `MarginPreference` with the guides resolved against
  its own size. Publisher draws a guide, not a gutter, so a column guide
  becomes two columns that meet — gutter zero. A page the guides do not
  fit inside keeps the reader's default: a margin that cannot be true is
  worse than none, because the reader would draw it. No baseline grid is
  carried; the file may state one, but nothing here has looked.
- **Facing pages are read from the file, and can be overridden.** A
  booklet is laid out as reader's spreads — the cover alone as a recto,
  then `2-3`, `4-5`, so odd numbers stay right of the spine — with
  `FacingPages` declared so the reader agrees. `--facing-pages` forces it
  on where the file does not say so, `--no-facing-pages` forces it off.

  What is read is the *shape* of a booklet, not Publisher's layout type,
  and it takes two things that have to agree. The file states the print
  sheet it is imposed onto, and that sheet has to reach two of these pages
  side by side without reaching three; and the page count has to be a
  multiple of four, which is what a saddle stitch folds. In the three
  newsletters the sheet reads 914.0 × 681.4pt against a 421.0 × 595.0pt
  page, which is the sheet Publisher's own exported PDF of them is imposed
  onto to a fifth of a point. Across the 22-file corpus this fires on
  exactly those three and on nothing else.

  Neither half is enough alone: a count of four describes any four-page
  document and a two-up sheet describes a flyer printed two to a page.
  Pages that differ in size are never guessed at, since reader's spreads
  assume one sheet throughout. A folded card has this shape too and would
  be laid out facing — which is what a folded card wants. Every document
  read this way says so in its report and is flagged `review`, because a
  spread layout is the most visible thing about a converted file and one
  decided rather than asked for should not be silent.

  **The limit worth knowing.** The sheet lives in a printer devmode blob —
  `0x06`/`0x07` are resolutions, `0x11`–`0x16` printer margins — not in the
  document, and it is absent from every file in the corpus nobody has
  printed. So a booklet that was never printed carries nothing to read and
  needs `--facing-pages` after all. Reading Publisher's own layout type
  instead would remove that limit, and is `actions.md` §12.

  Publisher's layout type itself is **not** readable, which is why the
  shape is read instead. libmspub has no fold or facing concept anywhere in
  it: `parseDocumentChunk` reads `DOCUMENT_SIZE` and `DOCUMENT_PAGE_LIST`
  and `skipBlock`s every other block, and `startDocument` arrives with an
  empty property list. In the file, the candidate is chunk `0x8F` block
  `0x0A`, which reads 4 in all three newsletters and in nothing else that
  has the chunk — but that is three files from one monthly template against
  one counter-example, in a chunk that is otherwise printer settings. A
  lead, not a reading; `actions.md` §12 has the controlled pair that would
  settle it.

  A master spread follows the layout: one page wide where the document is
  single-page, and where it is facing, a page on each side of the spine
  that pages apply it from — which is what InDesign's own facing master
  spread is. That matters because master content is carried onto a page by
  `MasterPageTransform`, written as the identity, and the identity is only
  the true matrix when the master page sits at the same offset as the
  pages taking their content from it. Content on a facing master is
  therefore written once per side, since a running head on one really is
  two frames. `research/probe_masterspread.py` builds both cases and says
  what to read off each; it is still the only thing that exercises the
  path, because no `.pub` in the corpus does.

  What the newsletters' own sheet size confirms is what this is *for*.
  `1336` and `1338` export from Publisher as fourteen sheets of
  914 × 681pt, two A5 pages up, imposed `28|1`, `2|27`, `26|3` … `14|15` —
  a saddle-stitched booklet. Affinity's PDF export writes pages in document
  order and never imposes; the imposition lives in Print, and it needs a
  facing document with a page count that is a multiple of four to find the
  spine. Converted singly, neither condition holds and no print order it
  produces can be the right one.
- **Blank pages are put back.** libmspub calls `startPage` only for a page
  carrying shapes of its own, so a page whose content comes from its master
  alone — a numbered, otherwise empty leaf — never reaches the event stream
  at all. The loss is silent and it is not at the end: `1336 kerkbode.pub`
  drops its page 23 of 28 and `1337 kerkbode.pub` its page 17 of 32.
  Everything after arrives one place early, which puts the wrong number on
  every later page and, laid out facing, moves each of them to the wrong
  side of the spine.

  It is libmspub's own doing, and its source says so plainly.
  `MSPUBCollector::writePage` opens with
  `if (!shapeGroupsOrdered.empty())` — where `shapeGroupsOrdered` is the
  page's *own* shapes, before any master is written — so a page with none
  emits no `startPage`, no master replay, nothing.

  Chunk `0x44` holds the fix, and libmspub names it: `DOCUMENT` is `0x44`
  and its block `0x02` is `DOCUMENT_PAGE_LIST`, which
  `parseDocumentChunk` walks straight into `setNextPage` — so that array is
  precisely the order libmspub itself pages the document in, and not the
  chunk order, which is a permutation of it (§14). Across all 22 files in
  the corpus the entries of that list which do carry shapes are exactly
  libmspub's pages in exactly libmspub's order, measured independently by
  the shapes both halves state the position of. So an entry without shapes
  standing between two of them is a page whose position is stated rather
  than guessed, and it is inserted there, empty.

  Confirmed against Publisher rather than inferred: its own exported PDFs
  of `1336` and `1338` impose fourteen two-up sheets each, so both
  documents are 28 pages where libmspub reports 27 and 28. After the pass
  both read 28, `1337` reads 32, and the pages either side of each gap are
  the ones that were either side of it.

  Two limits. Publisher keeps a scratch band of unused page chunks at the
  tail of the list, and a blank page at the very end of a document cannot
  be told from it, so the trailing run is dropped rather than guessed at —
  a missing final leaf is a sheet you add in a second, one inserted
  wrongly renumbers everything after it. And the restored page arrives
  bare: the master is applied per page from the items libmspub drew, of
  which a blank page has none, so Publisher's number and running head are
  not on it. Files where this fires are flagged `review`.
- **Text frame columns are carried.** Publisher offers a column count and
  one uniform spacing, which is exactly what IDML calls `TextColumnCount`
  and `TextColumnGutter`, so the mapping is direct. Affinity honours both
  on import, verified by opening `research/probe_columns.py`'s package.
  The gutter is always written explicitly when there is more than one
  column: InDesign's own default is 12pt against Publisher's 2mm, so
  leaving it out would widen every gap and narrow every column.

  Two details that look like bugs and are not. Affinity's UI rounds the
  gutter to one decimal, so Publisher's 2mm shows as 5.7pt where the file
  says 5.6664 — display only. And that figure is 5.6664 rather than the
  exact 5.66929 because librevenge stringifies inch properties to four
  decimals, so libmspub reports `0.0787in`; the 0.003pt shortfall is a
  thousandth of a millimetre.

  No `.pub` in the sample set has a multi-column box, so this path has no
  corpus coverage. libmspub emits `fo:column-count` only when the file
  recorded one, and across nine files and 300-odd text objects it never
  appears, while `fo:column-gap` appears on nearly all of them — the gap
  alone is no evidence of columns. Note too that a two-column Publisher
  article may be one box with two columns *or* two linked boxes: identical
  on the page, different structures, and the second is handled by story
  threading below rather than by this.
- **Line spacing is converted, resting on one equivalence.** Publisher
  measures it either in "spaces" — multiples of single line spacing — or in
  points. libmspub reports the first as a *percentage* and the second as
  points, so `90%` means 0.9 spaces and **not** 90% of the type size; the
  difference is a fifth of the leading. Points map straight to IDML
  leading. Spaces are proportional, so they are resolved against each run's
  own type size, taking single spacing to be IDML's own Auto leading of
  120%: 0.9 spaces of 10pt type gives 10.8pt.

  That equivalence is what makes the commonest case right by construction.
  libmspub omits the property at exactly 1 sp, so single-spaced text is
  written with no leading at all and inherits Auto — the same 120%.

  Two consequences. Leading is a character property in IDML, not a
  paragraph one, so a paragraph mixing type sizes gets a value per run and
  the largest on each line wins, which is what Publisher does too. And a
  run whose size libmspub never reported is assumed to be 12pt, IDML's
  default — which is also the size it will be rendered at, so the leading
  stays proportionally correct even there.
- **Text carries the language it is written in.** libmspub reports
  `fo:language` and `fo:country` on every run, and both were read nowhere.
  This is not styling and nothing about it is visible directly, but it
  decides hyphenation: Dutch text broken by English rules reflows, and in
  a two-column newsletter that moves every line after the first bad break.
  Each run now states an `AppliedLanguage` and the document declares a
  `Language` for each locale it uses, which is what InDesign itself
  writes — a Czech document's designmap declares Czech and nothing else.

  IDML names languages by a display string rather than a locale tag, so
  the mapping is by name: `nl-NL` is `$ID/Dutch`, `en-US` is
  `$ID/English: USA`. A country IDML does not list falls back to the bare
  language, which is the same hyphenation dictionary — `fr-CA` and
  `fr-FR` are both French to a reader that names only French. Where there
  is no fallback either, nothing is written and the run keeps the
  reader's own default: `en-AU` has no plain "English" to fall back to,
  and naming a neighbouring dictionary would be choosing one the file
  never did. Those runs are counted in the report.
- **Tab stops are carried where the file states one, and most tabs have
  none.** libmspub reports five paragraph properties and no tab stop is
  among them, which is not because Publisher does not record them: the
  parser reads them into `ParagraphStyle::m_tabStopsInEmu` and its
  collector never looks at that member again, so they are dropped before
  librevenge sees anything. `pubfile` reads them out of the Quill stream
  instead — a position in EMU per stop, and an alignment byte on the
  centre and right tabs of a header or footer.

  Nothing in the event stream says which paragraph libmspub is reporting,
  so a stop is tied to its paragraph by the text: a paragraph is given the
  stops the file states for that same text, and where one text is stated
  two different ways neither is applied, the rule two tables drawing one
  grid already get.

  Across the corpus **203 paragraphs contain a tab and 3 of them state a
  stop**. The other 200 were lined up on the document's own default grid
  — "Default tab stops" in Publisher's Format → Tabs dialog, and
  `Document.DefaultTabStop` in its VBA, a per-publication value. The
  Quill stream's `SGP ` chunk states it, and each of those paragraphs is
  written out with an explicit ruler of left stops at that spacing,
  covering the width its tabs have to cross — one stop past the edge
  where the interval does not divide it, because Publisher's grid has no
  end and a ruler that stopped inside the frame would drop the last tabs
  back on the reader's. Without it they would land on InDesign's own
  default grid of half an inch, which three of the nine corpus files put
  four and a half times too far apart.

  A file stating **no** interval gets no ruler, because there is nothing
  to write: half an inch is the reader's default and not Publisher's —
  all thirteen files created from scratch on the metric install state
  an interval of their own, 28.3pt — so such a document's grid is unknown
  rather than known to match, and its tabbed paragraphs are counted in
  the report. What the `SGP ` block means is **confirmed against
  Publisher itself**: `? ActiveDocument.DefaultTabStop` reads back
  8.07874 on `1336 kerkbode.pub` and 28.28976 on `Lisa Hoogendijk.pub`,
  against 8.0787 and 28.2898 from the block — and 28.2898 is a value no
  other candidate predicts. A document given a ruler still says so in the
  report, with the interval it was given, because a tabbed column is
  worth a glance either way.

  One position is recoverable without any stop at all, and is written: a
  hanging indent implies a stop at its left indent, because that is where
  the wrapped lines start and what the tab after the outdented label is
  reaching for. Publisher and Word both honour it without recording it.
- **Story threading is read from the file.** librevenge's drawing
  interface cannot say "this frame continues that one", so libmspub hands
  the *complete* story to every frame in a linked chain — one sample
  carried the same 11,121-character article eight times, 73% of that
  file's apparent text. Written through verbatim that puts the article on
  the page once per frame, each one overset. The frames are threaded into
  a single IDML story that reflows across them instead.

  **Which frames are linked, and in what order, comes out of the .pub.**
  Each shape names the story it holds and its own place in that story, so
  the frames sharing a story *are* the chain and the index puts them in
  flow order — no inference, and in particular no assumption that the
  story flowed with the page sequence. A story only one shape holds is
  not a chain, which is how a page-number footer stays a footer: it is
  one master shape replayed onto every page, not a run of frames.

  A shape is matched to the frame libmspub drew for it by centre, and the
  search is one page wide rather than document wide, since a newsletter
  repeats its layout and a column on one page sits exactly where the
  column on another does. The file says which page chunk holds the shape
  and the chunk-to-page mapping says which page that is.

  **Where the record cannot be reached, the frames are measured instead**
  — a chain is recognised by the one thing that distinguishes it from a
  genuinely repeated label: the story does not fit the frame holding it,
  which is *why* the boxes were linked. That is what a file whose
  structure will not read still gets, and what covers a chain the event
  stream drew only part of. It can only see a chain that oversets, so a
  chain whose text fits its first frame is invisible to it — which is
  what reading the record fixes. The report says which of the two each
  chain came from. Files where this fires are flagged `review`.
- **A story libmspub cuts short is completed from the file.** libmspub
  builds its character runs from the Quill run tables, and where it
  misreads them for a story it emits *a run per character* — alternating
  bold letter by letter, a stray colour on a single glyph — and then
  stops partway through. In `1337 kerkbode.pub` one story arrives holding
  81 of the 4,562 characters the file states for it, cut mid-word at
  `voor over|leg`: a two-page article that reaches the page as one line,
  under a heading, with the facing page left empty. libmspub's own
  `pub2raw` truncates it identically, so this is upstream of the shim and
  not something the JSON boundary introduces.

  **The words come from the file.** `TEXT` holds every story run
  together with nothing between them, and `STRS` is the ruler that
  divides it: a count, two words nothing reads, then one length per story
  in characters. Those lengths are trusted only when they add up to the
  `TEXT` chunk exactly — which is what says this is the `STRS` layout and
  not something else the same four letters name, and which holds on every
  file in the corpus that carries text at all.

  A frame is completed only where the file settles what is missing beyond
  argument: exactly one story has the delivered text as a prefix, **and
  no story in the file *is* that text**. The second half is what leaves a
  complete frame alone — a short label like `Datum` opens a longer story
  elsewhere in the same document, and a frame holding all of its own
  story must never be extended with somebody else's. Both frames of a
  linked chain are filled, since libmspub hands the story to each of
  them, and threading collapses them afterwards; the count in the report
  is per story rather than per frame.

  The restored text carries the format of the last run that did arrive —
  the same sentence carrying on, and the only statement about its format
  this has. Inventing nothing further is the point: what failed is
  libmspub's reading of the formatting, so the run that survives at the
  cut is better evidence than the fragments before it. The 70 characters
  libmspub did deliver keep its reading, junk and all, because they are
  its reading and not ours. Files where this fires are flagged `review`,
  and the warning says how much went back into how many stories.
- **A story libmspub never delivers at all is found by its id.** The
  same failure has a limit case the rule above cannot touch. For five
  frames of `1337 kerkbode.pub` libmspub emits `startTextObject` and then
  `endTextObject` with not one paragraph between them — pages 24 and 25,
  a two-page Open Monumentendag letter, reach the page as a photograph
  and a page number. Matching on the text cannot help here, and not by
  accident: **every story in the file begins with nothing**, so a prefix
  rule asked about an empty frame is ambiguous by construction.

  The file answers without going through the words. A shape names the
  story it holds by *id* (`_SHAPE_STORY_ID`, the field the linked-frame
  chains are already read from), and `SYID` lists those ids in the same
  order `STRS` cuts the text — a word nothing reads, the story count,
  then one id per story. Compose the two and a shape has a position in
  `story_texts`. That is an identity the file states, not an inference
  from what happens to be on the page, and it holds on all 22 files in
  the corpus: `SYID` names exactly as many stories as `STRS` cuts, always
  ascending, always unique, and every id a shape carries is in it.

  Two guards. The shape-to-frame match must be **injective** — in
  `Lisa Hoogendijk.pub` a decorative shape sits 0.2pt from a text frame,
  inside the half-point the match allows, so both claim that frame and
  the decoration's story is not the one that belongs there; where two
  shapes claim one frame, neither speaks for it. And only frames
  libmspub left *entirely* empty are filled, so a frame that received
  text keeps it and a story that is itself empty stays empty.

  **The type is nobody's.** Text merely cut short continues the run that
  did arrive, so its format came from Publisher. Here nothing arrived,
  so there is no run to take a format from and the words land in the
  document's default face at its default size. The words are the file's;
  the type has to be put back by hand. Files where this fires are
  flagged `review`, and the warning says so in those terms.

  One consequence reaches `model`: an empty frame is no longer dropped
  as it is parsed, because at that point a frame Publisher left blank
  and a frame libmspub failed to fill look identical. Both are placed,
  and `convert._drop_blank_frames` decides between them once the file
  has had its say — the same deferral `_drop_blank_tables` already makes
  for a grid whose rules have not been read yet.
- **Groups are flattened.** Children keep their absolute positions;
  nothing moves, but the grouping is gone.
- **Gradients are carried, with every stop.** They become real IDML
  gradient resources, shared between shapes that use the same ramp, with
  Publisher's angle passed through — folded into the half turn either way
  that IDML states angles in, since the corpus reports a −225°. Only a ramp
  with fewer than two usable stops falls back to a flat colour, and the
  report says how many did.

  **The ramp is read from the file, because libmspub reports only its
  middle.** Publisher states a gradient as two colours — the shape's fill
  and its fill-back — with waypoints between them. libmspub reads all
  three and then, whenever there is a waypoint list at all, builds the
  ramp from that list *alone* and drops both ends. Where the list holds a
  single waypoint that leaves one stop, which is nothing to ramp between
  and paints the shape flat; where it holds several, the ramp survives but
  begins and ends in the wrong colours. In the three newsletters that is
  **32 shapes flattened outright** and **32 more missing their ends**: the
  banner behind every section heading runs white to brown through grey and
  came out flat grey, and the heading bars run navy to white and came out
  light blue to pale blue.

  Those colours are in the Escher stream, so `pubfile` reads them there —
  resolving palette references and intensity changes the way
  `ColorReference` does — and rebuilds the ramp. Three details decide
  whether the result is Publisher's ramp or merely a plausible one:

  - **Which end it starts from.** Publisher's *focus* says so, and at 100,
    which is every shape in the corpus that states a waypoint list, the
    ramp runs from the fill-back colour and the waypoints run backwards
    with it, each at its distance from the other end.
  - **Its angle.** Three transformations sit between the file and
    `draw:angle`: degrees in the high half of a fixed-point word, two
    angles the format states ninety degrees askew, and a negation, since
    ODF measures clockwise. A flattened fill never became a gradient at
    all, so its angle was lost with the ramp.
  - **Which shape it belongs to.** By where the shape sits, and — where a
    banner and its backing panel share a centre to within half a point —
    by which is nearer its size. The size cannot be *required* to match:
    the anchor measures the shape with its outline while libmspub reports
    the path inside it.

  What makes this a reading rather than a second opinion is that libmspub
  reports 32 of these ramps in full, and on every one of them the
  waypoints reconstructed here are identical to its own, stop for stop,
  and so are the angles. What is added is the pair of colours it drops. A
  fill whose shade list is empty is left alone: libmspub builds those from
  the two end colours itself, and gets them right.

  Worth knowing why this mattered: keeping just the first stop is how a
  background disappears rather than merely flattening. The ramps in the
  sample corpus start white — `#ffffff → #ffeedd → #ffffff` for a panel,
  `#913801 → #ffd17d` for the masthead ribbon — so the flat stand-in was
  white on white paper. Publisher's shade ramps also repeat an offset to
  make a hard edge, and those offsets are passed through as reported rather
  than evened out, which would smooth the effect away.

  **Text takes a ramp too.** A WordArt headline is painted the way a shape
  is, so the ramp goes on the run rather than on a frame, and the outline
  with it. Without that the *Kerkbode* masthead — the ribbon whose
  brown-to-gold is the example above — came back through WordArt recovery
  flattened to its first stop again, with the gold end absent from the
  package altogether.

  **A ramp needs its *geometry* stated, on a shape as much as on a run.**
  An angle says which way the ramp runs, not how far, and the default is a
  length of nothing — which paints everything before the start point in
  the first colour and everything after it in the last. The masthead came
  out brown for its left half and gold for its right, with a hard edge
  down the middle, and so did every shape: a banner reading white → brown
  was white for the half of it right of centre and brown for the half left
  of it, and the cream panel behind an article was cream to its middle and
  bare paper after it. Nothing about that says "gradient", which is why it
  read as a fill that had gone wrong rather than as a ramp with no room.
  The box is the distance the ramp was meant to run over — the shape's own
  bounds, or for a headline the band it was stretched into — and that is
  what is written, projected onto the ramp's own angle.

  **A ramp is given both its ends.** Publisher's often occupy only part of
  their range, 32 → 49 or 3 → 69, and what happens outside that is the
  reader's choice: hold the end colours, as Publisher does, or stretch the
  ramp to fit. Those look nothing alike, so a stop is written at 0 and at
  100 in the colour already there. The reported offsets themselves are
  untouched, repeats and all.

  **A see-through ramp is carried as far as IDML allows.** An IDML gradient
  stop has a colour and a position and no opacity, so transparency can only
  be stated for the object as a whole. That is exact whenever the stops
  agree, which is every case in the corpus — 42 shapes, all of them a
  two-stop ramp with both stops at 60% and no stroke to fade along with the
  fill. A ramp whose stops *differ* in opacity cannot be carried at all,
  and is counted in the report rather than averaged into something the file
  never said.
- **Transparency is opacity, not a tint.** A shape's own `draw:opacity`
  used to be written as `FillTint`, which is a different thing: a tint
  mixes the colour with the paper, so a 78% fill came out pale rather than
  see-through and looked right only over white. It is now a
  `BlendingSetting`, IDML's real opacity. This applies to the whole object
  including its stroke, where Publisher's applies to the fill; every
  transparent shape in the corpus is unstroked, so the two coincide.
- **Shadows are carried.** libmspub reports a Publisher shadow in full —
  colour, both offsets and opacity — and all of it used to be dropped,
  flattening 15 shapes in each newsletter. It becomes an IDML
  `DropShadowSetting` with no blur, no spread and no noise, because
  Publisher's shadow is a flat offset copy and a reader's own defaults
  would soften it. IDML states the offset twice, as X/Y and as an angle
  with a distance; both are written and they agree.
- **Elliptical arcs are approximated** with straight segments.
- **Tables keep their grid.** libmspub describes a Publisher table
  completely — a width per column, a height per row, and a row/column pair
  plus any spans on every cell — so it becomes a real IDML table rather
  than cells flowed into one frame as consecutive paragraphs. Cells that a
  span covers are not emitted twice. A grid that is empty of *everything*
  is dropped, the same rule an empty text frame follows — **and the report
  says how many went**, one counted line per document rather than one per
  table.

  Empty of everything is the whole of that test, and it used to be asked
  too early. A table's lines and fills are not in the event stream at all;
  they are shapes in the drawing stream, and until they were read a blank
  ruled grid and a blank one looked identical. So 13 of the 31 tables in
  the three newsletters were dropped as contributing nothing — and 11 of
  those 13 rule themselves, which is the whole of what a layout grid
  contributes. The question is now asked after the rules have been read
  onto the cells, which leaves 2 dropped across the three files and keeps
  the other 11 with their lines on.

  **A row keeps the height the file states.** Publisher's row heights have
  no slack in them — a row measures the leading of the text it holds and
  under a point more — and a row in a reader can only grow, so anything
  written into a cell that the row cannot hold pushes every row under it
  down and the grid sinks out of the frame it was placed in. Two things
  used to do that, and both were the converter inventing rather than
  reading. Paragraph space after, which libmspub reports on 351 cell
  paragraphs of the corpus at 14pt against rows of 9 and which Publisher
  lays out none of, is now dropped at a cell's own edges — the first
  paragraph's space before and the last one's space after, the two that
  have nothing to space away from — while space between two paragraphs of
  one cell is left alone. And a cell Publisher left empty, which it records
  with no run and often no paragraph at all, is set the way the body of its
  own table is set instead of in the reader's own 12pt on Auto. A third followed from
  the same rule: Publisher opens line spacing above single *between* lines
  rather than above the first one, so a cell holding one line is as tall
  as that line however wide the spacing is set, and a cell led above
  single is written at its natural line rather than at 150% of it.
  Measured against the frame Publisher gives each table — which is the
  right yardstick, since a frame taller than its own grid is Publisher
  having grown the rows itself — all 18 tables in the corpus now render
  within a point of it, where the worst was 621pt out. `backlog.md` §11
  carries the numbers and the one case that still renders *short* of its
  frame.

  **Cell insets are carried**, read out of the .pub rather than from
  libmspub, which stops at a cell's row and column and marks the rest of
  the record "width/height of content + margins?" in a comment. The file
  gives four insets per cell in EMU, and the table's own grid — a width
  per column and a height per row, both also in the file — is what ties a
  cells chunk back to the table the event stream is showing. Across the
  newsletters that is 18 tables and 974 cells, every one matched.

  This matters most where the padding is small. The newsletters build
  their two-column look from layout tables whose gutter column is an
  eighth of an inch — 9pt — so a reader's own default inset applied to
  both sides of it leaves nothing to set text in. A cell the .pub did not
  describe is left without inset attributes rather than written as zero,
  so the default still applies wherever we genuinely do not know.

  Affinity honours all four, and honours a zero: checked on Affinity
  Publisher for macOS with `research/probe_cell_insets.py`, whose rows
  differ only in their insets. The uninset row sits tight against every
  edge rather than picking up a default, a 24pt inset moves the text by
  24pt, and the 9pt gutter column still sets text.

  **Cell vertical alignment is carried too**, from the same record: field
  `0x07`, which reads 1 for centre and 2 for bottom and is left out where
  the cell is top-aligned — the way this format leaves out an inset of
  zero. libmspub stops before it. Read back in Publisher from a table set
  top down one column, centre down the next and bottom down the third,
  and written out as IDML's `VerticalJustification`. It sits on 869 of
  the 974 cells converted, so it is the difference between text sitting
  where Publisher put it and text sitting at the top of every cell.

  What that probe also showed, unasked, is that **the cell rules used to
  be Affinity's, not Publisher's.** We reference `TableStyle/$ID/[Basic
  Table]` without defining it, so the reader supplied its own, and the one
  Affinity supplies draws a line around every cell — a black grid over the
  layout tables these newsletters are built from, in a colour and a weight
  no .pub states.

  **Every cell we have read now says its four edges carry no rule**, both
  ways IDML can say it: a stroke weight of zero and a stroke colour of
  `Swatch/None`. Across the newsletters that is the same 18 tables and 974
  cells, all four edges on each. Which is safe to write was measured, not
  argued: `research/probe_cell_rules.py`, opened on Affinity Publisher for
  macOS, states cell edges six ways over one 3 x 3 table. A per-cell
  override beats the default outright — 4pt magenta where asked — it lands
  per edge, a stated edge stands alone against a neighbour that says
  nothing, and each of the zero, the `Swatch/None` and the two together
  removes the line. No table style has to be defined for any of it.

  The reasoning for writing them at all is the format's own rule, that **a
  field the file leaves out is absent rather than defaulted**: a cell
  record states its padding and its alignment and nothing else, in all
  1,260 of them, so a cell we have read is a cell Publisher recorded no
  lines for *in that record*. A cell whose record we never read is left
  alone, the same way its insets are. A plain table made in Publisher was
  since checked and prints no lines, so the zeros are not deleting a
  default of Publisher's own.

  **Publisher does rule tables, and the lines are not in those records —
  they are read now, out of the Escher stream.** A styled table and a
  plain one have an identical `Contents` chunk inventory; the shading and
  the rules are in the drawing stream instead, as a shape per shaded cell
  and a shape per ruled run, tied back to their table by the seqnum its
  own chunk carries.

  A rule is not stated as a side of a cell. It is a segment on the grid's
  *lattice* — the lines between cells, numbered from zero — running from
  one lattice point to another, which is why one shape can rule a whole
  row of cells at once and why 80 of the corpus's 604 do. The record that
  on an ordinary shape is the anchor box carries it: an orientation, a
  start row and column and an end row and column, with a zero left out
  rather than written. Both cells along an interior line are given it,
  because a cell whose record was read has its unruled sides written off
  and a zero on the other side of the line would otherwise argue with the
  rule. Publisher draws each one as a thin filled rectangle rather than as
  a stroke — the shape's own fill booleans say so — so the colour to read
  is the fill, resolved through the file's palette like every other, and
  the weight is the line width the shape still carries.

  **A merged cell is ruled on its footprint.** A rule is stated per grid
  position and a spanning cell covers several, so its four sides are the
  sides of what it covers: the top of every column it spans, the bottom
  of the last row it reaches, and so on. A line lying *inside* a merged
  cell is one IDML cannot draw — there is a single stroke per side — and
  is left out rather than promoted to a whole side Publisher never ruled.
  Across the three newsletters that is 24 rules that would otherwise have
  been dropped, and none invented.

  Checked against the one sample whose ruling is known from outside the
  file: a 3 × 3 table drawn to order in Publisher with two shaded cells,
  every side of R2C1 ruled and the top of R2C2. All nine cells come out
  exactly as drawn, colours included, and the plain control beside it
  comes out with every edge still off. Across `1337 kerkbode` that is 409
  ruled cell sides at 0.25, 0.5 and 2pt where all of them used to be
  written off as no line.

  **A cell's own runs count as text.** Fonts and colours were collected by
  walking text frames alone, so anything named only inside a table never
  reached the package: in two of the three newsletters that was Arial, left
  out of the font list while 104 runs named it, and a colour used only
  there would have resolved to nothing and been written as black. One
  shared walk — `model.Document.stories`, cells included — now feeds the
  fonts, the swatches, the gradient resources and the language list alike.

  **A cell's fill is carried the same way**, from a shape naming one cell
  rather than a run of lattice, and written as the cell's `FillColor`.
  What is *not* in the cell records remains true and is worth keeping
  straight: every cell record holds only its row and column bounds, its
  insets, its alignment and two cached extents, in all 1,260 of them. The
  rules and the shades were never there to find. A table's *own* fill and
  border arrive separately, as the rectangle libmspub draws behind it.

  **A field the file leaves out is absent rather than defaulted**, which
  is what lets a missing inset be read as zero, and it is measured rather
  than assumed. A cell states an inset exactly when it has one: 3,289
  sides are stated across the corpus and **not one of them is zero**,
  against 1,751 left out. The tables that mean Publisher's own 0.04in
  default write all four sides out explicitly, so omission cannot be the
  default either. And the geometry agrees independently — a cell caches
  the height of its laid-out text, and on the 454 rows grown to fit that
  text the row height less the top inset and that cached height leaves
  about nothing over, where a 0.04in bottom inset would leave 2.88pt.
  Omission is per-side rather than a truncation of the trailing ones:
  cells state a top inset while leaving the left one out.

  Two fields in each cell record remain unidentified and are not
  converted: one holding 1 or 2, uniform across a table — plausibly
  vertical alignment, whose enumeration in this format is top, middle,
  bottom — and one holding an eighth or a quarter of an inch. Guessing at
  the first would move the text in 869 of the 974 cells converted, so it
  waits for the same sample.
- **Fonts are referenced by name.** Affinity substitutes anything not
  installed — install the source fonts first, or expect reflow.
- **A source document can state a text box collapsed to nothing.** One
  sample has a 5.5 x 5.7 pt text frame holding 3,911 characters, which
  Affinity shows as an empty box. This is not a parsing artefact: the
  `.pub`'s own Escher anchor for that shape states the same box, so
  Publisher showed it empty too. Inventing a plausible size would be
  inventing layout, so the frame is left as stated and the file is
  flagged `review` with the character count and frame size, ready to be
  resized by hand.
- **Text wrap is inferred for images, and read from the file for
  headlines.** libmspub exposes no wrap data at all, and images arrive
  after the text in z-order, so without help they paint over the copy.
  Images therefore get a bounding-box wrap by default, which is what
  Publisher layouts almost always intend. Page-sized images are treated as
  backgrounds and left unwrapped. A recovered WordArt headline is not
  guessed at the same way: giving every headline a wrap moves text that
  Publisher never moved — a band clipping the corner of a date box would
  push the date out of it — so the wrap is taken per shape from the .pub,
  which states how far the text kept clear of a shape only where the wrap
  was on. That is what puts a dropped initial beside its paragraph instead
  of through it. Use `--no-image-wrap` for exact source stacking instead.

## Verification status

Verified by round-tripping through Affinity on macOS and measuring the
rendered output, not by inspection alone.

**Geometry is exact.** A test document with known rectangles rendered at
300 dpi and measured by connected-component analysis:

| | expected (pt) | measured (pt) |
|---|---|---|
| plain rect | 50, 50, 100×60 | 49.9, 49.9, 100.1×60.0 |
| plain rect | 412, 100, 150×80 | 412.1, 100.1, 150.0×79.9 |
| rotated 30° | bbox 162.6, 527.0, 274.8×176.0 | 162.7, 527.0, 274.6×175.9 |

Rotation is applied about the item centre, matching Publisher, and a
positive angle renders clockwise. Text frames measure equally exactly
(169.7 pt against a specified 170 pt), and text wraps within them.

Also confirmed against real documents: multi-page output, accented Latin
text, font and colour mapping, italics, placed images resolving through
the sidecar link folder, and text flowing around images.

**Rotation direction matches libmspub.** libmspub states a rotated shape
twice — as `librevenge:rotate` on the object, and as an outline polygon
in absolute page coordinates that it computes from that property — so the
outline says how the reference consumer reads its own field. Every
matched object in the corpus lands within 0.5pt of its outline, and the
shapes that discriminate are decisive: negating the sign moves the
corners of one 755 × 1155pt frame by 306pt. `research/rotation_sign.py`
prints it; `RotationSignTest` pins it. A LibreOffice render agrees.

**Not yet verified:** whether *libmspub* matches *Publisher* on that sign.
That needs a reference rendering from Publisher itself, and is covered by
the PDF exports actions.md asks for.

## Layout

```
Makefile              builds bin/pubdump
src/pubdump.cpp       libmspub → JSON event stream
pub2idml              CLI entry point
pubidml/
  units.py            length parsing, points conversion
  model.py            event stream → document model
  idml.py             document model → IDML package
  pubfile.py          the .pub read directly, for what libmspub drops
  fontmetrics.py      sfnt reading: what a string measures in a font
  metafile.py         WMF/EMF inspection and bitmap unwrapping
  wmf.py              WMF drawing records → IDML paths
  imagemeta.py        image dimensions and density
  textrepair.py       libmspub's code page bug
  logsetup.py         diagnostic log
  convert.py          single-file conversion
  cli.py              batch driver and CSV report
files/                sample .pub documents
research/             probes that answer one question each
```
