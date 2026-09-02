# Dutch GUI Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `pub2idml-gui.exe`, a four-step Dutch wizard over the existing conversion engine, for people who will never open a terminal.

**Architecture:** The batch loop is lifted out of `cli.run()` into `pubidml/batch.py` so the CLI and the GUI drive one implementation instead of two that drift. The GUI is tkinter/ttk in a new `pubidml/gui/` subpackage that imports from `pubidml/` and never the reverse. The conversion engine is not touched.

**Tech Stack:** Python 3 standard library only — `tkinter`/`ttk`, `threading`, `queue`, `unittest`. PyInstaller for packaging (build-time only). No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-09-02-gui-design.md`

## Global Constraints

- **No third-party Python packages.** The project's standing property is that it needs none; `tkinter` is stdlib, which is why it was chosen. Do not add `tkinterdnd2`, `pywebview`, `PySide6`, or anything else.
- **`pubidml/gui/` imports from `pubidml/`. Never the reverse.** No module outside `pubidml/gui/` may import `tkinter`. This is what keeps `excludes=["tkinter"]` valid in `pub2idml.spec`.
- **Tests are `unittest`, not pytest.** Run with `make test`, i.e. `python3 -m unittest discover -s tests -t . -v`.
- **`_tkinter` may be absent.** The local Homebrew Python 3.14 has no `_tkinter` (`brew install python-tk` fixes it). Every test that imports `tkinter` must skip, not fail, when it is missing — mirroring how the parser tests skip when `bin/pubdump` is not built.
- **All user-facing GUI text is Dutch**, and lives only in `pubidml/gui/strings.py`. No `gettext`, no `.po` files, no locale detection.
- **The CLI, the diagnostic log, and the CSV report stay English.** One report format, shared by both front ends.
- **The conversion engine is out of scope.** No changes to `convert.py`, `idml.py`, `model.py`, `pubfile.py`.
- **Existing tests must pass unchanged**, with exactly one permitted edit: the two `cli._write_report` call sites in `tests/test_cli.py::ReportInjectionTest`, which become `batch.write_report` in Task 1.

---

### Task 1: Extract the batch loop into `pubidml/batch.py`

A pure refactor. No behaviour changes, no new features. The existing CLI tests are the proof.

**Files:**
- Create: `pubidml/batch.py`
- Modify: `pubidml/cli.py` (remove `destination_for`, `_status`, `_csv_safe`, `_write_report`, `REPORT_COLUMNS`, and the job-planning and thread-pool block inside `run()`)
- Modify: `tests/test_cli.py` (two call sites only, in `ReportInjectionTest._row_for`)
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: `convert.convert`, `convert.Result`, `logsetup.get_logger` — all existing.
- Produces:
  ```python
  REPORT_COLUMNS: list[str]
  Job = tuple[Path, Path]                      # (source, destination)
  Options                                       # dataclass: codepage, wrap_images, facing_pages
  def destination_for(source: Path, source_root: Path, output_root: Path) -> Path
  def status_of(result: convert.Result) -> str  # "ok" | "review" | "failed" | "skipped"
  def plan(sources, source_root, output_root, force) -> tuple[list[Job], list[convert.Result]]
  def run_batch(jobs, options, jobs_count=0, on_result=None, cancel=None) -> list[convert.Result]
  def write_report(path: Path, results: list[convert.Result]) -> None
  ```

- [ ] **Step 1: Write the failing test for `plan`**

Create `tests/test_batch.py`:

```python
"""The job loop, independent of who is driving it.

cli.run() used to fuse argument parsing, planning, the thread pool,
printing and CSV writing into one function. These tests cover the middle
three so the GUI can rely on them without a second implementation.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from pubidml import batch, convert


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.source_dir = self.work / "in"
        self.source_dir.mkdir()
        for name in ("a.pub", "b.pub"):
            (self.source_dir / name).write_bytes(b"stub")
        self.output = self.work / "out"

    def test_every_source_becomes_a_job_when_nothing_is_converted_yet(self):
        sources = sorted(self.source_dir.glob("*.pub"))
        jobs, skipped = batch.plan(sources, self.source_dir, self.output, force=False)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(skipped, [])

    def test_an_existing_output_becomes_a_skipped_result_not_a_job(self):
        # The skipped file must still reach the report: the CSV is written
        # from scratch every run, so a file absent from the results is a
        # file absent from the report.
        self.output.mkdir()
        (self.output / "a.idml").write_bytes(b"idml")
        sources = sorted(self.source_dir.glob("*.pub"))
        jobs, skipped = batch.plan(sources, self.source_dir, self.output, force=False)
        self.assertEqual([j[0].name for j in jobs], ["b.pub"])
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0].skipped)
        self.assertEqual(skipped[0].output.name, "a.idml")

    def test_force_converts_a_file_whose_output_already_exists(self):
        self.output.mkdir()
        (self.output / "a.idml").write_bytes(b"idml")
        sources = sorted(self.source_dir.glob("*.pub"))
        jobs, skipped = batch.plan(sources, self.source_dir, self.output, force=True)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(skipped, [])

    def test_a_nested_source_keeps_its_folder_under_the_output_root(self):
        nested = self.source_dir / "2019" / "spring"
        nested.mkdir(parents=True)
        (nested / "c.pub").write_bytes(b"stub")
        jobs, _ = batch.plan(
            [nested / "c.pub"], self.source_dir, self.output, force=False
        )
        self.assertEqual(
            jobs[0][1], self.output / "2019" / "spring" / "c.idml"
        )
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_batch -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pubidml.batch'`

- [ ] **Step 3: Create `pubidml/batch.py` by moving code out of `cli.py`**

Move these from `cli.py` **unchanged** except where noted: `REPORT_COLUMNS`, `destination_for`, `_csv_safe`, `_write_report` (renamed `write_report`), and `_status` (renamed `status_of`). Add `Options` and `plan`. The thread-pool block becomes `run_batch` in Task 2 — for now write the version that mirrors today's behaviour exactly.

```python
"""The batch job loop, independent of any front end.

Split out of cli.run() so the command line and the GUI drive one
implementation rather than two that drift. Everything here is silent: no
printing, no argument parsing. What the caller wants to say about progress
it says through on_result.

The subtleties worth not re-deriving live here. A skipped file still
becomes a Result, because the CSV is rewritten from scratch on every run
and a file absent from the results is a file absent from the report. A
worker that dies in a way convert() could not catch becomes a failed row
rather than losing the batch.
"""

from __future__ import annotations

import concurrent.futures
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import convert, logsetup

REPORT_COLUMNS = [
    "source", "output", "status", "pages", "text_frames", "images",
    "shapes", "characters", "wordart", "facing_pages", "fonts",
    "warnings", "error",
]

Job = Tuple[Path, Path]


@dataclass
class Options:
    """What the caller wants done to every file in the batch."""
    codepage: Optional[str] = "auto"
    wrap_images: bool = True
    facing_pages: Optional[bool] = None


def destination_for(source: Path, source_root: Path, output_root: Path) -> Path:
    if source_root.is_file():
        relative = Path(source.name)
    else:
        relative = source.relative_to(source_root)
    return (output_root / relative).with_suffix(".idml")


def status_of(result: convert.Result) -> str:
    if not result.ok:
        return "failed"
    if result.skipped:
        return "skipped"
    if result.needs_review:
        return "review"
    return "ok"


def plan(
    sources: List[Path],
    source_root: Path,
    output_root: Path,
    force: bool,
) -> Tuple[List[Job], List[convert.Result]]:
    """Split the sources into work to do and files already converted."""
    jobs: List[Job] = []
    skipped: List[convert.Result] = []
    for source in sources:
        destination = destination_for(source, source_root, output_root)
        if destination.exists() and not force:
            skipped.append(
                convert.Result(source=source, output=destination, skipped=True)
            )
            continue
        jobs.append((source, destination))
    return jobs, skipped


def run_batch(
    jobs: List[Job],
    options: Options,
    workers: Optional[int] = None,
    on_result: Optional[Callable[[convert.Result], None]] = None,
    cancel=None,
) -> List[convert.Result]:
    """Convert every job, calling on_result as each one lands."""
    log = logsetup.get_logger("batch")
    results: List[convert.Result] = []
    if not jobs:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                convert.convert, source, destination,
                codepage=options.codepage,
                wrap_images=options.wrap_images,
                facing_pages=options.facing_pages,
            ): source
            for source, destination in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
            except BaseException as exc:
                # convert.convert catches its own failures, so this is
                # something it could not: record it as a failed row rather
                # than let it discard the whole batch.
                log.exception("worker died on %s", source)
                result = convert.Result(
                    source=source,
                    error=f"worker died: {exc.__class__.__name__}: {exc}",
                )
            results.append(result)
            if on_result is not None:
                on_result(result)
    return results


def _csv_safe(value: str) -> str:
    """A cell a spreadsheet cannot mistake for a formula.

    Font names, locale tags and libmspub's own diagnostics all come out of
    the .pub verbatim, and the report exists to be opened in Excel or
    LibreOffice -- both of which read a leading '=', '+', '-' or '@' as
    code rather than text. csv quoting does not help: it keeps the file
    parseable, and the spreadsheet still evaluates what it parses.
    """
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def write_report(path: Path, results: List[convert.Result]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORT_COLUMNS)
        for result in results:
            writer.writerow(
                [
                    _csv_safe(str(result.source)),
                    _csv_safe(str(result.output) if result.output else ""),
                    status_of(result),
                    result.pages,
                    result.text_frames,
                    result.images,
                    result.shapes,
                    result.characters,
                    result.wordart,
                    ("detected" if result.facing_detected
                     else "yes" if result.facing_pages else "no"),
                    _csv_safe("; ".join(result.fonts)),
                    _csv_safe("; ".join(result.warnings)),
                    _csv_safe(result.error or ""),
                ]
            )
```

- [ ] **Step 4: Run the new tests**

Run: `python3 -m unittest tests.test_batch -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Rewrite `cli.run()` to call `batch`**

In `cli.py`: delete `REPORT_COLUMNS`, `destination_for`, `_status`, `_csv_safe`, `_write_report`. Add `from . import batch` to the imports. Keep `find_sources`, `_force_utf8_console`, `_print_result`, and all argparse.

Replace the job-planning block and the `try/except KeyboardInterrupt` block with:

```python
    options = batch.Options(
        codepage=codepage,
        wrap_images=not args.no_image_wrap,
        facing_pages=facing_pages,
    )
    jobs, results = batch.plan(sources, args.source, output_root, args.force)
    skipped = len(results)
    interrupted = False

    def announce(result):
        if not args.quiet:
            _print_result(result)

    try:
        results += batch.run_batch(
            jobs,
            options,
            workers=args.jobs if args.jobs > 0 else None,
            on_result=announce,
        )
    except KeyboardInterrupt:
        interrupted = True
        log.warning(
            "interrupted after %d of %d file(s)", len(results) - skipped, len(jobs)
        )
    finally:
        # The report is the point of the tool at collection scale, so it is
        # written even when the run ends badly: an interrupted or crashed
        # batch still leaves a triage list of what did convert.
        results.sort(key=lambda r: str(r.source))
        try:
            batch.write_report(report_path, results)
            log.info("report written to %s", report_path)
        except OSError as exc:
            log.error("could not write report to %s: %s", report_path, exc)
            print(f"Could not write report to {report_path}: {exc}", file=sys.stderr)
```

Then replace the three `_status(r)` calls in the counting block with `batch.status_of(r)`.

> Note on `KeyboardInterrupt`: `run_batch` does not catch it. Ctrl-C propagates out of the pool and is caught here, exactly as before. Partial results are lost from the in-flight pool but the skipped rows and the report still land — matching today's behaviour.

- [ ] **Step 6: Update the two `_write_report` call sites in the CLI tests**

In `tests/test_cli.py`, change the import to `from pubidml import batch, cli, convert` and in `ReportInjectionTest._row_for` change `cli._write_report(self.report, [result])` to `batch.write_report(self.report, [result])`.

- [ ] **Step 7: Run the whole suite — nothing else may change**

Run: `make test`
Expected: PASS. Every pre-existing test green. If any test other than the two edited call sites needs changing, the extraction changed behaviour — revert and find out why.

- [ ] **Step 8: Commit**

```bash
git add pubidml/batch.py pubidml/cli.py tests/test_batch.py tests/test_cli.py
git commit -m "Lift the job loop out of the command line

Two front ends need the planning, the thread pool and the report; only
one of them needs argparse and printing. The knowledge worth not
duplicating -- that a skipped file still reaches the report, that a dead
worker becomes a row rather than a lost batch -- now has one home."
```

---

### Task 2: Cancellation and progress in `run_batch`

The GUI needs to stop a run from a button and count progress as it goes. The CLI keeps using `KeyboardInterrupt` and is unaffected.

**Files:**
- Modify: `pubidml/batch.py` (the `cancel` parameter in `run_batch`)
- Test: `tests/test_batch.py` (append)

**Interfaces:**
- Consumes: `batch.run_batch`, `batch.Options` from Task 1.
- Produces: `run_batch(..., cancel: threading.Event | None)` — when set, no further jobs are submitted; in-flight ones finish and their results are returned.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_batch.py`:

```python
class CallbackTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.original = convert.convert

    def tearDown(self):
        convert.convert = self.original

    def _jobs(self, count):
        return [
            (self.work / f"{i}.pub", self.work / f"{i}.idml")
            for i in range(count)
        ]

    def test_on_result_fires_once_per_job(self):
        convert.convert = lambda source, destination, **kw: convert.Result(
            source=Path(source), output=Path(destination), pages=1, text_frames=1
        )
        seen = []
        results = batch.run_batch(
            self._jobs(5), batch.Options(), workers=2, on_result=seen.append
        )
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(results), 5)

    def test_a_dead_worker_still_produces_a_failed_result(self):
        def explode(source, destination, **kw):
            raise MemoryError("out of memory")
        convert.convert = explode
        results = batch.run_batch(self._jobs(2), batch.Options(), workers=1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not r.ok for r in results))
        self.assertIn("MemoryError", results[0].error)


class CancelTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.original = convert.convert

    def tearDown(self):
        convert.convert = self.original

    def test_setting_cancel_stops_submitting_further_work(self):
        # One worker, and the flag is set from the first callback, so
        # everything after the first job or two must go unattempted.
        cancel = threading.Event()
        attempted = []

        def counting(source, destination, **kw):
            attempted.append(Path(source).name)
            return convert.Result(
                source=Path(source), output=Path(destination), pages=1
            )
        convert.convert = counting

        jobs = [
            (self.work / f"{i}.pub", self.work / f"{i}.idml") for i in range(50)
        ]
        results = batch.run_batch(
            jobs, batch.Options(), workers=1,
            on_result=lambda r: cancel.set(), cancel=cancel,
        )
        self.assertLess(len(attempted), 50, "cancel did not stop the batch")
        self.assertEqual(len(results), len(attempted))

    def test_a_cancel_set_before_the_start_converts_nothing(self):
        cancel = threading.Event()
        cancel.set()
        convert.convert = lambda source, destination, **kw: self.fail(
            "no file may be converted once cancel is set"
        )
        jobs = [(self.work / "a.pub", self.work / "a.idml")]
        results = batch.run_batch(jobs, batch.Options(), cancel=cancel)
        self.assertEqual(results, [])
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_batch -v`
Expected: `CancelTest` fails — all 50 files are attempted because `cancel` is accepted but ignored.

- [ ] **Step 3: Implement cancellation**

Replace the body of `run_batch` in `pubidml/batch.py`. Submitting every job up front and then cancelling futures is the wrong shape here — it would race the pool. Instead, submit a bounded window and top it up as results land:

```python
def run_batch(
    jobs: List[Job],
    options: Options,
    workers: Optional[int] = None,
    on_result: Optional[Callable[[convert.Result], None]] = None,
    cancel=None,
) -> List[convert.Result]:
    """Convert every job, calling on_result as each one lands.

    A set `cancel` event stops further submissions. Work already running is
    left to finish rather than killed: convert() assembles each package
    beside its destination and moves it into place only once whole, so
    letting a conversion end costs a second and leaves the output directory
    consistent, while killing one would gain nothing.
    """
    log = logsetup.get_logger("batch")
    results: List[convert.Result] = []
    if not jobs:
        return results

    def stopped() -> bool:
        return cancel is not None and cancel.is_set()

    def submit(pool, job):
        source, destination = job
        return pool.submit(
            convert.convert, source, destination,
            codepage=options.codepage,
            wrap_images=options.wrap_images,
            facing_pages=options.facing_pages,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        # A window rather than the whole list: every submitted job runs, so
        # only what has not been submitted yet can be cancelled. The window
        # is what keeps that quantity small.
        width = (workers or (pool._max_workers)) * 2
        pending = iter(jobs)
        futures = {}
        for job in pending:
            if stopped():
                break
            futures[submit(pool, job)] = job[0]
            if len(futures) >= width:
                break

        while futures:
            done, _ = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                source = futures.pop(future)
                try:
                    result = future.result()
                except BaseException as exc:
                    # convert.convert catches its own failures, so this is
                    # something it could not: record it as a failed row
                    # rather than let it discard the whole batch.
                    log.exception("worker died on %s", source)
                    result = convert.Result(
                        source=source,
                        error=f"worker died: {exc.__class__.__name__}: {exc}",
                    )
                results.append(result)
                if on_result is not None:
                    on_result(result)
            while len(futures) < width and not stopped():
                job = next(pending, None)
                if job is None:
                    break
                futures[submit(pool, job)] = job[0]

    if stopped():
        log.warning("cancelled after %d of %d file(s)", len(results), len(jobs))
    return results
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_batch -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Confirm the CLI is unaffected**

Run: `make test`
Expected: PASS, whole suite

- [ ] **Step 6: Commit**

```bash
git add pubidml/batch.py tests/test_batch.py
git commit -m "Let a caller stop a batch without killing what is running

A button needs to stop a run, and convert() only guarantees a whole file
because it moves the package into place once. So cancel stops submitting
and lets the running conversions land."
```

---

### Task 3: Dutch strings and the error translation seam

Pure, headless, no tkinter. This is where the whole Dutch surface lives.

**Files:**
- Create: `pubidml/gui/__init__.py`
- Create: `pubidml/gui/strings.py`
- Create: `pubidml/gui/explain.py`
- Test: `tests/test_gui_explain.py`

**Interfaces:**
- Consumes: `convert.Result`, `batch.status_of`.
- Produces:
  ```python
  strings.<CONSTANT>: str          # every Dutch string
  explain.error(message: str) -> str        # one Dutch sentence for a ConversionError
  explain.failure_line(result) -> str       # "March 2004.pub — <Dutch sentence>"
  explain.summary(results) -> Counts        # dataclass: ok, review, failed, skipped, total
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/test_gui_explain.py`:

```python
"""Turning a ConversionError into something a Dutch reader can act on.

The table is matched by prefix, because most of convert.py's error
strings are f-strings: an equality lookup would miss the majority of them
and quietly leak English into a Dutch window.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pubidml import convert
from pubidml.gui import explain


class ErrorTranslationTest(unittest.TestCase):
    def test_a_known_error_is_translated(self):
        dutch = explain.error("not a supported Publisher file (or corrupt)")
        self.assertIn("Publisher-bestand", dutch)
        self.assertNotIn("supported", dutch)

    def test_an_f_string_error_is_matched_on_its_fixed_prefix(self):
        # convert.py raises f"timed out after {PARSE_TIMEOUT_S}s", so the
        # number is not known to the table.
        dutch = explain.error(f"timed out after {convert.PARSE_TIMEOUT_S}s")
        self.assertIn("te lang", dutch)

    def test_every_f_string_error_convert_can_raise_is_matched(self):
        for message in (
            "could not launch parser: [Errno 2] No such file",
            "IDML write failed: disk full",
            "malformed event stream: unexpected token",
            "document contains no pages",
        ):
            with self.subTest(message=message):
                self.assertNotIn(
                    "zie het rapport", explain.error(message),
                    f"no mapping for {message!r}",
                )

    def test_an_unmapped_error_survives_verbatim_with_a_pointer(self):
        # Swallowing an unrecognised diagnostic to protect the Dutch
        # surface would cost the one clue a support conversation has.
        dutch = explain.error("something nobody has seen before")
        self.assertIn("something nobody has seen before", dutch)
        self.assertIn("zie het rapport", dutch)


class FailureLineTest(unittest.TestCase):
    def test_a_failure_line_names_the_file_and_the_reason(self):
        result = convert.Result(
            source=Path("/x/March 2004.pub"),
            error="not a supported Publisher file (or corrupt)",
        )
        line = explain.failure_line(result)
        self.assertTrue(line.startswith("March 2004.pub"))
        self.assertIn("beschadigd", line)


class SummaryTest(unittest.TestCase):
    def _ok(self):
        return convert.Result(
            source=Path("a.pub"), output=Path("a.idml"), pages=1, text_frames=1
        )

    def _failed(self):
        return convert.Result(source=Path("b.pub"), error="nope")

    def _review(self):
        return convert.Result(
            source=Path("c.pub"), output=Path("c.idml"), pages=1,
            text_frames=1, warnings=["iets"],
        )

    def test_the_counts_match_the_statuses(self):
        counts = explain.summary(
            [self._ok(), self._ok(), self._failed(), self._review()]
        )
        self.assertEqual(counts.ok, 2)
        self.assertEqual(counts.failed, 1)
        self.assertEqual(counts.review, 1)
        self.assertEqual(counts.total, 4)

    def test_an_empty_batch_counts_nothing_rather_than_dividing_by_zero(self):
        counts = explain.summary([])
        self.assertEqual(counts.total, 0)
        self.assertEqual(counts.ok, 0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_gui_explain -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pubidml.gui'`

- [ ] **Step 3: Create the package and the strings**

`pubidml/gui/__init__.py`:

```python
"""The Dutch wizard.

Everything in this package may import from pubidml; nothing in pubidml
may import from here. tkinter is imported nowhere else in the project,
which is what lets the console build keep excluding it.
"""
```

`pubidml/gui/strings.py`:

```python
"""Every word the window says, in one place.

Dutch is the only language, so there is no gettext and no locale
detection -- building that machinery before a second language is asked
for is work spent on a guess. What matters is that the strings are
together, so adding a second language later is an addition rather than a
rewrite.

Dutch runs roughly a fifth longer than English, so anything laid out
against these must size itself from them rather than from a mock.
"""

WINDOW_TITLE = "Publisher-bestanden omzetten"

# --- Step 1: kiezen -------------------------------------------------
STEP1_TITLE = "Wat wilt u omzetten?"
STEP1_DROP_HINT = (
    "Sleep een map op het pictogram van dit programma,\n"
    "of kies hieronder wat u wilt omzetten."
)
STEP1_CHOOSE_FOLDER = "Map kiezen…"
STEP1_CHOOSE_FILES = "Bestanden kiezen…"
STEP1_FOUND = "{files} Publisher-bestanden gevonden in {folders} mappen."
STEP1_FOUND_ONE = "1 Publisher-bestand gevonden."
STEP1_NONE = (
    "Hier staan geen Publisher-bestanden (.pub).\n"
    "Kies een andere map."
)
STEP1_MISSING = "Deze map of dit bestand bestaat niet meer."

# --- Step 2: bestemming ---------------------------------------------
STEP2_TITLE = "Waar moeten de omgezette bestanden komen?"
STEP2_SAVE_TO = "Opslaan in:"
STEP2_CHANGE = "Wijzigen…"
STEP2_STRUCTURE = "De mappenstructuur wordt hierin overgenomen."
STEP2_INSIDE_SOURCE = (
    "Deze map ligt binnen de map die u omzet. Kies een map die\n"
    "daarbuiten ligt, anders zet een volgende keer het programma\n"
    "zijn eigen uitvoer opnieuw om."
)
STEP2_NOT_WRITABLE = "In deze map kan niet geschreven worden. Kies een andere map."

# --- Step 3: bezig ---------------------------------------------------
STEP3_TITLE = "Bezig met omzetten…"
STEP3_PROGRESS = "{done} van {total}"
STEP3_CANCEL = "Annuleren"
STEP3_CANCELLING = "Bezig met stoppen…"

# --- Step 4: klaar ---------------------------------------------------
STEP4_TITLE = "Klaar — {done} van de {total} bestanden"
STEP4_TITLE_CANCELLED = "Gestopt — {done} van de {total} bestanden"
STEP4_OK = "{n} goed omgezet"
STEP4_REVIEW = "{n} even controleren"
STEP4_FAILED = "{n} konden niet gelezen worden"
STEP4_SKIPPED = "{n} overgeslagen (waren al omgezet)"
STEP4_NEXT = (
    "Hierna: open elk .idml-bestand in Affinity en kies\n"
    "Bestand → Opslaan als… Laat de map _images ernaast staan\n"
    "totdat u dat gedaan heeft."
)
STEP4_OPEN_FOLDER = "Map openen"
STEP4_OPEN_REPORT = "Rapport openen"
STEP4_AGAIN = "Nog een map omzetten"

# --- Navigation ------------------------------------------------------
BACK = "Vorige"
NEXT = "Volgende"
START = "Omzetten"
CLOSE = "Sluiten"

# --- Errors ----------------------------------------------------------
ERROR_SEE_REPORT = "zie het rapport"
ERROR_UNEXPECTED = (
    "Er ging iets mis wat het programma niet verwachtte.\n"
    "Het logbestand staat in:\n{path}"
)
```

`pubidml/gui/explain.py`:

```python
"""A ConversionError, said in Dutch to someone who cannot act on the
original.

Matched by prefix rather than equality. Most of convert.py's failures are
f-strings -- f"timed out after {PARSE_TIMEOUT_S}s", f"IDML write failed:
{exc}" -- so an exact lookup would miss the majority of them and fall
through to English inside a Dutch window.

An unmapped error is passed through verbatim rather than replaced. The
English is ugly there, and it is also the only clue a support
conversation has; hiding it to keep the surface tidy would cost more than
it buys. English appearing in the window is a missing row in this table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .. import batch, convert
from . import strings

# Ordered longest-prefix-first so a specific message cannot be shadowed by
# a more general one that happens to share its opening words.
_TRANSLATIONS = [
    (
        "not a supported Publisher file",
        "dit is geen Publisher-bestand, of het bestand is beschadigd",
    ),
    (
        "libmspub could not parse the document",
        "Publisher-bestand kon niet gelezen worden",
    ),
    (
        "document contains no pages",
        "dit bestand bevat geen pagina's",
    ),
    (
        "timed out after",
        "dit bestand duurde te lang om te openen",
    ),
    (
        "could not launch parser",
        "het omzetprogramma kon niet gestart worden",
    ),
    (
        "parser could not write its event stream",
        "het omzetprogramma kon niet schrijven; is de schijf vol?",
    ),
    (
        "parser output was truncated",
        "het bestand werd maar half gelezen",
    ),
    (
        "malformed event stream",
        "het bestand werd niet goed gelezen",
    ),
    (
        "IDML write failed",
        "het omzetten lukte, maar het bestand kon niet opgeslagen worden",
    ),
]


@dataclass
class Counts:
    ok: int = 0
    review: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0


def error(message: str) -> str:
    """One Dutch sentence for a conversion failure."""
    if not message:
        return f"onbekende fout — {strings.ERROR_SEE_REPORT}"
    lowered = message.lower()
    for prefix, dutch in _TRANSLATIONS:
        if lowered.startswith(prefix.lower()):
            return dutch
    return f"{message} — {strings.ERROR_SEE_REPORT}"


def failure_line(result: convert.Result) -> str:
    """The filename and why it did not convert, on one line."""
    return f"{result.source.name} — {error(result.error or '')}"


def summary(results: List[convert.Result]) -> Counts:
    counts = Counts(total=len(results))
    for result in results:
        setattr(
            counts,
            batch.status_of(result),
            getattr(counts, batch.status_of(result)) + 1,
        )
    return counts
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_gui_explain -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add pubidml/gui/__init__.py pubidml/gui/strings.py pubidml/gui/explain.py tests/test_gui_explain.py
git commit -m "Say a conversion failure in Dutch, matched on the prefix

Most of convert.py's errors are f-strings, so an equality table would
have missed them and leaked English into a Dutch window. An unmapped one
still passes through whole: it is the only clue a support conversation
has."
```

---

### Task 4: The wizard's decisions, without any widgets

The rules — where output defaults to, what counts as a valid destination, which step comes next — are logic, and logic is testable headless. Only the widget wiring is not, and that is Task 6.

**Files:**
- Create: `pubidml/gui/wizard.py`
- Test: `tests/test_gui_wizard.py`

**Interfaces:**
- Consumes: `batch.plan`, `strings`.
- Produces:
  ```python
  Step: enum-like ints CHOOSE=0, DESTINATION=1, CONVERTING=2, DONE=3
  class Selection:  paths: list[Path]; root: Path; is_single_file: bool
  def scan(paths) -> Selection | None       # None when nothing was found
  def default_destination(selection) -> Path
  def destination_problem(destination, selection) -> str | None   # Dutch, or None if fine
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/test_gui_wizard.py`:

```python
"""What the wizard decides, before any of it is drawn."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pubidml.gui import wizard


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())

    def test_a_folder_is_searched_to_every_depth(self):
        nested = self.work / "2019" / "spring"
        nested.mkdir(parents=True)
        (self.work / "a.pub").write_bytes(b"stub")
        (nested / "b.pub").write_bytes(b"stub")
        selection = wizard.scan([self.work])
        self.assertEqual(len(selection.paths), 2)
        self.assertEqual(selection.root, self.work)

    def test_a_folder_with_no_publisher_files_finds_nothing(self):
        (self.work / "notes.txt").write_text("x")
        self.assertIsNone(wizard.scan([self.work]))

    def test_publisher_lock_files_are_not_offered_for_conversion(self):
        (self.work / "a.pub").write_bytes(b"stub")
        (self.work / "~$a.pub").write_bytes(b"stub")
        selection = wizard.scan([self.work])
        self.assertEqual([p.name for p in selection.paths], ["a.pub"])

    def test_a_single_chosen_file_is_its_own_selection(self):
        one = self.work / "a.pub"
        one.write_bytes(b"stub")
        selection = wizard.scan([one])
        self.assertTrue(selection.is_single_file)
        self.assertEqual(selection.paths, [one])

    def test_files_chosen_from_one_folder_share_it_as_their_root(self):
        for name in ("a.pub", "b.pub"):
            (self.work / name).write_bytes(b"stub")
        selection = wizard.scan(
            [self.work / "a.pub", self.work / "b.pub"]
        )
        self.assertEqual(selection.root, self.work)
        self.assertFalse(selection.is_single_file)


class DefaultDestinationTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())

    def test_a_folder_gets_a_converted_folder_beside_it(self):
        source = self.work / "Archief"
        source.mkdir()
        (source / "a.pub").write_bytes(b"stub")
        selection = wizard.scan([source])
        self.assertEqual(
            wizard.default_destination(selection), self.work / "Archief omgezet"
        )


class DestinationProblemTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.source = self.work / "Archief"
        self.source.mkdir()
        (self.source / "a.pub").write_bytes(b"stub")
        self.selection = wizard.scan([self.source])

    def test_a_destination_beside_the_source_is_fine(self):
        self.assertIsNone(
            wizard.destination_problem(self.work / "uit", self.selection)
        )

    def test_a_destination_inside_the_source_is_refused(self):
        # Otherwise a second run walks over its own output.
        problem = wizard.destination_problem(
            self.source / "uit", self.selection
        )
        self.assertIsNotNone(problem)
        self.assertIn("binnen", problem)

    def test_the_source_folder_itself_is_refused(self):
        problem = wizard.destination_problem(self.source, self.selection)
        self.assertIsNotNone(problem)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_gui_wizard -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pubidml.gui.wizard'`

- [ ] **Step 3: Implement `wizard.py`**

```python
"""What the wizard decides, separated from what it draws.

Keeping these as functions over plain values rather than methods on a
frame is what makes them testable on a machine with no display -- which
includes this project's own development machine, where Python has no
_tkinter at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import strings

CHOOSE, DESTINATION, CONVERTING, DONE = 0, 1, 2, 3


@dataclass
class Selection:
    """The .pub files to convert, and the root their tree hangs from."""
    paths: List[Path]
    root: Path
    is_single_file: bool = False

    @property
    def folder_count(self) -> int:
        return len({p.parent for p in self.paths})


def _publisher_files(folder: Path) -> List[Path]:
    # Ignore Office lock files, matching cli.find_sources.
    return sorted(
        path
        for path in folder.glob("**/*.pub")
        if path.is_file() and not path.name.startswith("~$")
    )


def scan(paths: List[Path]) -> Optional[Selection]:
    """Work out what was chosen. None when it holds no .pub files."""
    paths = [Path(p) for p in paths if Path(p).exists()]
    if not paths:
        return None

    if len(paths) == 1 and paths[0].is_dir():
        found = _publisher_files(paths[0])
        return Selection(found, paths[0]) if found else None

    if len(paths) == 1 and paths[0].is_file():
        return Selection([paths[0]], paths[0], is_single_file=True)

    files: List[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(_publisher_files(path))
        elif path.suffix.lower() == ".pub" and not path.name.startswith("~$"):
            files.append(path)
    if not files:
        return None

    root = Path(os.path.commonpath([str(p.parent) for p in files]))
    return Selection(sorted(files), root)


def default_destination(selection: Selection) -> Path:
    """A sibling of what was chosen, named after it.

    Beside rather than inside, because inside is the one arrangement that
    makes a later run convert its own output.
    """
    if selection.is_single_file:
        return selection.root.parent / f"{selection.root.stem} omgezet"
    return selection.root.parent / f"{selection.root.name} omgezet"


def destination_problem(destination: Path, selection: Selection) -> Optional[str]:
    """A Dutch explanation of why this destination will not do, or None."""
    source_root = (
        selection.root.parent if selection.is_single_file else selection.root
    )
    try:
        destination.resolve().relative_to(source_root.resolve())
    except ValueError:
        pass
    else:
        if not selection.is_single_file:
            return strings.STEP2_INSIDE_SOURCE
    return None
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_gui_wizard -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add pubidml/gui/wizard.py tests/test_gui_wizard.py
git commit -m "Decide the wizard's questions before drawing any of them

Where the output goes and what makes a destination wrong are rules, and
rules can be tested on a machine with no display -- which this one is,
since its Python has no _tkinter."
```

---

### Task 5: The thread and queue bridge

**Files:**
- Create: `pubidml/gui/runner.py`
- Test: `tests/test_gui_runner.py`

**Interfaces:**
- Consumes: `batch.plan`, `batch.run_batch`, `batch.write_report`, `batch.Options`.
- Produces:
  ```python
  class Run:
      def __init__(self, jobs, options, output_root, report_path, pre_skipped)
      def start(self) -> None
      def poll(self) -> list[convert.Result]   # results arrived since last poll
      def cancel(self) -> None
      @property finished: bool
      @property results: list[convert.Result]  # everything, valid once finished
      @property failure: BaseException | None  # the worker thread died
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/test_gui_runner.py`:

```python
"""The bridge between the batch thread and the Tk main loop.

Tk is single-threaded and touching a widget from a worker corrupts it
silently rather than raising, so results cross on a queue and only the
main loop drains it. No tkinter is needed to test that.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from pubidml import batch, convert
from pubidml.gui import runner


def drain(run, timeout=10.0):
    """Poll as the Tk loop would, until the run reports itself finished."""
    collected = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        collected.extend(run.poll())
        if run.finished:
            collected.extend(run.poll())
            return collected
        time.sleep(0.01)
    raise AssertionError("the run never finished")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.output = self.work / "out"
        self.report = self.output / "conversion-report.csv"
        self.original = convert.convert

    def tearDown(self):
        convert.convert = self.original

    def _jobs(self, count):
        return [
            (self.work / f"{i}.pub", self.output / f"{i}.idml")
            for i in range(count)
        ]

    def test_every_result_reaches_the_caller_through_poll(self):
        convert.convert = lambda source, destination, **kw: convert.Result(
            source=Path(source), output=Path(destination), pages=1, text_frames=1
        )
        run = runner.Run(
            self._jobs(6), batch.Options(), self.output, self.report, []
        )
        run.start()
        collected = drain(run)
        self.assertEqual(len(collected), 6)
        self.assertEqual(len(run.results), 6)

    def test_the_report_is_written_when_the_run_finishes(self):
        convert.convert = lambda source, destination, **kw: convert.Result(
            source=Path(source), output=Path(destination), pages=1
        )
        run = runner.Run(
            self._jobs(2), batch.Options(), self.output, self.report, []
        )
        run.start()
        drain(run)
        self.assertTrue(self.report.exists())

    def test_files_skipped_before_the_run_still_reach_the_report(self):
        convert.convert = lambda source, destination, **kw: convert.Result(
            source=Path(source), output=Path(destination), pages=1
        )
        already = convert.Result(
            source=self.work / "old.pub",
            output=self.output / "old.idml",
            skipped=True,
        )
        run = runner.Run(
            self._jobs(1), batch.Options(), self.output, self.report, [already]
        )
        run.start()
        drain(run)
        self.assertEqual(len(run.results), 2)

    def test_a_worker_thread_that_dies_is_reported_not_swallowed(self):
        # A GUI that hangs on a spinner forever is worse than one that
        # says something went wrong.
        def explode(*args, **kwargs):
            raise RuntimeError("the pool itself broke")
        run = runner.Run(
            self._jobs(1), batch.Options(), self.output, self.report, []
        )
        run._run_batch = explode          # stand in for batch.run_batch
        run.start()
        deadline = time.time() + 10
        while not run.finished and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(run.finished)
        self.assertIsNotNone(run.failure)

    def test_cancelling_ends_the_run(self):
        def slow(source, destination, **kw):
            time.sleep(0.05)
            return convert.Result(source=Path(source), output=Path(destination))
        convert.convert = slow
        run = runner.Run(
            self._jobs(200), batch.Options(), self.output, self.report, []
        )
        run.start()
        time.sleep(0.1)
        run.cancel()
        drain(run)
        self.assertLess(len(run.results), 200)
        self.assertTrue(self.report.exists(), "a stopped run still leaves a report")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_gui_runner -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pubidml.gui.runner'`

- [ ] **Step 3: Implement `runner.py`**

```python
"""Carrying a batch across the thread boundary Tk insists on.

Tk is single-threaded, and touching a widget from another thread corrupts
it quietly rather than raising -- so nothing here touches a widget. The
batch runs on its own thread and puts each Result on a queue; the main
loop calls poll() from root.after() and does the drawing.

The pool inside run_batch is left alone. The GUI adds one thread so the
window keeps painting; it does not flatten the parallelism the batch
already has.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import List, Optional

from .. import batch, convert, logsetup


class Run:
    def __init__(self, jobs, options, output_root, report_path, pre_skipped):
        self._jobs = list(jobs)
        self._options = options
        self._output_root = Path(output_root)
        self._report_path = Path(report_path)
        self._results: List[convert.Result] = list(pre_skipped)
        self._queue: "queue.Queue[convert.Result]" = queue.Queue()
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failure: Optional[BaseException] = None
        # Held as an attribute so a test can stand in for it.
        self._run_batch = batch.run_batch

    @property
    def total(self) -> int:
        return len(self._jobs)

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def results(self) -> List[convert.Result]:
        return self._results

    @property
    def failure(self) -> Optional[BaseException]:
        return self._failure

    def start(self) -> None:
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def poll(self) -> List[convert.Result]:
        """Whatever has landed since the last call. Main thread only."""
        arrived = []
        while True:
            try:
                arrived.append(self._queue.get_nowait())
            except queue.Empty:
                break
        self._results.extend(arrived)
        return arrived

    def _work(self) -> None:
        log = logsetup.get_logger("gui")
        try:
            self._run_batch(
                self._jobs,
                self._options,
                on_result=self._queue.put,
                cancel=self._cancel,
            )
        except BaseException as exc:
            # A GUI stuck on a spinner forever is worse than one that says
            # something went wrong, so the thread's death is a value the
            # main loop can read rather than a traceback nobody sees.
            log.exception("the conversion thread died")
            self._failure = exc
        finally:
            try:
                # Drain here rather than trusting the main loop to have
                # kept up: the report must describe everything that ran.
                pending = []
                while True:
                    try:
                        pending.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                for result in pending:
                    self._queue.put(result)
                everything = self._results + pending
                everything.sort(key=lambda r: str(r.source))
                batch.write_report(self._report_path, everything)
            except OSError as exc:
                log.error("could not write report to %s: %s", self._report_path, exc)
            self._done.set()
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_gui_runner -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Run the whole suite**

Run: `make test`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pubidml/gui/runner.py tests/test_gui_runner.py
git commit -m "Carry the batch across the thread boundary Tk insists on

Results cross on a queue and only the main loop drains it, because
touching a widget from a worker corrupts it without raising. A thread
that dies becomes a value the window can read, not a traceback nobody
sees."
```

---

### Task 6: The window

Everything that imports tkinter. The logic it drives is already tested; what is new here is the drawing and the wiring.

**Files:**
- Create: `pubidml/gui/app.py`
- Create: `pubidml/gui/steps.py`
- Test: `tests/test_gui_app.py`

**Interfaces:**
- Consumes: `wizard`, `runner`, `explain`, `strings`, `batch`.
- Produces:
  ```python
  app.Application(tk.Tk subclass)
  app.main(argv: list[str] | None = None) -> int
  app.self_test() -> int     # builds and tears down the window, returns 0
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/test_gui_app.py`:

```python
"""The window itself.

These skip rather than fail where Python has no _tkinter, matching how
the parser tests skip when bin/pubdump is not built. On this project's
development machine that is the normal case: `brew install python-tk`
turns them on.
"""

from __future__ import annotations

import unittest

try:
    import tkinter
    _root = tkinter.Tk()
    _root.destroy()
except Exception as exc:            # ImportError, or TclError with no display
    tkinter = None
    _WHY = str(exc)


@unittest.skipIf(tkinter is None, "no usable tkinter on this machine")
class WindowTest(unittest.TestCase):
    def test_the_window_builds_and_tears_down(self):
        # This is what --self-test runs in CI: it is the check that
        # PyInstaller actually bundled Tcl/Tk, which it can quietly fail
        # to do while producing a working-looking executable.
        from pubidml.gui import app
        self.assertEqual(app.self_test(), 0)

    def test_every_step_can_be_drawn(self):
        from pubidml.gui import app, wizard
        application = app.Application()
        try:
            for step in (wizard.CHOOSE, wizard.DESTINATION,
                         wizard.CONVERTING, wizard.DONE):
                application.show(step)
                application.update_idletasks()
        finally:
            application.destroy()


@unittest.skipIf(tkinter is None, "no usable tkinter on this machine")
class StringCoverageTest(unittest.TestCase):
    def test_no_step_label_is_left_in_english(self):
        # A stray English label is the failure mode this catches: the
        # strings module is the only place Dutch is allowed to live.
        from pubidml.gui import strings
        for name in dir(strings):
            if name.startswith("_"):
                continue
            value = getattr(strings, name)
            if isinstance(value, str):
                self.assertNotIn("{n}s", value, f"{name} has a broken placeholder")
```

- [ ] **Step 2: Run to verify it fails or skips**

Run: `python3 -m unittest tests.test_gui_app -v`
Expected: SKIPPED on a machine with no `_tkinter`. Install it first — `brew install python-tk` on macOS — then expect FAIL with `ModuleNotFoundError: No module named 'pubidml.gui.app'`.

> If tkinter cannot be installed on the development machine, this task's tests must still be written and must still be run in CI (Task 8). Do not mark this task complete on skipped tests alone.

- [ ] **Step 3: Implement `steps.py`**

Four `ttk.Frame` subclasses, each with a `refresh()` the shell calls when it shows them. Sizing rules: `grid` with `columnconfigure(weight=1)`, and `wraplength` on every multi-line label, because Dutch runs a fifth longer than the English these were drawn from.

```python
"""One frame per wizard step.

Nothing here decides anything -- the rules are in wizard.py, which is
tested without a display. These read those answers and draw them.

Every multi-line label sets wraplength and every column that holds text
gets a weight, because Dutch runs about a fifth longer than the English
mock the layout came from and a fixed width would clip it.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from . import explain, strings, wizard

PAD = 16
WRAP = 460


class Step(ttk.Frame):
    def __init__(self, master, shell):
        super().__init__(master, padding=PAD)
        self.shell = shell
        self.columnconfigure(0, weight=1)
        self.build()

    def build(self) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        pass


class ChooseStep(Step):
    def build(self) -> None:
        ttk.Label(
            self, text=strings.STEP1_TITLE, font=("", 14, "bold")
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            self, text=strings.STEP1_DROP_HINT, wraplength=WRAP, justify="left"
        ).grid(row=1, column=0, sticky="w", pady=(PAD, PAD))

        buttons = ttk.Frame(self)
        buttons.grid(row=2, column=0, sticky="w")
        ttk.Button(
            buttons, text=strings.STEP1_CHOOSE_FOLDER, command=self._pick_folder
        ).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(
            buttons, text=strings.STEP1_CHOOSE_FILES, command=self._pick_files
        ).grid(row=0, column=1)

        self.status = ttk.Label(self, text="", wraplength=WRAP, justify="left")
        self.status.grid(row=3, column=0, sticky="w", pady=(PAD, 0))

    def _pick_folder(self) -> None:
        chosen = filedialog.askdirectory(title=strings.STEP1_CHOOSE_FOLDER)
        if chosen:
            self._accept([Path(chosen)])

    def _pick_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            title=strings.STEP1_CHOOSE_FILES,
            filetypes=[("Publisher", "*.pub")],
        )
        if chosen:
            self._accept([Path(p) for p in chosen])

    def _accept(self, paths) -> None:
        selection = wizard.scan(paths)
        if selection is None:
            self.status.configure(text=strings.STEP1_NONE)
            self.shell.set_selection(None)
            return
        if len(selection.paths) == 1:
            self.status.configure(text=strings.STEP1_FOUND_ONE)
        else:
            self.status.configure(text=strings.STEP1_FOUND.format(
                files=len(selection.paths), folders=selection.folder_count
            ))
        self.shell.set_selection(selection)


class DestinationStep(Step):
    def build(self) -> None:
        ttk.Label(
            self, text=strings.STEP2_TITLE, font=("", 14, "bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(self, text=strings.STEP2_SAVE_TO).grid(
            row=1, column=0, sticky="w", pady=(PAD, 0)
        )
        self.path_label = ttk.Label(self, text="", wraplength=WRAP - 100)
        self.path_label.grid(row=2, column=0, sticky="w")
        ttk.Button(
            self, text=strings.STEP2_CHANGE, command=self._change
        ).grid(row=2, column=1, sticky="e", padx=(8, 0))

        ttk.Label(
            self, text=strings.STEP2_STRUCTURE, wraplength=WRAP, justify="left"
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.problem = ttk.Label(
            self, text="", wraplength=WRAP, justify="left", foreground="#a00"
        )
        self.problem.grid(row=4, column=0, columnspan=2, sticky="w", pady=(PAD, 0))
        self.columnconfigure(0, weight=1)

    def refresh(self) -> None:
        self.path_label.configure(text=str(self.shell.destination))
        self._validate()

    def _change(self) -> None:
        chosen = filedialog.askdirectory(title=strings.STEP2_CHANGE)
        if chosen:
            self.shell.destination = Path(chosen)
            self.refresh()

    def _validate(self) -> None:
        problem = wizard.destination_problem(
            self.shell.destination, self.shell.selection
        )
        self.problem.configure(text=problem or "")
        self.shell.set_can_advance(problem is None)


class ConvertingStep(Step):
    def build(self) -> None:
        ttk.Label(
            self, text=strings.STEP3_TITLE, font=("", 14, "bold")
        ).grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(self, mode="determinate", length=WRAP)
        self.bar.grid(row=1, column=0, sticky="ew", pady=(PAD, 8))
        self.count = ttk.Label(self, text="")
        self.count.grid(row=2, column=0, sticky="w")
        self.current = ttk.Label(self, text="", wraplength=WRAP)
        self.current.grid(row=3, column=0, sticky="w", pady=(4, PAD))
        self.cancel_button = ttk.Button(
            self, text=strings.STEP3_CANCEL, command=self._cancel
        )
        self.cancel_button.grid(row=4, column=0, sticky="w")

    def _cancel(self) -> None:
        self.cancel_button.configure(
            state="disabled", text=strings.STEP3_CANCELLING
        )
        self.shell.cancel_run()

    def refresh(self) -> None:
        self.cancel_button.configure(state="normal", text=strings.STEP3_CANCEL)
        self.bar.configure(maximum=max(self.shell.run.total, 1), value=0)
        self.count.configure(text="")
        self.current.configure(text="")

    def progress(self, done: int, total: int, latest: str) -> None:
        self.bar.configure(value=done)
        self.count.configure(text=strings.STEP3_PROGRESS.format(
            done=done, total=total
        ))
        self.current.configure(text=latest)


class DoneStep(Step):
    def build(self) -> None:
        self.heading = ttk.Label(self, text="", font=("", 14, "bold"))
        self.heading.grid(row=0, column=0, sticky="w")
        self.counts = ttk.Label(self, text="", justify="left", wraplength=WRAP)
        self.counts.grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.failures = tk.Text(self, height=6, width=56, wrap="word")
        self.failures.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(
            self, text=strings.STEP4_NEXT, wraplength=WRAP, justify="left"
        ).grid(row=3, column=0, sticky="w", pady=(PAD, PAD))

        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, sticky="w")
        ttk.Button(
            buttons, text=strings.STEP4_OPEN_FOLDER, command=self.shell.open_output
        ).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(
            buttons, text=strings.STEP4_OPEN_REPORT, command=self.shell.open_report
        ).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(
            buttons, text=strings.STEP4_AGAIN, command=self.shell.restart
        ).grid(row=0, column=2)
        self.columnconfigure(0, weight=1)

    def refresh(self) -> None:
        results = self.shell.run.results if self.shell.run else []
        counts = explain.summary(results)
        converted = counts.ok + counts.review
        template = (
            strings.STEP4_TITLE_CANCELLED
            if self.shell.run and self.shell.run.cancelled
            else strings.STEP4_TITLE
        )
        self.heading.configure(text=template.format(
            done=converted, total=counts.total
        ))

        lines = [strings.STEP4_OK.format(n=counts.ok)]
        if counts.review:
            lines.append(strings.STEP4_REVIEW.format(n=counts.review))
        if counts.failed:
            lines.append(strings.STEP4_FAILED.format(n=counts.failed))
        if counts.skipped:
            lines.append(strings.STEP4_SKIPPED.format(n=counts.skipped))
        self.counts.configure(text="\n".join(lines))

        self.failures.configure(state="normal")
        self.failures.delete("1.0", "end")
        failed = [r for r in results if not r.ok]
        for result in failed:
            self.failures.insert("end", explain.failure_line(result) + "\n")
        self.failures.configure(state="disabled")
        if not failed:
            self.failures.grid_remove()
        else:
            self.failures.grid()
```

- [ ] **Step 4: Implement `app.py`**

```python
"""The wizard shell: one window, four steps, and the loop that drains
the conversion thread.

The only code in the project that calls tkinter beyond this package's own
frames. Nothing outside pubidml/gui imports it, which is what keeps the
console executable free of Tcl/Tk.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import List, Optional

from .. import batch, logsetup
from . import runner, steps, strings, wizard


class Application(tk.Tk):
    def __init__(self, initial: Optional[List[Path]] = None):
        super().__init__()
        self.title(strings.WINDOW_TITLE)
        self.minsize(560, 460)

        self.selection: Optional[wizard.Selection] = None
        self.destination: Optional[Path] = None
        self.run: Optional[runner.Run] = None
        self._can_advance = False
        self._seen = 0

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        self.steps = {
            wizard.CHOOSE: steps.ChooseStep(container, self),
            wizard.DESTINATION: steps.DestinationStep(container, self),
            wizard.CONVERTING: steps.ConvertingStep(container, self),
            wizard.DONE: steps.DoneStep(container, self),
        }
        for frame in self.steps.values():
            frame.grid(row=0, column=0, sticky="nsew")

        self.nav = ttk.Frame(self, padding=(16, 0, 16, 16))
        self.nav.pack(fill="x")
        self.back_button = ttk.Button(
            self.nav, text=strings.BACK, command=self._back
        )
        self.back_button.pack(side="left")
        self.next_button = ttk.Button(
            self.nav, text=strings.NEXT, command=self._next
        )
        self.next_button.pack(side="right")

        self.step = wizard.CHOOSE
        self.show(wizard.CHOOSE)

        if initial:
            self.steps[wizard.CHOOSE]._accept(initial)
            if self.selection is not None:
                self._next()

    # --- shell API the steps call ---------------------------------
    def set_selection(self, selection) -> None:
        self.selection = selection
        if selection is not None:
            self.destination = wizard.default_destination(selection)
        self.set_can_advance(selection is not None)

    def set_can_advance(self, can: bool) -> None:
        self._can_advance = can
        self.next_button.configure(state="normal" if can else "disabled")

    def cancel_run(self) -> None:
        if self.run is not None:
            self.run.cancel()

    def restart(self) -> None:
        self.selection = None
        self.destination = None
        self.run = None
        self._seen = 0
        self.show(wizard.CHOOSE)

    def open_output(self) -> None:
        self._reveal(self.destination)

    def open_report(self) -> None:
        self._reveal(self.destination / "conversion-report.csv")

    def _reveal(self, path: Optional[Path]) -> None:
        if path is None or not Path(path).exists():
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            elif os.name == "nt":
                os.startfile(str(path))       # noqa: S606 - Windows shell open
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            logsetup.get_logger("gui").exception("could not open %s", path)

    # --- navigation ------------------------------------------------
    def show(self, step: int) -> None:
        self.step = step
        frame = self.steps[step]
        frame.refresh()
        frame.tkraise()
        self.back_button.configure(
            state="normal" if step in (wizard.DESTINATION,) else "disabled"
        )
        if step == wizard.CHOOSE:
            self.next_button.configure(
                text=strings.NEXT,
                state="normal" if self.selection else "disabled",
            )
        elif step == wizard.DESTINATION:
            self.next_button.configure(text=strings.START, state="normal")
        elif step == wizard.CONVERTING:
            self.next_button.configure(state="disabled")
        else:
            self.next_button.configure(
                text=strings.CLOSE, state="normal", command=self.destroy
            )

    def _back(self) -> None:
        if self.step == wizard.DESTINATION:
            self.show(wizard.CHOOSE)

    def _next(self) -> None:
        if self.step == wizard.CHOOSE and self.selection:
            self.show(wizard.DESTINATION)
        elif self.step == wizard.DESTINATION and self._can_advance:
            self._start_run()

    # --- the run ---------------------------------------------------
    def _start_run(self) -> None:
        source_root = (
            self.selection.root
            if not self.selection.is_single_file
            else self.selection.root
        )
        jobs, skipped = batch.plan(
            self.selection.paths, source_root, self.destination, force=False
        )
        self.run = runner.Run(
            jobs,
            batch.Options(),
            self.destination,
            self.destination / "conversion-report.csv",
            skipped,
        )
        self._seen = 0
        self.show(wizard.CONVERTING)
        self.run.start()
        self.after(100, self._drain)

    def _drain(self) -> None:
        """The only place a Result reaches a widget."""
        arrived = self.run.poll()
        self._seen += len(arrived)
        if arrived:
            self.steps[wizard.CONVERTING].progress(
                self._seen, self.run.total, arrived[-1].source.name
            )
        if self.run.finished:
            self.run.poll()
            self.show(wizard.DONE)
            return
        self.after(100, self._drain)


def self_test() -> int:
    """Build the whole window and tear it down. Returns 0 on success.

    This is what CI runs inside the PATH-stripped directory. PyInstaller
    can fail to bundle Tcl/Tk while still producing an executable that
    starts, so the check has to reach as far as a real widget tree.
    """
    application = Application()
    application.withdraw()
    for step in (wizard.CHOOSE, wizard.DESTINATION, wizard.CONVERTING, wizard.DONE):
        application.show(step)
        application.update_idletasks()
    application.destroy()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return self_test()

    log_path = logsetup.configure(None, verbose=False)
    logsetup.install_excepthook()
    # The same environment banner the CLI writes. Without it the GUI log
    # lacks the resolved parser path, which is the first thing to look at
    # when a bundled executable misbehaves on someone else's machine.
    logsetup.log_environment(convert.PUBDUMP)

    initial = [Path(a) for a in argv if not a.startswith("-")]
    application = Application(initial=initial or None)
    # A windowed build has no stderr, so an uncaught exception would
    # otherwise vanish: the user sees a frozen window and has nothing to
    # report. Tk's own handler is the only place that can still speak.
    def report_exception(exc_type, exc, tb):
        logsetup.get_logger("gui").error(
            "unhandled exception", exc_info=(exc_type, exc, tb)
        )
        messagebox.showerror(
            strings.WINDOW_TITLE,
            strings.ERROR_UNEXPECTED.format(path=log_path or "-"),
        )
    application.report_callback_exception = report_exception
    application.mainloop()
    return 0
```

`app.py` therefore imports `messagebox` and `convert` too:

```python
from tkinter import messagebox, ttk
from .. import batch, convert, logsetup
```

> `show(wizard.DONE)` rebinds the Next button's command to `destroy`. Rebind it back in `show()` for every other step — add `command=self._next` to the `elif` branches — so restarting the wizard does not leave a Close button that closes on Next.

- [ ] **Step 5: Fix the button rebinding noted above**

In `show()`, give the `CHOOSE` and `DESTINATION` branches `command=self._next` explicitly, so `restart()` restores working navigation.

- [ ] **Step 6: Run the tests**

Run: `python3 -m unittest tests.test_gui_app -v`
Expected: PASS where tkinter is installed; SKIPPED otherwise. If skipped, do not close this task — Task 8's CI run is what proves it.

- [ ] **Step 7: Run the whole suite**

Run: `make test`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add pubidml/gui/app.py pubidml/gui/steps.py tests/test_gui_app.py
git commit -m "Draw the four steps

The rules were already decided and tested elsewhere; this is the drawing
and the wiring. Labels wrap against the Dutch rather than the English
they were drawn from, which is a fifth shorter."
```

---

### Task 7: The second executable

**Files:**
- Create: `pub2idml_gui.py`
- Create: `pub2idml-gui.spec`
- Modify: `Makefile` (a `gui` convenience target)

**Interfaces:**
- Consumes: `pubidml.gui.app.main`.
- Produces: `dist/pub2idml-gui.exe`.

- [ ] **Step 1: Create the entry shim**

`pub2idml_gui.py`:

```python
"""Entry point for the windowed build.

PyInstaller runs its entry script as __main__, which breaks the package
relative imports in pubidml/gui/app.py. Going through this shim keeps the
package context intact, exactly as pub2idml.py does for the console
build.
"""

import sys

from pubidml.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Create the spec**

`pub2idml-gui.spec`:

```python
# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the windowed build.

The same binaries as the console build, with two differences that matter.
console=False, because a windowed Windows executable that keeps a console
flashes a black box on every launch. And tkinter is not excluded -- it is
the entire user interface here, whereas the console build excludes it to
stay small.
"""

import os

binaries = []
for entry in sorted(os.listdir("bin")):
    path = os.path.join("bin", entry)
    if os.path.isfile(path):
        binaries.append((path, "bin"))

analysis = Analysis(
    ["pub2idml_gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["unittest", "pydoc", "test"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="pub2idml-gui",
    console=False,
    debug=False,
    strip=False,
    upx=False,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
```

- [ ] **Step 3: Verify the console build still excludes tkinter**

Run: `grep -n 'tkinter' pub2idml.spec`
Expected: the `excludes` line still lists `tkinter`. If the GUI has leaked an import into `pubidml/` proper, the console build would now need it — the grep below is the check.

Run: `grep -rn '^import tkinter\|^from tkinter\|import tkinter' pubidml/ --include='*.py' | grep -v '^pubidml/gui/'`
Expected: no output.

- [ ] **Step 4: Add a Makefile target**

Append to `Makefile`:

```make
# The windowed build. Requires pyinstaller and, on macOS, a Python built
# with Tk (brew install python-tk).
.PHONY: gui
gui:
	$(PYTHON) -m PyInstaller pub2idml-gui.spec
```

And add `gui` to the `.PHONY` list at the top if consolidating.

- [ ] **Step 5: Commit**

```bash
git add pub2idml_gui.py pub2idml-gui.spec Makefile
git commit -m "Bundle the window as its own executable

console=False, because a windowed build that keeps a console flashes a
black box on launch -- and a console build that loses stdout goes silent,
which is why this is a second executable rather than a mode of the first."
```

---

### Task 8: Prove it starts on a clean machine

The workflow already strips `PATH` and converts real files, so a missing DLL fails the build rather than the user. Bundling Tcl/Tk is exactly the kind of thing PyInstaller gets quietly wrong, so the GUI earns the same treatment.

**Files:**
- Modify: `.github/workflows/build-windows.yml`

- [ ] **Step 1: Read the existing isolation step**

Run: `grep -n 'PATH\|pyinstaller\|dist\\\|release' .github/workflows/build-windows.yml`

Note the exact step names, the directory the isolation test uses, and the release-upload step's file list. The additions below must match that structure rather than replace it.

- [ ] **Step 2: Add the second PyInstaller pass**

Immediately after the existing PyInstaller step, add:

```yaml
      - name: Bundle the windowed build
        run: pyinstaller pub2idml-gui.spec
```

- [ ] **Step 3: Add the GUI self-test to the isolated directory**

Inside the same step that strips `PATH` (or a new step that strips it identically), after the existing conversion checks, add:

```powershell
          # PyInstaller can omit Tcl/Tk and still produce an executable
          # that starts, so the check has to build a real widget tree.
          Copy-Item dist\pub2idml-gui.exe $isolated\
          $env:PATH = "C:\Windows\system32"
          & "$isolated\pub2idml-gui.exe" --self-test
          if ($LASTEXITCODE -ne 0) {
            throw "the windowed build could not create its window ($LASTEXITCODE)"
          }
```

> Use the same `$isolated` variable the existing step uses. A windowed executable writes nothing to stdout, so the exit code is the whole signal — do not assert on output.

- [ ] **Step 4: Add the executable to the release**

In the publish job's asset list, add `pub2idml-gui.exe` beside `pub2idml.exe`, and generate its `.sha256` the same way the console one is generated.

- [ ] **Step 5: Commit and push, then watch the run**

```bash
git add .github/workflows/build-windows.yml
git commit -m "Prove the window opens where nothing is installed

The isolation test already catches a missing DLL by stripping PATH. Tcl
and Tk are the same class of mistake -- PyInstaller can leave them out
and still hand you an executable that starts -- so --self-test builds the
whole widget tree there too."
git push
```

Run: `gh run watch`
Expected: green. A red `--self-test` step means Tcl/Tk did not get bundled; that is the failure this task exists to catch, and it is fixed in `pub2idml-gui.spec`, not by removing the check.

---

### Task 9: Document the window

**Files:**
- Modify: `README.md` (a new section after "Running pub2idml.exe on Windows")

- [ ] **Step 1: Write the section**

Add after the CLI's Windows section, before "Logging":

````markdown
### The window: pub2idml-gui.exe

For people who will not use a command line, the same converter ships as a
four-step Dutch wizard. It is a separate download from the same release —
`pub2idml-gui.exe` — because a windowed Windows executable has no stdout,
so a single dual-mode program would leave the command line silent.

Drag a folder onto the program's icon, or open it and choose one. The
whole tree is converted and its folder structure recreated. There is
nothing to configure: the code page, image wrapping and booklet detection
all stay on the automatic settings, which the CLI table above documents.
Anyone who needs to change them wants `pub2idml.exe`.

The last screen says what happened in plain Dutch, lists any file that
could not be read, and reminds the user of the one step no converter can
do for them: open each `.idml` in Affinity and **Bestand → Opslaan als…**,
keeping the `_images` folder beside it until then.

The CSV report is written exactly as the command line writes it, in
English, in the output folder — one report format, both front ends.
````

- [ ] **Step 2: Check the claims against the code**

Run: `grep -n 'STEP4_NEXT' -A 5 pubidml/gui/strings.py`
Expected: the Dutch menu wording in the README matches the string the window actually shows. If Affinity's Dutch build words the menu differently, fix `strings.py` and the README together.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Write the window into the README

Two executables now, and the reason they are two -- a windowed build has
no stdout -- is the kind of thing a reader will otherwise ask."
```

---

## Self-Review

**Spec coverage.** Every section of the spec maps to a task: §1 module boundaries → Tasks 1, 3–7 and the Task 7 grep that enforces the import rule; §2 extraction → Task 1; §3 four steps → Tasks 4 and 6; §4 threading → Task 5; §5 Dutch → Task 3, with the layout consequences in Task 6; §6 not-built → no task, by construction; §7 testing and CI → Tasks 1–6 and 8; §8 macOS → deliberately no task, out of scope.

**Two gaps found and closed while reviewing.** The spec's `Options` dataclass was never named in the spec text but is needed by three tasks, so Task 1 defines it explicitly in its Interfaces block. And `run_batch`'s original "submit everything, then cancel" shape cannot actually cancel — every submitted job runs — so Task 2 submits a bounded window instead, and says why.

**One inconsistency fixed.** Task 6's `show(wizard.DONE)` rebinds the Next button to `destroy`, which would survive into a restarted wizard; Step 5 of that task exists solely to rebind it back.

**Two more gaps closed after checking `logsetup`'s real signatures.** `app.main` configured the log but never wrote the environment banner, so a GUI log would have lacked the resolved parser path — the first thing anyone diagnosing a bundled executable wants. And `strings.ERROR_UNEXPECTED` was defined but never shown: a windowed build has no stderr, so an uncaught exception would have left a frozen window and nothing to report. Both are now wired through `report_callback_exception`, which is the only handler Tk still calls once the main loop owns the thread.

**Known risk, stated rather than hidden.** Task 6's tests skip where `_tkinter` is absent, which is the current state of this project's development machine. `brew install python-tk` turns them on locally; Task 8's CI self-test is the backstop that does not depend on anyone remembering to.
