# A window for people who will never open a terminal

## The problem

`pub2idml.exe` is a command line tool, and the README says so plainly:
"double-clicking it just prints the usage and closes." That is the right
shape for the person converting a corpus and reading the CSV, and the wrong
shape for the person this tool was built for — someone with a folder of
parish newsletters, no Python, no terminal, and a deadline of
1 October 2026, after which Publisher will not open the files at all.

The conversion engine is not the gap. `convert.convert()` is already a
clean, thread-safe unit of work, already driven in parallel, already
atomic on disk. What is missing is a way to reach it without typing.

This spec designs that window: a four-step wizard, in Dutch, shipped as a
second Windows executable beside the existing one, with macOS to follow.

## Scope

In: the wizard, the extraction of the batch loop that makes two front ends
possible, the Dutch string layer, the second PyInstaller spec, and the CI
change that proves the result starts on a clean machine.

Out: macOS packaging (section 8 records what it will take, and nothing
here blocks it), any change to the conversion engine, and any change to
the CLI beyond the extraction.

## The audience decides the design

Every choice below follows from one fact: the person double-clicking this
has never heard of IDML and will not be supported by anyone when it goes
wrong. Three consequences run through the whole design.

**No option they cannot judge.** `--codepage`, `-j`, `--no-image-wrap`,
`--facing-pages`, `--no-recursive` all stay on the CLI. Each of them asks a
question this user cannot answer, and each already has an automatic default
that is right far more often than a guess would be. The wizard exposes two
things: what to convert, and where to put it.

**Recursion is not a setting.** `cli.find_sources` already globs `**/*.pub`
and `cli.destination_for` already mirrors the tree. Dropping a folder means
converting everything inside it, at every depth, into the same shape
underneath the destination. `--no-recursive` is the opt-out, and it is not
offered.

**Plain language, in Dutch, all the way to the end.** The CSV's
`status=review` is not an answer. Section 5 covers this.

## 1. Module boundaries

One rule holds the design together:

> `pubidml/gui/` imports from `pubidml/`. Never the reverse. `tkinter` is
> imported nowhere outside `pubidml/gui/`.

That is what lets the console build keep `excludes=["tkinter"]` in
`pub2idml.spec` and stay the size it is today.

```
pubidml/batch.py     NEW. The job loop, lifted out of cli.run().
pubidml/cli.py       Shrinks to argparse, printing, and a call to batch.
pubidml/gui/
  app.py             Wizard shell: Tk root and the step state machine.
  steps.py           One frame class per step.
  strings.py         Every Dutch string, as module constants.
  explain.py         Result -> one plain Dutch sentence. Pure.
  runner.py          Thread and queue bridge from batch to the Tk loop.
pub2idml_gui.py      Entry shim, mirroring pub2idml.py.
pub2idml-gui.spec    console=False, same binaries block.
```

## 2. Extracting the batch loop

`cli.run()` currently fuses five jobs into ninety lines: parsing arguments,
planning which files to skip, driving the thread pool, printing each result,
and writing the CSV. A GUI needs the middle three without the first and
fourth.

Reimplementing them in the GUI is the failure mode to design out. The
subtle knowledge lives in that loop — that a skipped file still becomes a
`Result` so the report describes the whole tree, that a dead worker becomes
a failed row rather than losing the batch, that the report is written in a
`finally` so an interrupted run still leaves a triage list. Two copies of
that would disagree within a month.

So `pubidml/batch.py` gets three functions, none of which print or parse
arguments:

```python
def plan(sources, source_root, output_root, force) -> tuple[list[Job], list[Result]]
def run_batch(jobs, options, on_result=None, cancel=None) -> list[Result]
def write_report(path, results) -> None
```

`plan` returns the jobs to do and the `Result` rows for files skipped
because their `.idml` already exists. `run_batch` owns the
`ThreadPoolExecutor` exactly as today, calls `on_result` as each future
completes, and checks `cancel` — a `threading.Event` — between submissions.
`write_report` and `REPORT_COLUMNS` move across unchanged.

`cli.run()` then becomes argparse, a call to each of the three, and its
existing printing. Its behaviour must not change; the existing CLI tests are
the proof, and they should pass untouched.

`KeyboardInterrupt` stays the CLI's business. `run_batch` knows only about
the `cancel` event, and the CLI sets it from its own handler.

## 3. The four steps

**1 — Kiezen.** A large panel inviting a folder onto the program's icon,
with `[Map kiezen…]` and `[Bestanden kiezen…]` beneath it. On selection the
tree is scanned and the result stated plainly: *145 Publisher-bestanden
gevonden in 12 mappen.* Finding nothing ends here, with an explanation,
rather than becoming an empty run three screens later.

If `sys.argv[1:]` holds paths — which is what Windows passes when a folder
is dropped on the `.exe` — step 1 arrives answered and advances itself. That
is the drag-and-drop gesture people actually use, and it costs nothing.

**2 — Bestemming.** Defaults to a `Converted` folder beside the source; for
a file selection spanning several folders, `Documents\Converted`. A
`[Wijzigen…]` button, and one line of consequence: *De mappenstructuur wordt
hierin overgenomen.*

This step blocks one mistake: a destination inside the source tree. That
would leave a later run walking over its own output, and it is the only
genuinely destructive thing an unadvised click can do here.

**3 — Bezig.** A determinate bar (*63 van 145*), the current filename,
elapsed time, and `[Annuleren]`.

Cancelling is safe by construction rather than by cleanup. The README's
guarantee — "a `.idml` that exists is a `.idml` that finished", because the
package is assembled beside its destination and moved onto it only when
whole — means a stopped run leaves no partial file. Cancel therefore stops
submitting, lets in-flight conversions finish, and goes to step 4 with the
results it has. Nothing is deleted.

**4 — Klaar.** Counts in plain Dutch, the failures listed by filename with a
readable reason, the Affinity follow-up, and two buttons — open the folder,
open the report.

```
Klaar — 142 van de 145 bestanden

  ✓ 139 goed omgezet
  ! 3 even controleren
  ✗ 3 konden niet gelezen worden
      March 2004.pub — dit is geen Publisher-bestand,
      of het bestand is beschadigd

Hierna: open elk .idml-bestand in Affinity en kies
Bestand → Opslaan als… Laat de map _images ernaast
staan totdat u dat gedaan heeft.

[ Map openen ]   [ Rapport openen ]
```

The `_images` sentence is not padding. It is the one way a successful
conversion still loses the pictures, and the only place the user can be
warned is here.

## 4. Threading

Tk is single-threaded, and touching a widget from another thread corrupts
it silently rather than raising.

So `run_batch` runs on one `threading.Thread`, and keeps its own
`ThreadPoolExecutor` inside — the GUI does not flatten the parallelism it
already has. Its `on_result` callback puts each `Result` on a
`queue.Queue`. The Tk main loop drains that queue from `root.after(100, …)`,
and that drain function is the only code that touches a widget.

Cancel is the `threading.Event` from section 2, set by the button and read
by the worker.

## 5. Dutch

Every user-facing string is a module constant in `pubidml/gui/strings.py`.
No `gettext`, no `.po` files, no locale detection: Dutch is the only
language, and building the machinery for a second before one is asked for
is exactly the speculative work to avoid. Centralising the strings is what
keeps a second language an addition rather than a rewrite.

`explain.py` is the translation seam for errors. It was needed anyway — to
turn a `ConversionError` into something a non-technical reader can act on —
and it now lands in Dutch:

| `ConversionError` | shown |
|---|---|
| `not a supported Publisher file (or corrupt)` | Dit is geen Publisher-bestand, of het bestand is beschadigd. |
| `document contains no pages` | Dit bestand bevat geen pagina's. |
| `timed out after 300s` | Dit bestand duurde te lang om te openen. |
| `could not launch parser: …` | Het omzetprogramma kon niet gestart worden. |
| `IDML write failed: …` | Het omzetten lukte, maar het bestand kon niet opgeslagen worden. |

The table is matched by **prefix**, not equality. Several of these are
f-strings — `f"timed out after {PARSE_TIMEOUT_S}s"`,
`f"could not launch parser: {exc}"`, `f"IDML write failed: {exc}"`,
`f"malformed event stream: {build_error}"` — so an exact-match lookup would
quietly miss the majority of them and fall through to English. Matching on
the fixed leading text is what makes the table work at all.

An **unmapped** error falls through verbatim, in English, followed by *— zie
het rapport*. That is deliberate: swallowing an unrecognised diagnostic to
protect the Dutch surface would cost the one clue a support conversation
has. English text appearing in the window is a bug report for a missing row
in that table.

Two things stay English on purpose. The **CLI** — a different audience, and
translating it forks its tests for no reader. And the **diagnostic log**,
which exists to be read by whoever debugs a machine they cannot inspect.

The **CSV report** also stays English, in one shared format written by
`batch.write_report` for both front ends. A Dutch dialect of it would mean
two column contracts to keep in step, and step 4 is already the Dutch triage
surface; the CSV is the escape hatch for whoever is helping.

Two consequences of Dutch for the layout, both from the same cause — Dutch
runs roughly a fifth longer than English:

- No fixed pixel widths. `grid` with weighted columns, and `wraplength` on
  the instruction labels, sized against the Dutch strings rather than the
  English mock they were drawn from.
- Step 4 quotes **Affinity's own Dutch menu wording** (*Bestand → Opslaan
  als…*), not a translation of the English menu. A non-technical user
  matches the words on their screen literally, and a near-miss sends them
  hunting.

## 6. What is deliberately not built

No codepage, jobs, image-wrap or facing-pages controls. No
`--no-recursive`. No per-file results table. No thumbnails or preview. No
drag-and-drop onto the window itself — only onto the `.exe`, via `argv`,
which is free.

The in-window drop is worth one line on why not: the usual library,
`tkinterdnd2`, is a compiled extension, and the project's standing property
is that it needs no third-party Python packages at all. Trading that for a
gesture already covered by the icon drop is a bad trade.

## 7. Testing and CI

`batch.py` gets tests it never had as part of `cli.run()`: cancel partway
through a batch, `on_result` firing once per job, and `plan` classifying
existing output as skipped. The existing CLI tests are the guard on the
extraction itself and must pass unchanged.

`explain.py` and the wizard state machine are pure and test headless. The
widget wiring is not, and is not pretended to be.

The CI change that matters is one line of discipline the project already
practises. `build-windows.yml` does not merely build — it copies the
executable somewhere bare, strips `PATH` down to `C:\Windows\system32`, and
converts real files, so a missing DLL fails the build rather than the user.
The GUI needs the same proof, because bundling Tcl/Tk is exactly the kind of
thing PyInstaller can quietly get wrong:

- a second PyInstaller pass over `pub2idml-gui.spec`;
- in the PATH-stripped directory, run `pub2idml-gui.exe --self-test`, which
  builds the window, tears it down, and exits 0;
- both executables uploaded to the release.

## 8. macOS, later

The same code, no rewrite — tkinter is on both platforms and `ttk` picks up
native widgets on each. The work is entirely packaging, and it is a
different problem from Windows:

- a `.app` via PyInstaller's `BUNDLE`;
- Homebrew's libmspub **dylibs** bundled and their install names rewritten
  with `install_name_tool` — the Mach-O equivalent of `make dlls`, and not
  the same job;
- codesign and notarisation. Windows' unsigned executable costs a
  SmartScreen click-through; an unsigned, un-notarised `.app` is refused by
  Gatekeeper outright, so this one is not optional.

Nothing in sections 1 to 7 has to change for it.
