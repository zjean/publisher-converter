"""Batch driver.

The CSV report is what makes a collection triageable, so the invariant
under test is that it survives whatever the batch does — including a
worker dying in a way convert() could not catch.
"""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pubidml import cli, convert


def read_report(path: Path) -> list:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class ReportSurvivalTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.source_dir = self.work / "in"
        self.source_dir.mkdir()
        for name in ("a.pub", "b.pub", "c.pub"):
            (self.source_dir / name).write_bytes(b"stub")
        self.output = self.work / "out"

    def run_cli(self, *extra: str) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.run(
                [str(self.source_dir), "-o", str(self.output), "--no-log", *extra]
            )
        return code, out.getvalue()

    def test_a_worker_that_dies_does_not_destroy_the_batch(self):
        # convert() has its own catch-all, so this simulates the case it
        # cannot cover: the future itself raising.
        calls = {"n": 0}
        original = convert.convert

        def flaky(source, destination, **kwargs):
            calls["n"] += 1
            if Path(source).name == "b.pub":
                raise MemoryError("out of memory")
            return convert.Result(
                source=Path(source), output=Path(destination), pages=1, text_frames=1
            )

        cli.convert.convert = flaky
        try:
            code, _ = self.run_cli("-j", "1")
        finally:
            cli.convert.convert = original

        self.assertEqual(calls["n"], 3, "every file must still be attempted")
        rows = read_report(self.output / "conversion-report.csv")
        self.assertEqual(len(rows), 3, "the report must list all three files")
        by_name = {Path(r["source"]).name: r for r in rows}
        self.assertEqual(by_name["b.pub"]["status"], "failed")
        self.assertIn("MemoryError", by_name["b.pub"]["error"])
        self.assertEqual(by_name["a.pub"]["status"], "ok")
        self.assertEqual(by_name["c.pub"]["status"], "ok")
        self.assertEqual(code, 1)

    def test_the_report_is_written_even_when_nothing_converts(self):
        original = convert.convert
        cli.convert.convert = lambda source, destination, **kw: convert.Result(
            source=Path(source), error="nope"
        )
        try:
            code, _ = self.run_cli()
        finally:
            cli.convert.convert = original
        rows = read_report(self.output / "conversion-report.csv")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["status"] == "failed" for r in rows))
        self.assertEqual(code, 1)


class SkippedFileTest(unittest.TestCase):
    """A second run must not empty the report it wrote the first time.

    Skipping a file that is already converted is right; leaving it out of
    the report is not, because the report is written with "w" on every run.
    A complete collection converted twice used to end up described by a
    header row and nothing else.
    """

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.source_dir = self.work / "in"
        self.source_dir.mkdir()
        for name in ("a.pub", "b.pub", "c.pub"):
            (self.source_dir / name).write_bytes(b"stub")
        self.output = self.work / "out"

    def run_cli(self, *extra: str) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.run(
                [str(self.source_dir), "-o", str(self.output), "--no-log", *extra]
            )

    def _convert_for_real(self):
        def stub(source, destination, **kwargs):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"idml")
            return convert.Result(
                source=Path(source), output=Path(destination), pages=1, text_frames=1
            )
        return stub

    def test_a_second_run_still_describes_every_file(self):
        original = convert.convert
        cli.convert.convert = self._convert_for_real()
        try:
            self.run_cli()
            rows_first = read_report(self.output / "conversion-report.csv")
            self.run_cli()
            rows_second = read_report(self.output / "conversion-report.csv")
        finally:
            cli.convert.convert = original

        self.assertEqual(len(rows_first), 3)
        self.assertEqual(len(rows_second), 3, "the re-run emptied the report")
        self.assertEqual(
            {r["status"] for r in rows_second}, {"skipped"}
        )

    def test_a_skipped_row_still_names_the_output_it_stands_for(self):
        original = convert.convert
        cli.convert.convert = self._convert_for_real()
        try:
            self.run_cli()
            self.run_cli()
        finally:
            cli.convert.convert = original
        rows = read_report(self.output / "conversion-report.csv")
        for row in rows:
            self.assertTrue(row["output"].endswith(".idml"))

    def test_a_skipped_file_is_not_counted_as_converted(self):
        original = convert.convert
        cli.convert.convert = self._convert_for_real()
        try:
            self.run_cli()
            code = self.run_cli()
        finally:
            cli.convert.convert = original
        self.assertEqual(code, 0)


class ReportInjectionTest(unittest.TestCase):
    """Font names come out of the .pub, and the report is opened in Excel."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.report = self.work / "report.csv"

    def _row_for(self, **fields) -> dict:
        result = convert.Result(
            source=Path("a.pub"), output=Path("a.idml"), pages=1, text_frames=1,
            **fields,
        )
        cli._write_report(self.report, [result])
        return read_report(self.report)[0]

    def test_a_font_name_cannot_become_a_formula(self):
        # A font name is an arbitrary string from the file's font table, and
        # a spreadsheet reads a leading '=' as code rather than text.
        row = self._row_for(fonts=["=HYPERLINK(\"http://x\")"])
        self.assertFalse(row["fonts"].startswith("="))
        self.assertIn("HYPERLINK", row["fonts"])

    def test_every_character_a_spreadsheet_treats_as_code_is_defused(self):
        for lead in "=+-@\t\r":
            with self.subTest(lead=lead):
                row = self._row_for(warnings=[f"{lead}cmd|'/c calc'!A1"])
                self.assertFalse(row["warnings"].startswith(lead))

    def test_ordinary_text_is_left_exactly_as_it_was(self):
        row = self._row_for(fonts=["Calibri", "Comic Sans MS"])
        self.assertEqual(row["fonts"], "Calibri; Comic Sans MS")


class CodepageValidationTest(unittest.TestCase):
    """A typo must be caught before an hour of conversion, not after."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        (self.work / "a.pub").write_bytes(b"stub")

    def _run(self, codepage: str) -> int:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            try:
                return cli.run(
                    [
                        str(self.work),
                        "-o",
                        str(self.work / "out"),
                        "--no-log",
                        "--codepage",
                        codepage,
                    ]
                )
            except SystemExit as exit_code:
                self.message = err.getvalue()
                return int(exit_code.code)

    def test_an_unknown_codec_is_rejected_up_front(self):
        self.assertEqual(self._run("cp1251x"), 2)
        self.assertIn("unknown codec", self.message)

    def test_valid_values_are_accepted(self):
        original = convert.convert
        cli.convert.convert = lambda source, destination, **kw: convert.Result(
            source=Path(source), output=Path(destination), pages=1
        )
        try:
            for value in ("auto", "none", "cp1251", "CP1253", "cp932"):
                with self.subTest(value=value):
                    self.assertNotEqual(self._run(value), 2)
        finally:
            cli.convert.convert = original


class NoLogTest(unittest.TestCase):
    """--no-log must be quieter than the default, not louder."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        (self.work / "a.pub").write_bytes(b"stub")

    def test_no_log_does_not_turn_console_warnings_on(self):
        # With no handler attached, logging falls back to stderr, so
        # --no-log used to start printing warnings the default run hid.
        original = convert.convert
        cli.convert.convert = lambda source, destination, **kw: convert.Result(
            source=Path(source), output=Path(destination), pages=1, text_frames=1,
            warnings=["something worth logging"],
        )
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                cli.run([str(self.work), "-o", str(self.work / "out"),
                         "--no-log", "-q"])
        finally:
            cli.convert.convert = original
        self.assertEqual(err.getvalue(), "")

    def test_warnings_still_reach_the_operator_without_quiet(self):
        original = convert.convert
        cli.convert.convert = lambda source, destination, **kw: convert.Result(
            source=Path(source), output=Path(destination), pages=1, text_frames=1,
            warnings=["something worth seeing"],
        )
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                cli.run([str(self.work), "-o", str(self.work / "out"), "--no-log"])
        finally:
            cli.convert.convert = original
        self.assertIn("something worth seeing", out.getvalue())


class FindSourcesTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())

    def test_office_lock_files_are_ignored(self):
        (self.work / "real.pub").write_bytes(b"x")
        (self.work / "~$real.pub").write_bytes(b"x")
        found = cli.find_sources(self.work, recursive=True)
        self.assertEqual([p.name for p in found], ["real.pub"])

    def test_recursion_can_be_switched_off(self):
        nested = self.work / "sub"
        nested.mkdir()
        (self.work / "top.pub").write_bytes(b"x")
        (nested / "deep.pub").write_bytes(b"x")
        self.assertEqual(len(cli.find_sources(self.work, recursive=True)), 2)
        self.assertEqual(len(cli.find_sources(self.work, recursive=False)), 1)

    def test_a_single_file_is_accepted_directly(self):
        target = self.work / "one.pub"
        target.write_bytes(b"x")
        self.assertEqual(cli.find_sources(target, recursive=True), [target])


class DestinationTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())

    def test_folder_structure_is_mirrored(self):
        nested = self.work / "2024" / "spring"
        nested.mkdir(parents=True)
        source = nested / "news.pub"
        source.write_bytes(b"x")
        destination = cli.destination_for(source, self.work, Path("/out"))
        self.assertEqual(destination, Path("/out/2024/spring/news.idml"))

    def test_a_single_source_file_lands_directly_in_the_output(self):
        # The source root is the file itself, so there is no tree to mirror.
        source = self.work / "news.pub"
        source.write_bytes(b"x")
        destination = cli.destination_for(source, source, Path("/out"))
        self.assertEqual(destination, Path("/out/news.idml"))


if __name__ == "__main__":
    unittest.main()


class DetailLineTest(unittest.TestCase):
    """What the one-line summary says about a converted file.

    A recovered WordArt headline is a count rather than a warning, so this
    is where it has to show: a file with fifteen of them used to print a
    paragraph about them and now prints nothing unless something is wrong.
    """

    @staticmethod
    def printed(result: convert.Result) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            cli._print_result(result)
        return out.getvalue()

    def test_the_detail_line_counts_recovered_headlines(self):
        result = convert.Result(source=Path("x.pub"), pages=1, wordart=15)
        self.assertIn("15 wordart", self.printed(result))

    def test_a_file_with_no_headlines_says_nothing_about_them(self):
        result = convert.Result(source=Path("x.pub"), pages=1, wordart=0)
        self.assertNotIn("wordart", self.printed(result))

    def test_the_count_reaches_the_csv(self):
        self.assertIn("wordart", cli.REPORT_COLUMNS)


class FacingPagesFlagTest(unittest.TestCase):
    """Three states, not two: force on, force off, and read the file.

    The flag used to be the only way a booklet could be laid out facing.
    It is now the override, so it has to be able to say *no* as well as
    yes -- a document the file describes as a booklet and the operator
    knows is not one has to have a way out.
    """

    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.work / "a.pub").write_bytes(b"not a real .pub")
        self.seen = []
        original = convert.convert

        def spy(source, destination, **kw):
            self.seen.append(kw.get("facing_pages"))
            return convert.Result(source=Path(source), output=Path(destination), pages=1)

        cli.convert.convert = spy
        self.addCleanup(setattr, cli.convert, "convert", original)

    def _run(self, *extra):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.run([str(self.work), "-o", str(self.work / "out"), "--no-log", *extra])
        return self.seen[-1]

    def test_neither_flag_leaves_the_decision_to_the_file(self):
        self.assertIsNone(self._run())

    def test_facing_pages_forces_it_on(self):
        self.assertIs(self._run("--facing-pages"), True)

    def test_no_facing_pages_forces_it_off(self):
        self.assertIs(self._run("--no-facing-pages"), False)

    def test_the_two_flags_together_are_refused(self):
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cli.run([
                    str(self.work), "-o", str(self.work / "out"), "--no-log",
                    "--facing-pages", "--no-facing-pages",
                ])
        self.assertEqual(int(raised.exception.code), 2)


class FacingDetailLineTest(unittest.TestCase):
    """Saying so when the layout was decided from the file rather than asked
    for. A silent change of spread layout is the thing to avoid."""

    @staticmethod
    def printed(result: convert.Result) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            cli._print_result(result)
        return out.getvalue()

    def test_a_detected_booklet_says_so(self):
        result = convert.Result(
            source=Path("x.pub"), pages=28, facing_pages=True, facing_detected=True
        )
        self.assertIn("facing, detected", self.printed(result))

    def test_a_booklet_that_was_asked_for_does_not(self):
        result = convert.Result(
            source=Path("x.pub"), pages=28, facing_pages=True, facing_detected=False
        )
        self.assertNotIn("detected", self.printed(result))

    def test_a_single_page_document_says_nothing(self):
        result = convert.Result(source=Path("x.pub"), pages=1)
        self.assertNotIn("facing", self.printed(result))

    def test_the_flag_reaches_the_csv(self):
        self.assertIn("facing_pages", cli.REPORT_COLUMNS)
