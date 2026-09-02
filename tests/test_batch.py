"""The job loop, independent of who is driving it.

cli.run() used to fuse argument parsing, planning, the thread pool,
printing and CSV writing into one function. These tests cover the middle
three so the GUI can rely on them without a second implementation.
"""

from __future__ import annotations

import tempfile
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
