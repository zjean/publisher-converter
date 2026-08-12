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
