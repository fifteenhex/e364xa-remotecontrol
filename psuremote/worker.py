"""The thread that owns the instrument.

All instrument I/O happens on one thread, for two reasons. A serial link
cannot have two conversations at once, and a 9600 baud round trip takes long
enough (a full poll is seven or eight queries) that doing it on the UI thread
would make the TUI visibly stutter.

The UI hands commands over with `submit` and gets results back through
callbacks. Those callbacks are invoked *on this thread*, so whatever the UI
wires up to them has to be thread-safe -- the Textual app turns each one
straight into a `post_message`.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from .instrument import PowerSupply, Snapshot
from .scpi import LinkError

#: Distinct from None, which means "connected and healthy". Without it the
#: first healthy poll looks like no change from the initial state and the UI
#: never hears that the link came up.
_FAULT_UNKNOWN = "<unknown>"

#: A queued operation. Gets the supply and the latest snapshot, may return a
#: line to log.
Command = Callable[[PowerSupply, Snapshot], "str | None"]


@dataclass
class _Job:
    label: str
    run: Command


class InstrumentWorker:

    def __init__(
        self,
        supply: PowerSupply,
        poll_interval: float = 0.35,
        slow_every: int = 6,
        on_snapshot: Callable[[Snapshot], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
        on_fault: Callable[[str | None], None] | None = None,
    ):
        self.supply = supply
        self.poll_interval = poll_interval
        self.slow_every = max(1, slow_every)
        self.on_snapshot = on_snapshot
        self.on_log = on_log
        self.on_fault = on_fault

        self.snapshot = Snapshot()
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fault: str | None = _FAULT_UNKNOWN
        self._polls = 0

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._main, name="instrument", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        # Unblock the queue wait so shutdown is immediate.
        self._queue.put(_Job("stop", lambda supply, snapshot: None))
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def submit(self, label: str, run: Command) -> None:
        """Queue an operation. Returns immediately."""
        self._queue.put(_Job(label, run))

    # -- callbacks ---------------------------------------------------------

    def _log(self, label: str, message: str) -> None:
        # Label and text stay separate all the way to the widget: instrument
        # error strings are arbitrary text and must never be parsed as markup.
        if self.on_log is not None:
            self.on_log(label, message)

    def _report_fault(self, message: str | None) -> None:
        """Announce a link problem, but only when the situation changes.

        A supply that has been switched off would otherwise produce an
        identical error line several times a second.
        """
        if message == self._fault:
            return
        was_faulted = self._fault not in (None, _FAULT_UNKNOWN)
        self._fault = message
        if self.on_fault is not None:
            self.on_fault(message)
        if message:
            self._log("link", message)
        elif was_faulted:
            # Only worth saying if it had actually broken; the first healthy
            # poll of the session has nothing to recover from.
            self._log("link", "recovered")

    def _publish(self, update: Snapshot) -> None:
        self.snapshot = self.snapshot.merge(update)
        if self.on_snapshot is not None:
            self.on_snapshot(self.snapshot)

    # -- the loop ----------------------------------------------------------

    def _main(self) -> None:
        try:
            try:
                self.supply.prepare()
                self._log("ident", self.supply.idn or self.supply.model.name)
                self._log("link", self.supply.link.description)
            except LinkError as exc:
                self._report_fault(str(exc))
                # Still poll: the user may fix the cable or the mode and the
                # UI should come to life rather than needing a restart.

            next_poll = monotonic()
            while not self._stop.is_set():
                wait = max(0.0, next_poll - monotonic())
                job: _Job | None
                try:
                    job = self._queue.get(timeout=wait)
                except queue.Empty:
                    job = None

                if self._stop.is_set():
                    break

                if job is not None:
                    self._run_job(job)
                    # Drain anything queued behind it -- holding a step key
                    # produces a burst, and we want the last value, not a
                    # backlog played out one poll at a time.
                    while True:
                        try:
                            self._run_job(self._queue.get_nowait())
                        except queue.Empty:
                            break
                    self._poll(full=True)
                else:
                    self._poll(full=self._polls % self.slow_every == 0)
                    self._polls += 1

                next_poll = monotonic() + self.poll_interval
        finally:
            self._shutdown()

    def _run_job(self, job: _Job) -> None:
        try:
            message = job.run(self.supply, self.snapshot)
        except LinkError as exc:
            self._report_fault(str(exc))
            return
        except (NotImplementedError, ValueError) as exc:
            self._log(job.label, str(exc))
            return
        if message:
            self._log(job.label, message)

    def _poll(self, full: bool) -> None:
        try:
            update = self.supply.read_fast()
            if full:
                update = update.merge(self.supply.read_slow())
        except LinkError as exc:
            self._report_fault(str(exc))
            return
        except ValueError as exc:
            self._report_fault(f"unexpected response: {exc}")
            return
        self._report_fault(None)
        self._publish(update)

    def _shutdown(self) -> None:
        try:
            self.supply.release()
        except LinkError:
            # Nothing to be done if the link is already gone.
            pass
        finally:
            self.supply.link.close()
