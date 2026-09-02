"""The window itself.

These skip rather than fail where Python has no _tkinter, matching how
the parser tests skip when bin/pubdump is not built. On this project's
development machine that is the normal case: `brew install python-tk`
turns them on.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import tkinter
    _root = tkinter.Tk()
    _root.destroy()
except Exception as exc:            # ImportError, or TclError with no display
    tkinter = None
    _WHY = str(exc)


@unittest.skipIf(tkinter is None, "no usable tkinter on this machine")
class WindowTest(unittest.TestCase):
    def test_the_window_builds_and_tears_down(self):
        # This is what --self-test runs in CI: it is the check that
        # PyInstaller actually bundled Tcl/Tk, which it can quietly fail
        # to do while producing a working-looking executable.
        from pubidml.gui import app
        self.assertEqual(app.self_test(), 0)

    def test_every_step_can_be_drawn(self):
        from pubidml.gui import app, wizard
        application = app.Application()
        try:
            for step in (wizard.CHOOSE, wizard.DESTINATION,
                         wizard.CONVERTING, wizard.DONE):
                application.show(step)
                application.update_idletasks()
        finally:
            application.destroy()


class _ApplicationCase(unittest.TestCase):
    """A window and a scratch tree, torn down whichever way a test ends."""

    def setUp(self):
        from pubidml.gui import app
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        self.application = app.Application()
        self.addCleanup(self.application.destroy)

    def pub_folder(self, name: str, count: int = 2) -> Path:
        folder = self.work / name
        folder.mkdir()
        for index in range(count):
            (folder / f"{index}.pub").write_bytes(b"stub")
        return folder


@unittest.skipIf(tkinter is None, "no usable tkinter on this machine")
class ChooseStepTest(_ApplicationCase):
    def test_a_folder_of_pub_files_is_counted_and_lets_the_user_on(self):
        from pubidml.gui import strings, wizard
        folder = self.pub_folder("Archief")
        self.application.steps[wizard.CHOOSE]._accept([folder])
        self.assertEqual(
            self.application.steps[wizard.CHOOSE].status.cget("text"),
            strings.STEP1_FOUND.format(files=2, folders=1),
        )
        self.assertEqual(str(self.application.next_button["state"]), "normal")

    def test_an_empty_folder_says_so_and_blocks_the_next_button(self):
        from pubidml.gui import strings, wizard
        empty = self.work / "Leeg"
        empty.mkdir()
        self.application.steps[wizard.CHOOSE]._accept([empty])
        self.assertEqual(
            self.application.steps[wizard.CHOOSE].status.cget("text"),
            strings.STEP1_NONE,
        )
        self.assertIsNone(self.application.selection)
        self.assertEqual(str(self.application.next_button["state"]), "disabled")

    def test_files_from_two_roots_say_so_rather_than_nothing_found(self):
        # scan() distinguishes "no .pub files" from "no shared root", and
        # the two need different advice: one says pick another folder, the
        # other says pick from one folder. A relative path beside an
        # absolute one is the only way to reach the second on POSIX.
        from pubidml.gui import strings, wizard
        folder = self.pub_folder("Twee", count=2)
        original_cwd = os.getcwd()
        try:
            os.chdir(folder)
            self.application.steps[wizard.CHOOSE]._accept(
                [Path("0.pub"), folder / "1.pub"]
            )
        finally:
            os.chdir(original_cwd)
        self.assertEqual(
            self.application.steps[wizard.CHOOSE].status.cget("text"),
            strings.STEP1_MIXED_ROOTS,
        )
        self.assertIsNone(self.application.selection)
        self.assertEqual(str(self.application.next_button["state"]), "disabled")


@unittest.skipIf(tkinter is None, "no usable tkinter on this machine")
class NavigationTest(_ApplicationCase):
    def test_a_dropped_folder_arrives_already_on_step_two(self):
        from pubidml.gui import app, wizard
        folder = self.pub_folder("Gesleept")
        application = app.Application(initial=[folder])
        try:
            self.assertEqual(application.step, wizard.DESTINATION)
        finally:
            application.destroy()

    def test_back_from_step_two_returns_to_the_choice(self):
        from pubidml.gui import wizard
        folder = self.pub_folder("Heen")
        self.application.steps[wizard.CHOOSE]._accept([folder])
        self.application._next()
        self.assertEqual(self.application.step, wizard.DESTINATION)
        self.application.back_button.invoke()
        self.assertEqual(self.application.step, wizard.CHOOSE)

    def test_next_still_advances_after_a_restart(self):
        # Step 4 rebinds Next to close the window; without rebinding it
        # back, the restarted wizard's Next would shut the program.
        from pubidml.gui import wizard
        self.application.show(wizard.DONE)
        self.application.restart()
        folder = self.pub_folder("Opnieuw")
        self.application.steps[wizard.CHOOSE]._accept([folder])
        self.application.next_button.invoke()
        self.assertTrue(self.application.winfo_exists())
        self.assertEqual(self.application.step, wizard.DESTINATION)


@unittest.skipIf(tkinter is None, "no usable tkinter on this machine")
class DestinationStepTest(_ApplicationCase):
    def test_a_destination_inside_the_source_is_explained_and_refused(self):
        from pubidml.gui import strings, wizard
        folder = self.pub_folder("Bron")
        self.application.steps[wizard.CHOOSE]._accept([folder])
        step = self.application.steps[wizard.DESTINATION]

        self.application.destination = folder / "uit"
        self.application.show(wizard.DESTINATION)
        self.assertEqual(step.problem.cget("text"), strings.STEP2_INSIDE_SOURCE)
        self.assertEqual(str(self.application.next_button["state"]), "disabled")

        self.application.destination = self.work / "uit"
        self.application.show(wizard.DESTINATION)
        self.assertEqual(step.problem.cget("text"), "")
        self.assertEqual(str(self.application.next_button["state"]), "normal")


class _StubRun:
    """A finished Run, without a thread or a conversion behind it."""

    def __init__(self, failure=None, results=(), cancelled=False):
        self.failure = failure
        self.total = 1
        self.finished = True
        self.cancelled = cancelled
        self.cancel_calls = 0
        self._results = list(results)
        self.polls = 0

    def poll(self):
        self.polls += 1
        return []

    @property
    def results(self):
        return list(self._results)

    def cancel(self):
        self.cancel_calls += 1


@unittest.skipIf(tkinter is None, "no usable tkinter on this machine")
class DrainTest(_ApplicationCase):
    def test_a_finished_run_is_polled_once_more_before_step_four(self):
        # Run.results is complete only one poll past finished: the flag
        # goes up after the report is written, which is after the last put.
        from pubidml.gui import wizard
        self.application.run = _StubRun()
        self.application._drain()
        self.assertEqual(self.application.run.polls, 2)
        self.assertEqual(self.application.step, wizard.DONE)

    def test_a_failure_is_shown_and_step_four_is_still_reached(self):
        # failure means either the worker died or only the report could
        # not be written, and nothing can tell those apart here. Stopping
        # would be a dead end; step 4 counts what did arrive either way.
        from pubidml.gui import app, wizard
        self.application.run = _StubRun(failure=RuntimeError("thread died"))
        with mock.patch.object(app.messagebox, "showerror") as showerror:
            self.application._drain()
        self.assertEqual(showerror.call_count, 1)
        self.assertEqual(self.application.step, wizard.DONE)

    def test_a_clean_run_shows_no_dialog(self):
        from pubidml.gui import app, wizard
        self.application.run = _StubRun()
        with mock.patch.object(app.messagebox, "showerror") as showerror:
            self.application._drain()
        self.assertEqual(showerror.call_count, 0)
        self.assertEqual(self.application.step, wizard.DONE)

    def test_cancelling_reaches_the_run_and_stops_offering_itself(self):
        from pubidml.gui import strings, wizard
        run = _StubRun()
        self.application.run = run
        step = self.application.steps[wizard.CONVERTING]
        self.application.show(wizard.CONVERTING)
        step.cancel_button.invoke()
        self.assertEqual(run.cancel_calls, 1)
        self.assertEqual(str(step.cancel_button["state"]), "disabled")
        self.assertEqual(
            step.cancel_button.cget("text"), strings.STEP3_CANCELLING
        )
