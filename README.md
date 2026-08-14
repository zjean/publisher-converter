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
| `--no-image-wrap` | keep the source's exact stacking instead of flowing text around images. Images will then cover text |
| `--facing-pages` | lay the pages out as reader's spreads — `1 \| 2-3 \| 4-5` — instead of singly. Use it for a booklet; libmspub never reports whether the publication was set up facing, so it has to be asked for |
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
rest set up the pen/brush object table, the coordinate window, or device
state. Records outside that set are counted and reported, so a partial
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

Sort by `status`, then by `characters` descending, and you have a triage
queue: the files worth a human's attention first.

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
  where `pubfile` now reads them: the text, the font, the point size and
  the rotation.

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

  What cannot come across is WordArt itself, since IDML has no warped or
  stretched type, so the headline arrives as straight text in its band and
  is flagged `review`. Two shapes in the corpus state no point size —
  WordArt fits the glyphs to the shape — and those are sized from the band
  by the ratio the sized shapes measure (1.33, spread 1.02 to 1.59), which
  the warning says out loud.

  Because WordArt fits its glyphs to the shape, the band is not a box the
  words sit somewhere inside — it *is* the words. So the text is centred
  in it both ways rather than hung off the top-left corner. The frame
  stays exactly the band: making it taller, so a headline wrapped by a
  substituted font had somewhere to go, was tried and taken back out,
  because the extra height only holds the words in place if the reader
  centres them vertically and hangs the headline half a band high if it
  does not. Centring inside the band is safe either way — ignored, it
  lands on the top of the band, where the words went before.

  A WordArt shape libmspub reported *nothing* for cannot be placed this
  way: without a shape in the event stream there is nothing to confirm
  where the words went, and putting them on the page on the strength of
  the .pub alone is how invented content gets in. One shape in
  `Cantico_dei_Cantici.pub` is in that position, and the report names its
  words so they can be retyped.
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
  Anything that does not line up cleanly stays flattened, exactly as
  before — nothing is ever moved on a guess.
- **Page numbers are resolved, not live.** Publisher stores a page-number
  field as a literal `#` and libmspub has no field handling at all, so
  the converter reads the master-page structure out of the `.pub` itself
  and substitutes the real number per page. The result is correct but
  static: reordering pages in Affinity will not renumber them. Files
  where this fires are flagged `review`. A `#` is only ever replaced when
  the document carries a field table *and* the text came from a master,
  so a typed `#` is left alone.
- **Margin and column guides are lost.** libmspub reports exactly two
  properties for a page, `svg:width` and `svg:height` — no margins, no
  guides, no baseline grid — so Affinity applies its own defaults.
- **Facing pages have to be asked for.** `--facing-pages` lays the document
  out as reader's spreads — the cover alone as a recto, then `2-3`, `4-5`,
  so odd numbers stay right of the spine — and declares `FacingPages` so
  the reader agrees. Without it every page is its own spread, exactly as
  before.

  It cannot be detected: `parseDocumentChunk` in libmspub reads
  `DOCUMENT_WIDTH` and `DOCUMENT_HEIGHT` and nothing else, so no
  publication type, book fold or pages-per-sheet reaches the event stream.
  The setting is presumably in the `.pub`, and could be found the same way
  the margins are being pursued in `actions.md` — with a controlled pair of
  files from Publisher — at which point the flag becomes a default rather
  than a question. Master spreads stay one page wide either way; whether
  Affinity applies a single-page master to a facing spread is worth a look
  the first time you use this on a document with a running header.
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

  The limit is what the file holds rather than what can be read out of it.
  Across the corpus **203 paragraphs contain a tab and 3 of them state a
  stop**; the rest were lined up on Publisher's document-wide default
  grid, which is not recorded in the file anywhere yet found. Those tabs
  land on the reader's own grid instead — half an inch in InDesign — so
  anything tabbed into columns needs checking, and the report says how
  many paragraphs per document are affected.

  One position is recoverable without any stop at all, and is written: a
  hanging indent implies a stop at its left indent, because that is where
  the wrapped lines start and what the tab after the outdented label is
  reaching for. Publisher and Word both honour it without recording it.
- **Story threading is inferred, not read.** librevenge's drawing
  interface cannot say "this frame continues that one", so libmspub hands
  the *complete* story to every frame in a linked chain — one sample
  carried the same 11,121-character article eight times, 73% of that
  file's apparent text. Written through verbatim that puts the article on
  the page once per frame, each one overset. The converter instead
  recognises a chain by the one thing that distinguishes it from a
  genuinely repeated label: the story does not fit the frame holding it,
  which is *why* the boxes were linked. Those frames are threaded into a
  single IDML story that reflows across them, in page order. A repeated
  caption or a page-number field fits its frame and is left alone. Two
  consequences worth knowing: a chain whose text happens to fit its first
  frame is not detected and still arrives duplicated, and the link order
  is page order, so an article that flowed against the page sequence needs
  re-linking by hand. Files where this fires are flagged `review`.
- **Groups are flattened.** Children keep their absolute positions;
  nothing moves, but the grouping is gone.
- **Gradients are carried, with every stop.** They become real IDML
  gradient resources, shared between shapes that use the same ramp, with
  Publisher's angle passed through — folded into the half turn either way
  that IDML states angles in, since the corpus reports a −225°. Only a ramp
  with fewer than two usable stops falls back to a flat colour, and the
  report says how many did.

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

  A run needs its ramp's *geometry* stated as well as the ramp. A shape
  takes that from its own bounds, but a run has no bounds of its own and
  the default is a length of nothing, which paints everything before the
  start point in the first colour and everything after it in the last: the
  masthead came out brown for its left half and gold for its right, with a
  hard edge down the middle. The band the headline was stretched into is
  the distance the ramp was meant to run over, and that is what is
  written, projected onto the ramp's own angle.

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
  span covers are not emitted twice. An all-empty grid with no fill or
  stroke is dropped, the same rule an empty text frame follows.

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

  What that probe also showed, unasked, is that **the cell rules are
  Affinity's, not Publisher's.** We reference `TableStyle/$ID/[Basic
  Table]` without defining it, so the reader supplies its own, and the
  one Affinity supplies draws a line around every cell. Whether a
  Publisher table with no ruling recorded should print no lines or
  Publisher's own default lines is unsettled — but letting the reader
  decide is the one answer that is certainly not the file's. `actions.md`
  §9 settles it with the same sample.

  Cell *fill and ruling* are still not carried, and this is a property of
  the corpus rather than a gap in the reader: no table in any sample file
  records either. Every cell record holds only its row and column bounds,
  its insets, and two cached extents, and a field the file leaves out is
  absent rather than defaulted — one table writes its insets as
  Publisher's own 0.04in default on all four sides explicitly, so the
  writer states what it means. A ruled or shaded table therefore still
  needs a sample before it can be read; `actions.md` says how to make
  one. A table's *own* fill and border do arrive, as the rectangle
  libmspub draws behind it.

  Two fields in each cell record remain unidentified and are not
  converted: one holding 1 or 2, uniform across a table — plausibly
  vertical alignment, whose enumeration in this format is top, middle,
  bottom — and one holding an eighth or a quarter of an inch. Guessing at
  the first would move the text in 869 of the 974 cells converted, so it
  waits for the same sample.
- **Fonts are referenced by name.** Affinity substitutes anything not
  installed — install the source fonts first, or expect reflow.
- **libmspub sometimes reports a degenerate frame size.** One sample has
  a 5.5 x 5.7 pt text frame holding 3,869 characters, which Affinity
  shows as an empty box. Inventing a plausible size would be inventing
  layout, so the frame is left as reported and the file is flagged
  `review` with the character count and frame size, ready to be resized
  by hand.
- **Text wrap is inferred, not read.** libmspub exposes no wrap data at
  all, and images arrive after the text in z-order, so without help they
  paint over the copy. Images therefore get a bounding-box wrap by
  default, which is what Publisher layouts almost always intend.
  Page-sized images are treated as backgrounds and left unwrapped. Use
  `--no-image-wrap` for exact source stacking instead.

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

**Not yet verified:** whether Publisher's rotation *sign* matches ours.
The magnitude and pivot are right, but confirming the direction needs a
reference rendering of the same `.pub` — either Publisher itself, or
LibreOffice, which drives the same libmspub and so shows how the
reference consumer reads the property.

## Layout

```
Makefile              builds bin/pubdump
src/pubdump.cpp       libmspub → JSON event stream
pub2idml              CLI entry point
pubidml/
  units.py            length parsing, points conversion
  model.py            event stream → document model
  idml.py             document model → IDML package
  convert.py          single-file conversion
  cli.py              batch driver and CSV report
files/                sample .pub documents
```
