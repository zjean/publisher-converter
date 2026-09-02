"""What the wizard decides, before any of it is drawn."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path, PureWindowsPath

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

    # test_files_chosen_from_one_folder_share_it_as_their_root above is
    # the same-anchor case: it already proves the mixed-root guard does
    # not fire on the common path of a normal, single-drive selection.

    def test_files_with_no_common_anchor_cannot_be_scanned_honestly_here(self):
        # PureWindowsPath honestly represents two drives / a UNC share
        # beside a drive letter, but scan() converts every input through
        # Path(), and on this POSIX machine Path(PureWindowsPath(...))
        # collapses both to anchor "" and fails .exists() -- so passing
        # these through scan() would prove nothing. The real guard is
        # _shares_one_root; test it directly with paths whose anchors are
        # genuinely different, which is exactly what scan()'s guard reads.
        c_drive = PureWindowsPath(r"C:\Archief\a.pub")
        d_drive = PureWindowsPath(r"D:\Archief\b.pub")
        unc_share = PureWindowsPath(r"\\server\share\c.pub")
        self.assertFalse(wizard._shares_one_root([c_drive, d_drive]))
        self.assertFalse(wizard._shares_one_root([c_drive, unc_share]))
        self.assertTrue(
            wizard._shares_one_root([c_drive, PureWindowsPath(r"C:\Archief\b.pub")])
        )


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
