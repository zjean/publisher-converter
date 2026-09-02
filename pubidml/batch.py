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


def find_sources(root: Path, recursive: bool = True) -> List[Path]:
    if root.is_file():
        return [root]
    pattern = "**/*.pub" if recursive else "*.pub"
    # Publisher templates use .pubz/.pubx variants; ignore Office lock files.
    return sorted(
        path
        for path in root.glob(pattern)
        if path.is_file() and not path.name.startswith("~$")
    )


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
