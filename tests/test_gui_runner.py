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
            self._jobs(6), batch.Options(), self.report, []
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
            self._jobs(2), batch.Options(), self.report, []
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
            self._jobs(1), batch.Options(), self.report, [already]
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
            self._jobs(1), batch.Options(), self.report, []
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
            self._jobs(200), batch.Options(), self.report, []
        )
        run.start()
        time.sleep(0.1)
        run.cancel()
        drain(run)
        self.assertLess(len(run.results), 200)
        self.assertTrue(self.report.exists(), "a stopped run still leaves a report")


if __name__ == "__main__":
    unittest.main()
