"""The job loop, independent of who is driving it.

cli.run() used to fuse argument parsing, planning, the thread pool,
printing and CSV writing into one function. These tests cover the middle
three so the GUI can rely on them without a second implementation.
"""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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


class FindSourcesTest(unittest.TestCase):
    """The glob and the ~$ lock-file filter, shared by every caller."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.root = self.work / "in"
        self.root.mkdir()

    def test_a_nested_tree_is_found_recursively(self):
        nested = self.root / "2019" / "spring"
        nested.mkdir(parents=True)
        (self.root / "a.pub").write_bytes(b"stub")
        (nested / "b.pub").write_bytes(b"stub")
        found = batch.find_sources(self.root, recursive=True)
        self.assertEqual({p.name for p in found}, {"a.pub", "b.pub"})

    def test_recursive_false_does_not_descend(self):
        nested = self.root / "2019"
        nested.mkdir()
        (self.root / "a.pub").write_bytes(b"stub")
        (nested / "b.pub").write_bytes(b"stub")
        found = batch.find_sources(self.root, recursive=False)
        self.assertEqual([p.name for p in found], ["a.pub"])

    def test_a_publisher_lock_file_is_excluded(self):
        (self.root / "a.pub").write_bytes(b"stub")
        (self.root / "~$a.pub").write_bytes(b"stub")
        found = batch.find_sources(self.root, recursive=True)
        self.assertEqual([p.name for p in found], ["a.pub"])

    def test_a_single_file_is_returned_directly(self):
        target = self.root / "a.pub"
        target.write_bytes(b"stub")
        found = batch.find_sources(target, recursive=True)
        self.assertEqual(found, [target])


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

    def test_the_pool_is_built_from_the_worker_count_run_batch_resolved(self):
        # The submission window is sized from resolved_workers, so the
        # pool has to be the same size: letting the executor pick its own
        # default again would make the window proportional to a number
        # nothing in this file owns, and would leave the documented
        # default a bet on what CPython happens to compute.
        convert.convert = lambda source, destination, **kw: convert.Result(
            source=Path(source), output=Path(destination), pages=1
        )
        seen = []
        real_pool = concurrent.futures.ThreadPoolExecutor

        def recording(*args, **kwargs):
            seen.append(kwargs.get("max_workers"))
            return real_pool(*args, **kwargs)

        with mock.patch.object(
            batch.concurrent.futures, "ThreadPoolExecutor", recording
        ):
            batch.run_batch(self._jobs(1), batch.Options())
        self.assertEqual(seen, [min(32, (os.cpu_count() or 1) + 4)])

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
