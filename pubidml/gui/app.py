"""The wizard shell: one window, four steps, and the loop that drains
the conversion thread.

The only code in the project that calls tkinter beyond this package's own
frames. Nothing outside pubidml/gui imports it, which is what keeps the
console executable free of Tcl/Tk.

The window is deliberately the thinnest layer in the package. Every rule
it applies -- what was chosen, where it may go, what the counts mean --
lives in wizard.py, explain.py and batch.py, all tested without a
display, because this machine has no _tkinter and a rule that can only be
checked by clicking is a rule nobody checks.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import List, Optional

from .. import batch, convert, logsetup
from . import runner, steps, strings, wizard

#: The same name the CLI defaults to, so a collection converted from the
#: window and one converted from the terminal leave the same report.
REPORT_NAME = "conversion-report.csv"


class Application(tk.Tk):
    def __init__(
        self,
        initial: Optional[List[Path]] = None,
        log_path: Optional[Path] = None,
    ):
        super().__init__()
        self.title(strings.WINDOW_TITLE)
        self.minsize(560, 460)

        self.selection: Optional[wizard.Selection] = None
        self.destination: Optional[Path] = None
        self.run: Optional[runner.Run] = None
        # Named in the one dialog this window can raise, so the person
        # reading it has something to send on. main() passes the path
        # logging actually opened; a test-built window has none.
        self.log_path = log_path
        self._log = logsetup.get_logger("gui")
        self._can_advance = False
        self._seen = 0

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        # All four frames are built once and stacked, and tkraise picks
        # which one shows. Building on demand would mean a step's widgets
        # only exist once someone reached it, and self_test() -- the check
        # that Tcl/Tk was really bundled -- would then prove nothing about
        # the three steps it did not reach.
        self.steps = {
            wizard.CHOOSE: steps.ChooseStep(container, self),
            wizard.DESTINATION: steps.DestinationStep(container, self),
            wizard.CONVERTING: steps.ConvertingStep(container, self),
            wizard.DONE: steps.DoneStep(container, self),
        }
        for frame in self.steps.values():
            frame.grid(row=0, column=0, sticky="nsew")

        self.nav = ttk.Frame(self, padding=(16, 0, 16, 16))
        self.nav.pack(fill="x")
        self.back_button = ttk.Button(
            self.nav, text=strings.BACK, command=self._back
        )
        self.back_button.pack(side="left")
        self.next_button = ttk.Button(
            self.nav, text=strings.NEXT, command=self._next
        )
        self.next_button.pack(side="right")

        self.step = wizard.CHOOSE
        self.show(wizard.CHOOSE)

        if initial:
            # A folder dropped on the program's icon. Skipping past step 1
            # is the whole point of the gesture: the choice has been made
            # already, and asking for it again reads as if the drop failed.
            self.steps[wizard.CHOOSE]._accept(initial)
            if self.selection is not None:
                self._next()

    # --- shell API the steps call ---------------------------------
    def set_selection(self, selection) -> None:
        self.selection = selection
        if selection is not None:
            self.destination = wizard.default_destination(selection)
        self.set_can_advance(selection is not None)

    def set_can_advance(self, can: bool) -> None:
        self._can_advance = can
        self.next_button.configure(state="normal" if can else "disabled")

    def cancel_run(self) -> None:
        if self.run is not None:
            self.run.cancel()

    def restart(self) -> None:
        # The old Run is dropped rather than reused: Run.start() refuses a
        # second call and its jobs are fixed at construction, so a second
        # batch is a second object by design.
        self.selection = None
        self.destination = None
        self.run = None
        self._seen = 0
        self.show(wizard.CHOOSE)

    def open_output(self) -> None:
        self._reveal(self.destination)

    def open_report(self) -> None:
        if self.destination is None:
            return
        self._reveal(self.destination / REPORT_NAME)

    def _reveal(self, path: Optional[Path]) -> None:
        if path is None or not Path(path).exists():
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            elif os.name == "nt":
                os.startfile(str(path))       # noqa: S606 - Windows shell open
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            self._log.exception("could not open %s", path)

    # --- navigation ------------------------------------------------
    def show(self, step: int) -> None:
        self.step = step
        frame = self.steps[step]
        # refresh() before the button states, because step 2's refresh is
        # what decides whether the run may start at all -- it calls
        # set_can_advance, and the branches below read the answer.
        frame.refresh()
        frame.tkraise()
        self.back_button.configure(
            state="normal" if step in (wizard.DESTINATION,) else "disabled"
        )
        if step == wizard.CHOOSE:
            self.next_button.configure(
                text=strings.NEXT,
                state="normal" if self.selection else "disabled",
                command=self._next,
            )
        elif step == wizard.DESTINATION:
            # Enabled only if the destination survived validation. Leaving
            # it enabled while _next() silently refuses would give a
            # button that does nothing, which reads as a broken program
            # rather than as a refused destination.
            self.next_button.configure(
                text=strings.START,
                state="normal" if self._can_advance else "disabled",
                command=self._next,
            )
        elif step == wizard.CONVERTING:
            self.next_button.configure(state="disabled", command=self._next)
        else:
            self.next_button.configure(
                text=strings.CLOSE, state="normal", command=self.destroy
            )

    def _back(self) -> None:
        if self.step == wizard.DESTINATION:
            self.show(wizard.CHOOSE)

    def _next(self) -> None:
        if self.step == wizard.CHOOSE and self.selection:
            self.show(wizard.DESTINATION)
        elif self.step == wizard.DESTINATION and self._can_advance:
            self._start_run()

    # --- the run ---------------------------------------------------
    def _start_run(self) -> None:
        # One root for both cases: batch.destination_for branches on
        # source_root.is_file() itself, which is what flattens a single
        # chosen file into the destination instead of mirroring the whole
        # path it happened to live at.
        source_root = self.selection.root
        jobs, skipped = batch.plan(
            self.selection.paths, source_root, self.destination, force=False
        )
        self.run = runner.Run(
            jobs,
            batch.Options(),
            self.destination / REPORT_NAME,
            skipped,
        )
        self._seen = 0
        self.show(wizard.CONVERTING)
        self.run.start()
        self.after(100, self._drain)

    def _drain(self) -> None:
        """The only place a Result reaches a widget."""
        arrived = self.run.poll()
        self._seen += len(arrived)
        if arrived:
            self.steps[wizard.CONVERTING].progress(
                self._seen, self.run.total, arrived[-1].source.name
            )
        if self.run.finished:
            # One more poll past the flag: the worker raises it only after
            # writing the report, which is after its last queue put, so
            # the queue can still be holding the final results.
            self.run.poll()
            failure = self.run.failure
            if failure is not None:
                # Two different things set this -- the conversion thread
                # died, or every file converted and only the report could
                # not be written -- and nothing here can tell them apart.
                # So do both: say something went wrong, and still show
                # step 4. Step 4 counts what actually arrived, which is
                # true either way, where stopping here would leave someone
                # whose files all converted with a dialog and no results.
                self._log.error("the run reported a failure", exc_info=failure)
                messagebox.showerror(
                    strings.WINDOW_TITLE,
                    strings.ERROR_UNEXPECTED.format(path=self.log_path or "-"),
                )
            self.show(wizard.DONE)
            return
        self.after(100, self._drain)


def self_test() -> int:
    """Build the whole window and tear it down. Returns 0 on success.

    This is what CI runs inside the PATH-stripped directory. PyInstaller
    can fail to bundle Tcl/Tk while still producing an executable that
    starts, so the check has to reach as far as a real widget tree -- and
    as far as all four steps, since a frame is only actually laid out once
    something raises it.
    """
    application = Application()
    application.withdraw()
    for step in (wizard.CHOOSE, wizard.DESTINATION,
                 wizard.CONVERTING, wizard.DONE):
        application.show(step)
        application.update_idletasks()
    application.destroy()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return self_test()

    log_path = logsetup.configure(None, verbose=False)
    logsetup.install_excepthook()
    # The same environment banner the CLI writes. Without it the GUI log
    # lacks the resolved parser path, which is the first thing to look at
    # when a bundled executable misbehaves on someone else's machine.
    logsetup.log_environment(convert.PUBDUMP)

    # Everything on the command line that is not a switch is treated as a
    # dropped path. Dropping onto the icon is the supported gesture --
    # in-window drag-and-drop needs a package outside the standard
    # library, and this program ships with none.
    initial = [Path(a) for a in argv if not a.startswith("-")]
    application = Application(initial=initial or None, log_path=log_path)

    # A windowed build has no stderr, so an uncaught exception in a Tk
    # callback would otherwise vanish: the user sees a window that stopped
    # responding and has nothing to report. Tk's own handler is the only
    # place that can still speak.
    def report_exception(exc_type, exc, tb):
        logsetup.get_logger("gui").error(
            "unhandled exception", exc_info=(exc_type, exc, tb)
        )
        messagebox.showerror(
            strings.WINDOW_TITLE,
            strings.ERROR_UNEXPECTED.format(path=log_path or "-"),
        )

    application.report_callback_exception = report_exception
    application.mainloop()
    return 0
