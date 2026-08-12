"""Batch command line interface.

Walks a source tree of .pub files, converts each to IDML in parallel and
mirrors the source folder structure into the output directory. Every
conversion is recorded in a CSV report so a large collection can be
triaged rather than inspected file by file.
"""

from __future__ import annotations

import argparse
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


def _status(result: convert.Result) -> str:
    if not result.ok:
        return "failed"
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
            "do not make text flow around images; images will overlap and "
            "hide text, but placement matches the source exactly"
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

    log_path = None
    if not args.no_log:
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
    skipped = 0
    for source in sources:
        destination = destination_for(source, args.source, output_root)
        if destination.exists() and not args.force:
            skipped += 1
            continue
        jobs.append((source, destination))

    codepage = None if args.codepage.lower() == "none" else args.codepage

    results: List[convert.Result] = []
    if jobs:
        workers = args.jobs if args.jobs > 0 else None
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    convert.convert, source, destination,
                    codepage=codepage, wrap_images=not args.no_image_wrap,
                ): source
                for source, destination in jobs
            }
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                if not args.quiet:
                    _print_result(result)

    results.sort(key=lambda r: str(r.source))
    _write_report(report_path, results)
    log.info("report written to %s", report_path)

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

    return 1 if failed else 0


def _print_result(result: convert.Result) -> None:
    status = _status(result)
    marker = {"ok": "  ok  ", "review": "review", "failed": "FAILED"}[status]
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


def _write_report(path: Path, results: List[convert.Result]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORT_COLUMNS)
        for result in results:
            writer.writerow(
                [
                    str(result.source),
                    str(result.output) if result.output else "",
                    _status(result),
                    result.pages,
                    result.text_frames,
                    result.images,
                    result.shapes,
                    result.characters,
                    "; ".join(result.fonts),
                    "; ".join(result.warnings),
                    result.error or "",
                ]
            )


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
