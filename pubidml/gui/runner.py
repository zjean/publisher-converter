"""Carrying a batch across the thread boundary Tk insists on.

Tk is single-threaded, and touching a widget from another thread corrupts
it quietly rather than raising -- so nothing here touches a widget. The
batch runs on its own thread and puts each Result on a queue; the main
loop calls poll() from root.after() and does the drawing.

The pool inside run_batch is left alone. The GUI adds one thread so the
window keeps painting; it does not flatten the parallelism the batch
already has.

Ownership is split cleanly down the thread boundary: the worker thread
owns its own accumulation of results (needed to write the report once the
run ends, however it ends) and the queue is how it hands each one to the
main thread without either side reading the other's memory. self._results
belongs to poll() and the main thread alone -- the worker never reads or
writes it, which is what makes concatenating "everything seen" safe: it
happens once, from the worker's own list, in the worker's own thread.

One object is genuinely shared without a lock: the individual Result
instances themselves cross from the worker's `seen` list into the queue
and from there into self._results, so the same object sits in both
places at once. Result is a plain dataclass with mutable `fonts` and
`warnings` lists, but nothing on either side of the boundary ever writes
to a Result once convert() has returned it -- both threads only read --
so the aliasing carries no live race. Freezing Result would close the
theoretical gap but lives in convert.py, out of this module's reach.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import List, Optional

from .. import batch, convert, logsetup


class Run:
    def __init__(self, jobs, options, report_path, pre_skipped):
        self._jobs = list(jobs)
        self._options = options
        self._report_path = Path(report_path)
        self._pre_skipped = list(pre_skipped)
        # Main-thread-only: seeded once here (before start(), so there is
        # no thread yet to race with) then extended only by poll(). The
        # worker thread never reads or writes this list -- it keeps its
        # own copy of pre_skipped above for the report.
        self._results: List[convert.Result] = list(pre_skipped)
        self._queue: "queue.Queue[convert.Result]" = queue.Queue()
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failure: Optional[BaseException] = None
        # Held as an attribute so a test can stand in for it without
        # patching the module -- the only way to exercise a dead thread
        # without actually breaking run_batch for every other test.
        self._run_batch = batch.run_batch

    @property
    def total(self) -> int:
        return len(self._jobs)

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def results(self) -> List[convert.Result]:
        """Everything poll() has drained.

        Complete only after one more poll() past finished: the worker
        sets the flag only after the report is written, which is after
        its last queue.put, so the queue can still hold items put just
        before the flag went up. A copy, not the accumulator itself, so a
        caller sorting or mutating what it gets back cannot corrupt
        poll()'s own list.
        """
        return list(self._results)

    @property
    def failure(self) -> Optional[BaseException]:
        return self._failure

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Run.start() called more than once")
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def poll(self) -> List[convert.Result]:
        """Whatever has landed since the last call. Main thread only."""
        arrived = []
        while True:
            try:
                arrived.append(self._queue.get_nowait())
            except queue.Empty:
                break
        self._results.extend(arrived)
        return arrived

    def _work(self) -> None:
        log = logsetup.get_logger("gui")

        # Owned entirely by this thread: the report is built from this
        # list, never from self._results, so there is nothing here for
        # the main thread's poll() to race against.
        seen: List[convert.Result] = []

        def collect(result: convert.Result) -> None:
            # run_batch calls this inline, between one job finishing and
            # the next being submitted -- an exception escaping here
            # propagates out of run_batch and discards every result it
            # already collected, including the ones already reported to
            # the user. Neither list.append nor an unbounded Queue.put
            # raises in normal operation, but the whole body is guarded
            # anyway: nothing that happens to a GUI's progress feed should
            # be able to cost the batch its results.
            try:
                seen.append(result)
                self._queue.put(result)
            except Exception:
                log.exception("on_result callback failed for %s", result.source)

        try:
            self._run_batch(
                self._jobs,
                self._options,
                on_result=collect,
                cancel=self._cancel,
            )
        except BaseException as exc:
            # A GUI stuck on a spinner forever is worse than one that says
            # something went wrong, so the thread's death is a value the
            # main loop can read rather than a traceback nobody sees.
            log.exception("the conversion thread died")
            self._failure = exc
        finally:
            # The report is written from a try whose except catches
            # Exception, not OSError: a decades-old Publisher collection
            # can hand back a source path the filesystem itself only
            # decoded with surrogateescape, and csv writing that under
            # utf-8 raises UnicodeEncodeError -- a ValueError, not an
            # OSError. Narrower than Exception would let that (or any
            # other surprise from write_report) skip straight past
            # self._done.set() below, and a flag the main loop never sees
            # go up is exactly the forever-spinner this class exists to
            # avoid.
            try:
                everything = self._pre_skipped + seen
                everything.sort(key=lambda r: str(r.source))
                batch.write_report(self._report_path, everything)
            except Exception as exc:
                log.exception(
                    "could not write report to %s", self._report_path
                )
                # A run_batch death (already recorded above) is the more
                # informative failure; don't let a report-writing failure
                # it caused overwrite it.
                if self._failure is None:
                    self._failure = exc
            finally:
                self._done.set()
