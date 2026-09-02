"""The window itself.

These skip rather than fail where Python has no _tkinter, matching how
the parser tests skip when bin/pubdump is not built. On this project's
development machine that is the normal case: `brew install python-tk`
turns them on.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import tkinter
    _root = tkinter.Tk()
    _root.destroy()
    _WHY = ""
except Exception as exc:            # ImportError, or TclError with no display
    tkinter = None
    _WHY = str(exc)

# Carried into the skip message rather than dropped: "no usable tkinter"
# in a CI log leaves the reader to guess between a Python built without
# _tkinter and a runner with no display, and those have different fixes.
_NO_TK = f"no usable tkinter: {_WHY}"


@unittest.skipIf(tkinter is None, _NO_TK)
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
        # Not addCleanup(destroy) directly: the close-path test destroys
        # the window itself, and Tk raises rather than shrugging when a
        # dead root is destroyed a second time -- which would turn a
        # passing test into an ERROR in teardown.
        self.addCleanup(self._destroy_quietly, self.application)

    @staticmethod
    def _destroy_quietly(application):
        try:
            application.destroy()
        except tkinter.TclError:
            pass

    def pub_folder(self, name: str, count: int = 2) -> Path:
        folder = self.work / name
        folder.mkdir()
        for index in range(count):
            (folder / f"{index}.pub").write_bytes(b"stub")
        return folder


@unittest.skipIf(tkinter is None, _NO_TK)
class ChooseStepTest(_ApplicationCase):
    def test_a_folder_of_pub_files_is_counted_and_lets_the_user_on(self):
        from pubidml.gui import strings, wizard
        folder = self.pub_folder("Archief")
        self.application.steps[wizard.CHOOSE].accept([folder])
        self.assertEqual(
            self.application.steps[wizard.CHOOSE].status.cget("text"),
            strings.STEP1_FOUND.format(files=2, folders=1),
        )
        self.assertEqual(str(self.application.next_button["state"]), "normal")

    def test_an_empty_folder_says_so_and_blocks_the_next_button(self):
        from pubidml.gui import strings, wizard
        empty = self.work / "Leeg"
        empty.mkdir()
        self.application.steps[wizard.CHOOSE].accept([empty])
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
            self.application.steps[wizard.CHOOSE].accept(
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


@unittest.skipIf(tkinter is None, _NO_TK)
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
        self.application.steps[wizard.CHOOSE].accept([folder])
        self.application._next()
        self.assertEqual(self.application.step, wizard.DESTINATION)
        self.application.back_button.invoke()
        self.assertEqual(self.application.step, wizard.CHOOSE)

    def test_a_restarted_wizard_stops_claiming_it_found_something(self):
        # The pair this asserts against is a status line reading "2
        # Publisher-bestanden gevonden" beside a Volgende that will not
        # move. test_next_still_advances_after_a_restart cannot see it,
        # because it accepts a new folder before it looks.
        from pubidml.gui import wizard
        folder = self.pub_folder("Eerst")
        self.application.steps[wizard.CHOOSE].accept([folder])
        self.assertNotEqual(
            self.application.steps[wizard.CHOOSE].status.cget("text"), ""
        )
        self.application.show(wizard.DONE)
        self.application.restart()
        self.assertEqual(
            self.application.steps[wizard.CHOOSE].status.cget("text"), ""
        )
        self.assertIsNone(self.application.selection)
        self.assertFalse(self.application._can_advance)

    def test_coming_back_from_step_two_keeps_the_count(self):
        # The other half of the rule above: Back still has a selection, so
        # clearing the status there would lose information the user needs.
        from pubidml.gui import strings, wizard
        folder = self.pub_folder("Terug")
        self.application.steps[wizard.CHOOSE].accept([folder])
        self.application._next()
        self.application.back_button.invoke()
        self.assertEqual(
            self.application.steps[wizard.CHOOSE].status.cget("text"),
            strings.STEP1_FOUND.format(files=2, folders=1),
        )

    def test_the_x_button_goes_through_the_close_handler(self):
        # Calling _on_close() in a test proves nothing about the X button:
        # without this, deleting the protocol registration leaves every
        # test green while the window silently reverts to Tk's default
        # destroy and R13 reopens with no signal at all.
        self.assertTrue(
            self.application.protocol("WM_DELETE_WINDOW"),
            "the X button is not bound to the close handler",
        )

    def test_sluiten_and_the_x_button_take_the_same_way_out(self):
        from pubidml.gui import app, wizard
        # Patched before the step is shown, because show() binds a bound
        # method: patching afterwards would leave the button holding the
        # real one and the assertion would be about nothing.
        with mock.patch.object(app.Application, "_on_close") as on_close:
            self.application.show(wizard.DONE)
            self.application.next_button.invoke()
        self.assertEqual(on_close.call_count, 1)

    def test_no_widget_is_built_while_the_window_could_be_seen(self):
        # The measurement in __init__ needs update_idletasks(), which may
        # map the window. A withdrawn toplevel cannot be mapped, so the
        # guarantee is the window's state, not a belief about Tk's timing
        # -- and the only way that evaporates is someone reordering the
        # withdraw and the widget building, which is what this catches.
        from pubidml.gui import app
        states = []
        original = app.ttk.Frame

        class Recording(original):
            def __init__(self, master, *args, **kwargs):
                states.append(master.winfo_toplevel().state())
                super().__init__(master, *args, **kwargs)

        with mock.patch.object(app.ttk, "Frame", Recording):
            application = app.Application()
        try:
            self.assertTrue(states, "no frame was built at all")
            self.assertEqual(
                set(states), {"withdrawn"},
                "a widget was built while the window was on screen",
            )
            # And it must not stay hidden: deiconify is the last statement.
            self.assertEqual(application.state(), "normal")
        finally:
            application.destroy()

    def test_a_dropped_folder_is_on_screen_only_once_it_reads_step_two(self):
        # deiconify last, so a wizard started from the icon does not
        # appear on step 1 and then jump.
        from pubidml.gui import app, wizard
        folder = self.pub_folder("Gesleept")
        states = []
        original = app.Application.deiconify

        def recording(self):
            states.append(self.step)
            return original(self)

        with mock.patch.object(app.Application, "deiconify", recording):
            application = app.Application(initial=[folder])
        try:
            self.assertEqual(states, [wizard.DESTINATION])
            self.assertEqual(application.state(), "normal")
        finally:
            application.destroy()

    def test_the_window_is_floored_at_the_roomiest_step(self):
        # Only one frame is mapped, so the window would otherwise grow
        # entering the tallest step and shrink leaving it.
        from pubidml.gui import app, wizard
        tallest = max(
            frame.winfo_reqheight()
            for frame in self.application.steps.values()
        )
        widest = max(
            frame.winfo_reqwidth()
            for frame in self.application.steps.values()
        )
        floor_width, floor_height = self.application.minsize()
        self.assertGreaterEqual(floor_width, max(app.MIN_WIDTH, widest))
        self.assertGreaterEqual(floor_height, max(app.MIN_HEIGHT, tallest))

    def test_only_the_current_step_is_in_the_keyboard_chain(self):
        # tkraise alone left every frame mapped, so the three hidden
        # steps' buttons stayed Tab-reachable -- step 4's restart button
        # could be pressed during step 3. An unmapped frame is out of the
        # traversal chain; grid_info is empty for one.
        from pubidml.gui import wizard
        self.application.show(wizard.CONVERTING)
        self.assertNotEqual(
            self.application.steps[wizard.CONVERTING].grid_info(), {}
        )
        for hidden in (wizard.CHOOSE, wizard.DESTINATION, wizard.DONE):
            self.assertEqual(
                self.application.steps[hidden].grid_info(), {},
                f"step {hidden} is still mapped behind step 3",
            )

    def test_next_still_advances_after_a_restart(self):
        # Step 4 rebinds Next to close the window; without rebinding it
        # back, the restarted wizard's Next would shut the program.
        from pubidml.gui import wizard
        self.application.show(wizard.DONE)
        self.application.restart()
        folder = self.pub_folder("Opnieuw")
        self.application.steps[wizard.CHOOSE].accept([folder])
        self.application.next_button.invoke()
        self.assertTrue(self.application.winfo_exists())
        self.assertEqual(self.application.step, wizard.DESTINATION)


@unittest.skipIf(tkinter is None, _NO_TK)
class DestinationStepTest(_ApplicationCase):
    def test_a_destination_inside_the_source_is_explained_and_refused(self):
        from pubidml.gui import strings, wizard
        folder = self.pub_folder("Bron")
        self.application.steps[wizard.CHOOSE].accept([folder])
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

    def __init__(self, failure=None, results=(), cancelled=False,
                 finished=True, arriving=(), total=1):
        self.failure = failure
        self.total = total
        self.finished = finished
        self.cancelled = cancelled
        self.cancel_calls = 0
        self._results = list(results)
        self._arriving = list(arriving)
        self.polls = 0

    def poll(self):
        self.polls += 1
        arrived, self._arriving = self._arriving, []
        self._results.extend(arrived)
        return arrived

    @property
    def results(self):
        return list(self._results)

    def cancel(self):
        self.cancel_calls += 1


@unittest.skipIf(tkinter is None, _NO_TK)
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

    def test_a_run_still_going_books_another_tick(self):
        # Without this, dropping the reschedule leaves every test green
        # and ships a wizard that freezes on step 3 after 100ms -- the
        # single worst outcome available, since the batch keeps running
        # and the window never says another word.
        from pubidml.gui import wizard
        self.application.run = _StubRun(finished=False)
        self.application.show(wizard.CONVERTING)
        self.application._drain()
        self.assertIsNotNone(self.application._tick)
        self.assertEqual(self.application.step, wizard.CONVERTING)

    def test_a_result_that_lands_moves_the_progress_line(self):
        from pubidml import convert
        from pubidml.gui import strings, wizard
        result = convert.Result(
            source=Path("een.pub"), output=Path("een.idml"),
            pages=1, text_frames=1,
        )
        self.application.run = _StubRun(
            finished=False, arriving=[result], total=2
        )
        self.application.show(wizard.CONVERTING)
        self.application._drain()
        step = self.application.steps[wizard.CONVERTING]
        self.assertEqual(
            step.count.cget("text"),
            strings.STEP3_PROGRESS.format(done=1, total=2),
        )
        self.assertEqual(step.current.cget("text"), "een.pub")

    def test_booking_a_tick_cancels_the_one_already_out(self):
        # The invariant _schedule_drain exists to hold. Nothing else
        # reaches it with a tick outstanding, so without this test the
        # cancel-before-book could be deleted in silence.
        from pubidml.gui import app
        self.application.run = _StubRun(finished=False)
        self.application._schedule_drain()
        first = self.application._tick
        with mock.patch.object(app.Application, "after_cancel") as cancel:
            self.application._schedule_drain()
        cancel.assert_called_once_with(first)
        self.assertNotEqual(self.application._tick, first)

    def test_a_restart_mid_run_leaves_no_tick_to_fire(self):
        # A tick already booked cannot be recalled, so dropping the Run
        # can leave exactly one queued callback behind. It must neither
        # raise nor book another.
        self.application.run = _StubRun(finished=False)
        self.application._schedule_drain()
        self.application.restart()
        self.assertIsNone(self.application._tick)
        self.application._drain()            # the tick that got away
        self.assertIsNone(self.application._tick)

    def test_closing_mid_run_asks_the_batch_to_stop(self):
        # Cancel and go: the worker is not waited for, because a close
        # that hangs for a wave of per-file time reads as a crash.
        #
        # destroy is asserted rather than performed. Tk swaps its whole
        # command table for the dead-app handler once the last main window
        # goes, so any call after a real destroy -- winfo_exists very much
        # included -- raises TclError. Asking the window whether it still
        # exists is the one question it can no longer answer, so this used
        # to be the very bug it was written to catch.
        from pubidml.gui import app
        run = _StubRun(finished=False)
        self.application.run = run
        self.application._schedule_drain()
        with mock.patch.object(app.Application, "destroy") as destroy:
            self.application._on_close()
        self.assertEqual(destroy.call_count, 1)
        self.assertEqual(run.cancel_calls, 1)
        self.assertIsNone(self.application._tick)

    def test_the_close_handler_really_takes_the_window_down(self):
        # The mocked test above proves _on_close *calls* destroy. It
        # cannot prove the call lands, so this one does it for real -- and
        # then asks the only way Tk still allows: once the last main
        # window goes, Tk swaps its command table for the dead-app
        # handler, so every further call raises instead of answering.
        # That is also what makes the teardown guard necessary, so this
        # test is what keeps the guard exercised.
        self.application._on_close()
        with self.assertRaises(tkinter.TclError):
            self.application.winfo_exists()

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


@unittest.skipIf(tkinter is None, _NO_TK)
class SelfTestEvidenceTest(unittest.TestCase):
    """--self-test has to leave evidence a windowed build can produce.

    The build that runs it is console=False: there is no stdout, and
    sys.stderr is None rather than a sink, so a print() of the diagnostic
    went nowhere at all -- silently discarded, not even an error to
    notice -- and the switch returns before the normal logging setup, so
    there was no log either. What reached CI was a red timeout with
    nothing in it.
    """

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        # The file handler this test's main() attaches would otherwise
        # outlive it, holding a deleted temporary open -- which arrives
        # as a ResourceWarning in another test's output.
        self.addCleanup(self._detach_log_handlers)
        # main() installs an excepthook, and hooks stack.
        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)

    @staticmethod
    def _detach_log_handlers():
        logger = logging.getLogger("pubidml")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    class _Frame:
        def __init__(self, width, height):
            self._size = (width, height)

        def winfo_reqwidth(self):
            return self._size[0]

        def winfo_reqheight(self):
            return self._size[1]

    def _stand_in_window(self, sizes):
        """A window that reports the sizes this test wants measured.

        A real Tk frame full of labels cannot be made to report 1x1, so
        the failure branch is unreachable with the real thing -- and the
        failure branch is the one whose evidence went missing.
        """
        frames = {step: self._Frame(*size) for step, size in sizes.items()}

        class _Window:
            def __init__(self):
                self.steps = dict(frames)

            def withdraw(self):
                pass

            def show(self, step):
                pass

            def update_idletasks(self):
                pass

            def destroy(self):
                pass

        return _Window

    def test_an_unmeasured_step_exits_one_with_readable_evidence(self):
        from pubidml.gui import app, wizard
        sizes = {
            wizard.CHOOSE: (420, 300),
            wizard.DESTINATION: (420, 300),
            wizard.CONVERTING: (420, 300),
            wizard.DONE: (1, 1),
        }
        with mock.patch.object(app, "Application", self._stand_in_window(sizes)), \
                mock.patch.object(app.sys, "stderr", None):
            with self.assertLogs("pubidml.gui", level="ERROR") as captured:
                code = app.self_test()
        self.assertEqual(code, 1)
        self.assertIn("done (1x1)", "\n".join(captured.output))

    def test_a_measured_window_records_what_it_saw(self):
        from pubidml.gui import app, wizard
        sizes = {step: (420, 300) for step in (
            wizard.CHOOSE, wizard.DESTINATION, wizard.CONVERTING, wizard.DONE
        )}
        with mock.patch.object(app, "Application", self._stand_in_window(sizes)):
            with self.assertLogs("pubidml.gui", level="INFO") as captured:
                code = app.self_test()
        self.assertEqual(code, 0)
        line = "\n".join(captured.output)
        for name in ("choose", "destination", "converting", "done"):
            self.assertIn(name, line)

    def test_the_evidence_file_sits_beside_a_frozen_executable(self):
        # Not the per-user log folder the run logs use: the workflow has
        # to collect this file, and it cannot guess a timestamped name
        # under %LOCALAPPDATA%.
        from pubidml.gui import app
        with mock.patch.object(app.sys, "frozen", True, create=True), \
                mock.patch.object(
                    app.sys, "executable", os.path.join("opt", "gui.exe")):
            self.assertEqual(
                app.self_test_log_path(),
                Path("opt") / app.SELF_TEST_LOG_NAME,
            )

    def test_the_switch_writes_that_file_before_it_returns(self):
        # End to end through main(), which is where the ordering matters:
        # the branch used to return before logging was configured at all.
        from pubidml.gui import app
        original = os.getcwd()
        os.chdir(self.work)
        try:
            code = app.main(["--self-test"])
        finally:
            os.chdir(original)
        self.assertEqual(code, 0)
        written = (self.work / app.SELF_TEST_LOG_NAME).read_text(
            encoding="utf-8"
        )
        self.assertIn("self-test:", written)
        # The environment banner too: which build produced this evidence
        # is the first question asked of it.
        self.assertIn("frozen bundle:", written)


@unittest.skipIf(tkinter is None, _NO_TK)
class DoneStepTest(_ApplicationCase):
    def test_step_four_says_to_keep_the_images_folder(self):
        # The one way a conversion that reports nothing wrong still loses
        # its pictures, and step 4 is the only place it is ever said. A
        # future edit could drop this label with a green suite otherwise.
        from pubidml.gui import strings, wizard
        self.application.show(wizard.DONE)
        self.assertEqual(
            self.application.steps[wizard.DONE].next_hint.cget("text"),
            strings.STEP4_NEXT,
        )
        self.assertIn("_images", strings.STEP4_NEXT)

    def test_a_file_that_failed_is_named_with_its_reason(self):
        from pubidml import convert
        from pubidml.gui import strings, wizard
        result = convert.Result(
            source=Path("kapot.pub"),
            error="not a supported Publisher file (or corrupt)",
        )
        self.application.run = _StubRun(results=[result])
        self.application.show(wizard.DONE)
        step = self.application.steps[wizard.DONE]
        shown = step.failures.get("1.0", "end")
        self.assertIn("kapot.pub", shown)
        self.assertIn("beschadigd", shown)
        self.assertEqual(
            step.counts.cget("text"),
            "\n".join([
                strings.STEP4_OK.format(n=0),
                strings.STEP4_FAILED.format(n=1),
            ]),
        )

    def _converted(self, name):
        from pubidml import convert
        return convert.Result(
            source=Path(name), output=Path(name).with_suffix(".idml"),
            pages=1, text_frames=1,
        )

    def test_a_stopped_run_counts_the_files_it_was_asked_for(self):
        # The heading's denominator has to be the size of the job, not
        # the number of results that came back: a 145-file batch stopped
        # after ten used to read "Gestopt -- 10 van de 10 bestanden",
        # which tells someone with nobody to ask that the work is done.
        from pubidml.gui import strings, wizard
        self.application.run = _StubRun(
            results=[self._converted(f"{i}.pub") for i in range(10)],
            cancelled=True, total=145,
        )
        self.application.show(wizard.DONE)
        self.assertEqual(
            self.application.steps[wizard.DONE].heading.cget("text"),
            strings.STEP4_TITLE_CANCELLED.format(done=10, total=145),
        )

    def test_a_run_that_failed_halfway_counts_the_same_way(self):
        # The failure path reaches step 4 as well (the run died, or only
        # the report could not be written), and the same arithmetic
        # applies there.
        from pubidml.gui import strings, wizard
        self.application.run = _StubRun(
            failure=RuntimeError("thread died"),
            results=[self._converted("0.pub")], total=8,
        )
        self.application.show(wizard.DONE)
        self.assertEqual(
            self.application.steps[wizard.DONE].heading.cget("text"),
            strings.STEP4_TITLE.format(done=1, total=8),
        )

    def test_a_clean_run_still_counts_every_file_including_the_skipped(self):
        # The other side of the fix: files passed over as already
        # converted are not jobs, so they are in the results and not in
        # run.total. Adding them back is what keeps a finished run's
        # heading agreeing with the lines beneath it.
        from pubidml import convert
        from pubidml.gui import strings, wizard
        skipped = [
            convert.Result(
                source=Path(f"oud{i}.pub"),
                output=Path(f"oud{i}.idml"), skipped=True,
            )
            for i in range(2)
        ]
        self.application.run = _StubRun(
            results=[self._converted("0.pub"), self._converted("1.pub")]
            + skipped,
            total=2,
        )
        self.application.show(wizard.DONE)
        self.assertEqual(
            self.application.steps[wizard.DONE].heading.cget("text"),
            strings.STEP4_TITLE.format(done=2, total=4),
        )

    def test_the_report_button_is_dead_only_when_there_is_no_report(self):
        # A button that opens nothing reads as a broken program, and the
        # missing-report case is exactly the case someone needs
        # explained. Both states are asserted, so the fix cannot degrade
        # into a button that is always grey.
        from pubidml.gui import app, strings, wizard
        self.application.destination = self.work / "omgezet"
        self.application.destination.mkdir()
        self.application.show(wizard.DONE)
        step = self.application.steps[wizard.DONE]
        self.assertEqual(str(step.open_report_button["state"]), "disabled")
        self.assertEqual(str(step.open_folder_button["state"]), "normal")
        self.assertTrue(step.no_report.winfo_ismapped()
                        or step.no_report.winfo_manager() == "grid")
        self.assertEqual(step.no_report.cget("text"), strings.STEP4_NO_REPORT)

        (self.application.destination / app.REPORT_NAME).write_text(
            "source\n", encoding="utf-8"
        )
        step.refresh()
        self.assertEqual(str(step.open_report_button["state"]), "normal")
        self.assertEqual(step.no_report.winfo_manager(), "")

    def test_the_folder_button_is_dead_when_nothing_was_written(self):
        # Reachable: a run whose destination could not be created at all.
        from pubidml.gui import wizard
        self.application.destination = self.work / "bestaat-niet"
        self.application.show(wizard.DONE)
        step = self.application.steps[wizard.DONE]
        self.assertEqual(str(step.open_folder_button["state"]), "disabled")
