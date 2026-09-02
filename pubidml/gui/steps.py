"""One frame per wizard step.

Nothing here decides anything -- the rules are in wizard.py, which is
tested without a display. These read those answers and draw them.

Every multi-line label sets wraplength and every column that holds text
gets a weight, because Dutch runs about a fifth longer than the English
mock the layout came from and a fixed width would clip it.

Each refresh() also has to survive being called with nothing chosen yet:
self_test() draws all four steps on a window that has never seen a
selection, and that walk is the whole point of the CI check -- a frame
that read shell.selection without guarding it would turn the one test
that proves Tcl/Tk was bundled into a crash about a None.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from . import explain, strings, wizard

PAD = 16
WRAP = 460


class Step(ttk.Frame):
    def __init__(self, master, shell):
        super().__init__(master, padding=PAD)
        self.shell = shell
        self.columnconfigure(0, weight=1)
        self.build()

    def build(self) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        pass


class ChooseStep(Step):
    def build(self) -> None:
        ttk.Label(
            self, text=strings.STEP1_TITLE, font=("", 14, "bold"),
            wraplength=WRAP, justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            self, text=strings.STEP1_DROP_HINT, wraplength=WRAP, justify="left"
        ).grid(row=1, column=0, sticky="w", pady=(PAD, PAD))

        buttons = ttk.Frame(self)
        buttons.grid(row=2, column=0, sticky="w")
        ttk.Button(
            buttons, text=strings.STEP1_CHOOSE_FOLDER, command=self._pick_folder
        ).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(
            buttons, text=strings.STEP1_CHOOSE_FILES, command=self._pick_files
        ).grid(row=0, column=1)

        self.status = ttk.Label(self, text="", wraplength=WRAP, justify="left")
        self.status.grid(row=3, column=0, sticky="w", pady=(PAD, 0))

    def refresh(self) -> None:
        # A restart clears the selection but cannot clear this label by
        # itself, and "12 Publisher-bestanden gevonden in 3 mappen" over a
        # Volgende that will not move is the worst pair this wizard can
        # show: the screen says the files are found and the only way
        # forward is a dead button with no explanation. Coming Back from
        # step 2 keeps its selection, so the count survives that.
        if self.shell.selection is None:
            self.status.configure(text="")

    def _pick_folder(self) -> None:
        chosen = filedialog.askdirectory(title=strings.STEP1_CHOOSE_FOLDER)
        if chosen:
            self.accept([Path(chosen)])

    def _pick_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            title=strings.STEP1_CHOOSE_FILES,
            filetypes=[("Publisher", "*.pub")],
        )
        if chosen:
            self.accept([Path(p) for p in chosen])

    def accept(self, paths) -> None:
        """Take a set of chosen paths, from a dialog or from argv.

        Public because the argv route is a supported gesture: dropping a
        folder onto the program's icon is how this window expects to be
        started, and the shell reaches this directly for it.
        """
        try:
            selection = wizard.scan(paths)
        except wizard.MixedRootsError:
            # Not the same failure as finding nothing, and the way out is
            # different too: there is no folder to mirror the tree from,
            # so the answer is to choose differently rather than to look
            # somewhere else. Saying "no .pub files here" about a folder
            # full of them would send someone hunting for hours.
            self.status.configure(text=strings.STEP1_MIXED_ROOTS)
            self.shell.set_selection(None)
            return
        if selection is None:
            self.status.configure(text=strings.STEP1_NONE)
            self.shell.set_selection(None)
            return
        if len(selection.paths) == 1:
            self.status.configure(text=strings.STEP1_FOUND_ONE)
        else:
            self.status.configure(text=strings.STEP1_FOUND.format(
                files=len(selection.paths), folders=selection.folder_count
            ))
        self.shell.set_selection(selection)


class DestinationStep(Step):
    def build(self) -> None:
        ttk.Label(
            self, text=strings.STEP2_TITLE, font=("", 14, "bold"),
            wraplength=WRAP, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(self, text=strings.STEP2_SAVE_TO).grid(
            row=1, column=0, sticky="w", pady=(PAD, 0)
        )
        # Narrower than the rest of the column: the Wijzigen button sits
        # beside it. A long path wraps rather than pushing the button off
        # the window, and wraps left, because a centred path is unreadable.
        self.path_label = ttk.Label(
            self, text="", wraplength=WRAP - 100, justify="left"
        )
        self.path_label.grid(row=2, column=0, sticky="w")
        ttk.Button(
            self, text=strings.STEP2_CHANGE, command=self._change
        ).grid(row=2, column=1, sticky="e", padx=(8, 0))

        ttk.Label(
            self, text=strings.STEP2_STRUCTURE, wraplength=WRAP, justify="left"
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.problem = ttk.Label(
            self, text="", wraplength=WRAP, justify="left", foreground="#a00"
        )
        self.problem.grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(PAD, 0)
        )

    def refresh(self) -> None:
        destination = self.shell.destination
        self.path_label.configure(text=str(destination) if destination else "")
        self._validate()

    def _change(self) -> None:
        chosen = filedialog.askdirectory(title=strings.STEP2_CHANGE)
        if chosen:
            self.shell.destination = Path(chosen)
            self.refresh()

    def _validate(self) -> None:
        if self.shell.selection is None or self.shell.destination is None:
            # Only reachable by drawing this step out of order, which
            # self_test() does deliberately. Nothing to explain and
            # nothing to start, so say nothing and offer nothing.
            self.problem.configure(text="")
            self.shell.set_can_advance(False)
            return
        problem = wizard.destination_problem(
            self.shell.destination, self.shell.selection
        )
        self.problem.configure(text=problem or "")
        self.shell.set_can_advance(problem is None)


class ConvertingStep(Step):
    def build(self) -> None:
        ttk.Label(
            self, text=strings.STEP3_TITLE, font=("", 14, "bold"),
            wraplength=WRAP, justify="left",
        ).grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(self, mode="determinate", length=WRAP)
        self.bar.grid(row=1, column=0, sticky="ew", pady=(PAD, 8))
        self.count = ttk.Label(self, text="")
        self.count.grid(row=2, column=0, sticky="w")
        self.current = ttk.Label(
            self, text="", wraplength=WRAP, justify="left"
        )
        self.current.grid(row=3, column=0, sticky="w", pady=(4, PAD))
        self.cancel_button = ttk.Button(
            self, text=strings.STEP3_CANCEL, command=self._cancel
        )
        self.cancel_button.grid(row=4, column=0, sticky="w")

    def _cancel(self) -> None:
        # Files already being converted are left to finish, so the button
        # has to stop offering itself and say what it is now waiting for.
        # One that still reads "Annuleren" after a click looks ignored,
        # and gets clicked again.
        self.cancel_button.configure(
            state="disabled", text=strings.STEP3_CANCELLING
        )
        self.shell.cancel_run()

    def refresh(self) -> None:
        self.cancel_button.configure(state="normal", text=strings.STEP3_CANCEL)
        total = self.shell.run.total if self.shell.run is not None else 0
        self.bar.configure(maximum=max(total, 1), value=0)
        self.count.configure(text="")
        self.current.configure(text="")

    def progress(self, done: int, total: int, latest: str) -> None:
        self.bar.configure(value=done)
        self.count.configure(text=strings.STEP3_PROGRESS.format(
            done=done, total=total
        ))
        self.current.configure(text=latest)


class DoneStep(Step):
    def build(self) -> None:
        self.heading = ttk.Label(
            self, text="", font=("", 14, "bold"),
            wraplength=WRAP, justify="left",
        )
        self.heading.grid(row=0, column=0, sticky="w")
        self.counts = ttk.Label(self, text="", justify="left", wraplength=WRAP)
        self.counts.grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.failures = tk.Text(self, height=6, width=56, wrap="word")
        self.failures.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        # The _images sentence is the one way a conversion that reports
        # nothing wrong still loses its pictures: the folder sits beside
        # the .idml and Affinity only takes the images in once the file
        # is saved as its own format. This is the last screen anyone
        # reads, so it is the only place left to say it.
        self.next_hint = ttk.Label(
            self, text=strings.STEP4_NEXT, wraplength=WRAP, justify="left"
        )
        self.next_hint.grid(row=3, column=0, sticky="w", pady=(PAD, PAD))

        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, sticky="w")
        ttk.Button(
            buttons,
            text=strings.STEP4_OPEN_FOLDER,
            command=self.shell.open_output,
        ).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(
            buttons,
            text=strings.STEP4_OPEN_REPORT,
            command=self.shell.open_report,
        ).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(
            buttons, text=strings.STEP4_AGAIN, command=self.shell.restart
        ).grid(row=0, column=2)

    def refresh(self) -> None:
        results = self.shell.run.results if self.shell.run else []
        counts = explain.summary(results)
        converted = counts.ok + counts.review
        template = (
            strings.STEP4_TITLE_CANCELLED
            if self.shell.run and self.shell.run.cancelled
            else strings.STEP4_TITLE
        )
        self.heading.configure(text=template.format(
            done=converted, total=counts.total
        ))

        lines = [strings.STEP4_OK.format(n=counts.ok)]
        if counts.review:
            lines.append(strings.STEP4_REVIEW.format(n=counts.review))
        if counts.failed:
            lines.append(strings.STEP4_FAILED.format(n=counts.failed))
        if counts.skipped:
            lines.append(strings.STEP4_SKIPPED.format(n=counts.skipped))
        self.counts.configure(text="\n".join(lines))

        # Read-only, but filled while it is writable: a disabled Text
        # refuses insert() silently rather than raising, which would
        # leave an empty box where the failures should be.
        self.failures.configure(state="normal")
        self.failures.delete("1.0", "end")
        failed = [r for r in results if not r.ok]
        for result in failed:
            self.failures.insert("end", explain.failure_line(result) + "\n")
        self.failures.configure(state="disabled")
        if not failed:
            self.failures.grid_remove()
        else:
            self.failures.grid()
