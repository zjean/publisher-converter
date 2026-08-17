"""Batch command line interface.

Walks a source tree of .pub files, converts each to IDML in parallel and
mirrors the source folder structure into the output directory. Every
conversion is recorded in a CSV report so a large collection can be
triaged rather than inspected file by file.
"""

from __future__ import annotations

import argparse
import codecs
import concurrent.futures
import csv
import sys
from pathlib import Path
from typing import List

from . import convert, logsetup

REPORT_COLUMNS = [
    "source",
    "output",
    "status",
    "pages",
    "text_frames",
    "images",
    "shapes",
    "characters",
    "fonts",
    "warnings",
    "error",
]


def find_sources(root: Path, recursive: bool) -> List[Path]:
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


def _force_utf8_console() -> None:
    """Print non-ASCII filenames without dying on a redirected stream.

    On Windows a redirected stdout uses the locale encoding (usually
    cp1252), so printing a Cyrillic or Greek filename — exactly the case
    the code-page repair exists to serve — raises UnicodeEncodeError in the
    middle of a batch. `pub2idml.exe archive -o out > run.txt` is an
    entirely ordinary thing to type.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            # Not a reconfigurable text stream (or absent in a windowed
            # build); printing is best-effort from here.
            pass


def _status(result: convert.Result) -> str:
    if not result.ok:
        return "failed"
    if result.skipped:
        return "skipped"
    if result.needs_review:
        return "review"
    return "ok"


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pub2idml",
        description="Convert Microsoft Publisher files to IDML for Affinity.",
    )
    parser.add_argument("source", type=Path, help="a .pub file or a folder of them")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("converted"),
        help="output folder (default: ./converted)",
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=0,
        help="parallel conversions (default: one per CPU core)",
    )
    parser.add_argument(
        "--no-recursive", action="store_true",
        help="do not descend into subfolders",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="reconvert files whose output already exists",
    )
    parser.add_argument(
        "--report", type=Path, default=None,
        help="CSV report path (default: <output>/conversion-report.csv)",
    )
    parser.add_argument(
        "--codepage", default="auto",
        help=(
            "how to handle libmspub's code page bug: 'auto' detects and "
            "repairs non-Latin text (default), 'none' disables repair, or "
            "give an explicit codec such as cp1251 or cp932 to force it"
        ),
    )
    parser.add_argument(
        "--no-image-wrap", action="store_true",
        help=(
            "do not make text flow around images, or around the headlines "
            "the file says it flowed around; both will overlap and hide "
            "text, but placement matches the source exactly"
        ),
    )
    parser.add_argument(
        "--facing-pages", action="store_true",
        help=(
            "lay the pages out as reader's spreads (1 | 2-3 | 4-5) rather "
            "than singly; libmspub does not report whether the publication "
            "was set up facing, so a booklet has to say so here"
        ),
    )
    parser.add_argument(
        "--log-file", type=Path, default=None,
        help=(
            "where to write the diagnostic log "
            f"(default: a timestamped file in {logsetup.default_log_dir()})"
        ),
    )
    parser.add_argument(
        "--no-log", action="store_true",
        help="do not write a log file",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="record debug-level detail in the log",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args(argv)
    _force_utf8_console()

    # Validate before anything expensive starts: an unknown codec is caught
    # per span deep inside the repair and the text silently left as it was,
    # so a typo would otherwise surface only as still-broken text in the
    # output, after the whole batch had run.
    codepage = None if args.codepage.lower() == "none" else args.codepage
    if codepage is not None and codepage.lower() != "auto":
        try:
            codecs.lookup(codepage)
        except LookupError:
            parser.error(f"unknown codec for --codepage: {args.codepage}")

    log_path = None
    if args.no_log:
        logsetup.disable()
    else:
        log_path = logsetup.configure(args.log_file, args.verbose)
        logsetup.install_excepthook()
        logsetup.log_environment(convert.PUBDUMP)
    log = logsetup.get_logger("cli")

    if not args.source.exists():
        log.error("source not found: %s", args.source)
        parser.error(f"source not found: {args.source}")

    sources = find_sources(args.source, not args.no_recursive)
    log.info("found %d .pub file(s) under %s", len(sources), args.source)
    if not sources:
        print(f"No .pub files found under {args.source}", file=sys.stderr)
        return 1

    output_root = args.output
    report_path = args.report or (output_root / "conversion-report.csv")

    jobs = []
    # Skipped files are carried as results, not just counted, so that the
    # report keeps describing the whole tree that was walked rather than
    # only the part this particular run happened to redo.
    results: List[convert.Result] = []
    for source in sources:
        destination = destination_for(source, args.source, output_root)
        if destination.exists() and not args.force:
            results.append(convert.Result(
                source=source, output=destination, skipped=True
            ))
            continue
        jobs.append((source, destination))

    skipped = len(results)
    interrupted = False
    try:
        if jobs:
            workers = args.jobs if args.jobs > 0 else None
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        convert.convert, source, destination,
                        codepage=codepage, wrap_images=not args.no_image_wrap,
                        facing_pages=args.facing_pages,
                    ): source
                    for source, destination in jobs
                }
                for future in concurrent.futures.as_completed(futures):
                    source = futures[future]
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
                    if not args.quiet:
                        _print_result(result)
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
            _write_report(report_path, results)
            log.info("report written to %s", report_path)
        except OSError as exc:
            log.error("could not write report to %s: %s", report_path, exc)
            print(f"Could not write report to {report_path}: {exc}", file=sys.stderr)

    ok = sum(1 for r in results if _status(r) == "ok")
    review = sum(1 for r in results if _status(r) == "review")
    failed = sum(1 for r in results if _status(r) == "failed")

    print()
    print(f"Converted {ok + review}/{len(jobs)} file(s) into {output_root}")
    if review:
        print(f"  {review} need review (see report)")
    if failed:
        print(f"  {failed} failed")
    if skipped:
        print(f"  {skipped} skipped (already converted; use --force to redo)")
    if interrupted:
        attempted = len(results) - skipped
        print(f"  interrupted: {len(jobs) - attempted} file(s) not attempted")
    print(f"Report: {report_path}")
    if log_path:
        print(f"Log:    {log_path}")

    log.info(
        "finished: %d ok, %d review, %d failed, %d skipped",
        ok, review, failed, skipped,
    )
    for result in results:
        if not result.ok:
            log.error("FAILED %s: %s", result.source, result.error)

    return 1 if (failed or interrupted) else 0


def _print_result(result: convert.Result) -> None:
    status = _status(result)
    marker = {
        "ok": "  ok  ", "review": "review", "failed": "FAILED", "skipped": " skip ",
    }[status]
    name = result.source.name
    if status == "failed":
        print(f"[{marker}] {name}: {result.error}")
        return
    detail = (
        f"{result.pages}p {result.text_frames} frames "
        f"{result.images} images {result.characters} chars"
    )
    print(f"[{marker}] {name}: {detail}")
    for warning in result.warnings:
        print(f"           ! {warning}")


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


def _write_report(path: Path, results: List[convert.Result]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORT_COLUMNS)
        for result in results:
            writer.writerow(
                [
                    _csv_safe(str(result.source)),
                    _csv_safe(str(result.output) if result.output else ""),
                    _status(result),
                    result.pages,
                    result.text_frames,
                    result.images,
                    result.shapes,
                    result.characters,
                    _csv_safe("; ".join(result.fonts)),
                    _csv_safe("; ".join(result.warnings)),
                    _csv_safe(result.error or ""),
                ]
            )


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
