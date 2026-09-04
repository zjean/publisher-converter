"""Batch command line interface.

Walks a source tree of .pub files, converts each to IDML in parallel and
mirrors the source folder structure into the output directory. Every
conversion is recorded in a CSV report so a large collection can be
triaged rather than inspected file by file.
"""

from __future__ import annotations

import argparse
import codecs
import sys
from pathlib import Path
from typing import List

from . import batch, convert, logsetup


# A thin delegation: batch.py owns the glob and the lock-file filter, but
# run() below still calls this by its own name.
def find_sources(root: Path, recursive: bool) -> List[Path]:
    return batch.find_sources(root, recursive)


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
        help="parallel conversions (default: the CPU count plus four, "
             "capped at 32)",
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
    # Three states, so neither flag can be a plain store_true: the default
    # is to read the layout off the file, and both answers have to be
    # sayable over that.
    parser.add_argument(
        "--facing-pages", action="store_true",
        help=(
            "lay the pages out as reader's spreads (1 | 2-3 | 4-5) rather "
            "than singly. Without either flag this is read from the file, "
            "which describes a booklet by the print sheet it states and its "
            "page count; pass this where the file does not say so"
        ),
    )
    parser.add_argument(
        "--no-facing-pages", action="store_true",
        help=(
            "lay every page out singly, overriding what the file says. Use "
            "it for a document read as a booklet that is not one"
        ),
    )
    parser.add_argument(
        "--bleed", type=float, default=convert.DEFAULT_BLEED_MM, metavar="MM",
        help=(
            "document bleed in millimetres on all four edges "
            f"(default: {convert.DEFAULT_BLEED_MM:g}). Publisher states no "
            "bleed of its own -- it has only a fixed print option -- so "
            "this is a setting rather than a reading; pass 0 for a "
            "document set up without bleed"
        ),
    )
    parser.add_argument(
        "--no-page-snap", action="store_true",
        help=(
            "keep the page size the file states. Without it, a page within "
            "a millimetre of a standard size is set up as that size -- the "
            "newsletters state 148.53 x 209.89mm and are A5 -- with every "
            "item left where it is, so the difference falls at the right "
            "and bottom trim"
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

    # None means read the layout off the file. Asking for both answers at
    # once is a mistake worth refusing rather than resolving: whichever one
    # won, half of what was typed would be silently ignored.
    if args.facing_pages and args.no_facing_pages:
        parser.error("--facing-pages and --no-facing-pages contradict each other")

    # A negative bleed is a page trimmed inside its own edges, which no
    # reader means and IDML has no way to express.
    if args.bleed < 0:
        parser.error("--bleed cannot be negative")
    facing_pages = True if args.facing_pages else False if args.no_facing_pages else None

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

    options = batch.Options(
        codepage=codepage,
        wrap_images=not args.no_image_wrap,
        facing_pages=facing_pages,
        bleed=args.bleed,
        snap_page=not args.no_page_snap,
    )
    jobs, results = batch.plan(sources, args.source, output_root, args.force)
    skipped = len(results)
    interrupted = False

    # Collected here as each result lands rather than from run_batch's
    # return value: a Ctrl-C reaching the main thread mid-batch never lets
    # run_batch return, so `results += run_batch(...)` would discard every
    # row it had already accumulated and the report below would describe
    # an interrupted run as having converted nothing.
    def announce(result):
        results.append(result)
        if not args.quiet:
            _print_result(result)

    try:
        batch.run_batch(
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

    ok = sum(1 for r in results if batch.status_of(r) == "ok")
    review = sum(1 for r in results if batch.status_of(r) == "review")
    failed = sum(1 for r in results if batch.status_of(r) == "failed")

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
    status = batch.status_of(result)
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
    if result.wordart:
        detail += f" {result.wordart} wordart"
    if result.facing_pages:
        detail += " (facing, detected)" if result.facing_detected else " (facing)"
    print(f"[{marker}] {name}: {detail}")
    for warning in result.warnings:
        print(f"           ! {warning}")


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
