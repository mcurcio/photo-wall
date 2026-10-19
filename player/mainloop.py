"""The Player's one GLib main-loop policy: every source it adds, at a priority owned here.

GLib dispatches only the highest-priority ready sources in each iteration, and a repeating
timeout re-arms from the iteration's start. So a 33 ms tick that costs more than 33 ms is ready on
every iteration and starves every source below it. The order is CONTROL < FRAME < TICK, each
refused at construction otherwise: control dispatch runs first, then the screens' frame turns
(`FrameLoop`, on reads of the screens' connection, wakes and the time the last turn answered),
then the planning tick, which re-arms only after its work, with a gap, so it never runs back to
back. The tick plans what to show; a picture that changes over time is committed by the frame
turns, never at the tick's pace.
tests/test_player_mainloop.py walks player/ and fails on any GLib source added outside this
module and holds the order.

Stdlib only: callers pass the GLib module, so importing this needs no native dependency.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future

CONTROL = -100
"""GLib.PRIORITY_HIGH: thread-to-main handoffs (control, readiness, probes)."""
TICK = 0
"""GLib.PRIORITY_DEFAULT: the Player tick and the frame renewer."""
BUS = 0
"""GLib.PRIORITY_DEFAULT: GStreamer bus watches."""
FRAME = -50
"""Between CONTROL and TICK: the screens' frame turns (render redesign S3, `FrameLoop`), so a fade's
next commit never waits behind a planning tick that is ready in the same iteration, and control
handoffs still run first."""
MIN_GAP_MS = 8
"""The least idle time a Tick leaves after its work, so lower-priority sources still run."""


class DispatchRefused(ValueError):
    """The dispatcher's slots are all queued; the callback was not posted."""


class LoopLate(DispatchRefused):
    """Not posted: an earlier post was abandoned at its caller's deadline and the loop has
    not reached it yet, so the loop is late and a new post would only queue behind it."""


class MainLoopDispatcher:
    """At most `slots` queued callbacks at `priority`; the caller awaits each, never blocking GLib.

    `priority` must be numerically below FRAME (so below TICK too), so neither an overrunning tick
    nor a run of frame turns can starve a handoff."""

    def __init__(self, glib, slots: int = 4, *, priority: int = CONTROL):
        if not priority < FRAME:
            raise ValueError("dispatch_priority")
        self.glib, self.priority = glib, priority
        self._slots = threading.BoundedSemaphore(slots)

    def lane(self, slots: int) -> MainLoopDispatcher:
        """A sibling at the same priority with its own slots: it never takes ours."""
        return MainLoopDispatcher(self.glib, slots, priority=self.priority)

    def __call__(self, callback: Callable) -> Future:
        future = Future()
        if not self._slots.acquire(blocking=False):
            future.set_exception(DispatchRefused("dispatch_capacity"))
            return future

        def run():
            try:
                # A cancelled future (the caller's deadline passed) never runs its callback.
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(callback())
                    except Exception as error:
                        future.set_exception(error)
            finally:
                self._slots.release()
            return False

        self.glib.idle_add(run, priority=self.priority)
        return future


class LatePosts:
    """Posts through a dispatcher that a caller may abandon at its deadline.

    An abandoned post stays queued (GLib cannot unqueue it) but never runs its callback; it
    counts as unreached until the loop reaches it. While any is unreached the loop is late:
    `post` refuses at once with LoopLate instead of queueing another, so a blocked loop holds
    at most the posts in flight when it blocked, never fills the dispatcher's slots, and the
    late state clears itself when the loop runs again (FIFO at one priority)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._unreached = 0

    @property
    def late(self) -> bool:
        """An abandoned post is still queued: the loop has not run since it was posted."""
        with self._lock:
            return self._unreached > 0

    def post(self, dispatcher: Callable[[Callable], Future],
             callback: Callable) -> tuple[Future, Callable[[], bool]]:
        """(future, abandon). abandon() returns False once the callback has started (it
        then completes); otherwise the callback never runs and the post counts as late."""
        with self._lock:
            if self._unreached:
                refused = Future()
                refused.set_exception(LoopLate("main_loop_late"))
                return refused, lambda: False
        state = {"started": False, "abandoned": False}

        def run():
            with self._lock:
                if state["abandoned"]:
                    self._unreached -= 1
                    return None
                state["started"] = True
            return callback()

        def abandon() -> bool:
            with self._lock:
                if state["started"] or state["abandoned"]:
                    return False
                state["abandoned"] = True
                self._unreached += 1
                return True

        return dispatcher(run), abandon


class Tick:
    """Runs `work` every `interval_ms`, re-armed one-shot after the work finishes.

    The next run is max(MIN_GAP_MS, interval_ms - elapsed) after the work ends, so an overrun
    leaves a gap instead of running back to back. `work` returning False, raising, or stop()
    ends the tick. `priority` must be numerically above CONTROL and FRAME, so a tick never runs
    before a control handoff or a frame turn ready in the same iteration."""

    def __init__(self, glib, interval_ms: int, work: Callable[[], object], *,
                 priority: int = TICK, clock: Callable[[], float] = time.monotonic):
        if not (CONTROL < priority and FRAME < priority):
            raise ValueError("tick_priority")
        if interval_ms < 1:
            raise ValueError("tick_interval")
        self.glib, self.interval_ms, self.priority = glib, interval_ms, priority
        self._work, self._clock = work, clock
        self._source = None
        self._stopped = False
        self._arm(interval_ms)

    def _arm(self, delay_ms: int) -> None:
        self._source = self.glib.timeout_add(delay_ms, self._fire, priority=self.priority)

    def _fire(self) -> bool:
        self._source = None
        if self._stopped:
            return False
        started = self._clock()
        keep = False
        try:
            keep = self._work() is not False
        finally:
            if keep and not self._stopped:
                elapsed_ms = (self._clock() - started) * 1000
                self._arm(max(MIN_GAP_MS, int(self.interval_ms - elapsed_ms)))
        return False

    def stop(self) -> None:
        self._stopped = True
        if self._source is not None:
            self.glib.source_remove(self._source)
            self._source = None


class FrameLoop:
    """Runs `turn()` on the main loop at FRAME priority (render redesign S3, run ledger
    .claude/runs/render-planes/s3-page.md, bead B5): after every read of the screens' Wayland
    connection `fd` (g_unix_fd_add: the compositor spoke, a commit's presentation feedback or a
    release, so a refresh went by), after `wake()` (a video's new frame, a new composition), and at
    the time `turn()` last answered (a GLib timeout armed for it: a fence that signals or a fade's
    next frame with no event to announce it); never otherwise. Coalesced: one turn runs for any
    number of wakes and reads that arrive before it. `turn()` answers when the next turn is owed with
    no event (seconds on `clock`'s timeline; None: only on an event); a raising `turn` is logged and
    the loop goes on as if it answered None.

    The composition root's `turn` reads the connection, presents and then finishes
    (player/renderer_root.py); nothing here knows the renderer."""

    def __init__(self, glib, fd: int, turn: Callable[[], float | None], *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """The read watch is level-triggered and kept armed: the turn reads the connection, so it
        is not ready again until the compositor speaks."""
        self.glib, self._turn, self._clock = glib, turn, clock
        self._idle: int | None = None
        self._timer: int | None = None
        self._stopped = False
        self._watch: int | None = glib.unix_fd_add_full(FRAME, fd, glib.IOCondition.IN, self._readable)

    def wake(self) -> None:
        """Run a turn soon (an idle at FRAME priority, once however many wakes come first)."""
        if not self._stopped and self._idle is None:
            self._idle = self.glib.idle_add(self._woken, priority=FRAME)

    def stop(self) -> None:
        """Remove every source this loop added; no turn runs after it."""
        self._stopped = True
        for source in (self._watch, self._idle, self._timer):
            if source is not None:
                self.glib.source_remove(source)
        self._watch = self._idle = self._timer = None

    def _readable(self, _fd, _condition) -> bool:
        self._run()
        return not self._stopped

    def _woken(self) -> bool:
        self._idle = None
        self._run()
        return False

    def _timed(self) -> bool:
        self._timer = None
        self._run()
        return False

    def _run(self) -> None:
        """One turn, serving every wake pending before it; then the timeout for its answer."""
        if self._stopped:
            return
        if self._idle is not None:
            self.glib.source_remove(self._idle)
            self._idle = None
        try:
            at = self._turn()
        except Exception:
            logging.getLogger(__name__).exception("frame turn failed")
            at = None
        if self._timer is not None:
            self.glib.source_remove(self._timer)
            self._timer = None
        if at is not None and not self._stopped:
            delay_ms = max(0, math.ceil((at - self._clock()) * 1000))
            self._timer = self.glib.timeout_add(delay_ms, self._timed, priority=FRAME)


def watch_bus(bus) -> None:
    """Emit a GStreamer bus's messages as signals on the main loop, at BUS priority."""
    bus.add_signal_watch_full(BUS)


__all__ = ["BUS", "CONTROL", "FRAME", "MIN_GAP_MS", "TICK", "DispatchRefused", "FrameLoop", "LatePosts",
           "LoopLate", "MainLoopDispatcher", "Tick", "watch_bus"]
