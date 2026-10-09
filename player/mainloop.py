"""The Player's one GLib main-loop policy: every source it adds, at a priority owned here.

GLib dispatches only the highest-priority ready sources in each iteration, and a repeating
timeout re-arms from the iteration's start. So a 33 ms tick that costs more than 33 ms, with
GDK paint (priority 120) behind it, is ready on every iteration and starves every source below
it. Control dispatch therefore runs ABOVE the tick and paint (refused at construction
otherwise), and the tick re-arms only after its work, with a gap, so it never runs back to
back. tests/test_player_mainloop.py walks player/ and fails on any GLib source added outside
this module; tests/native_player_mainloop_harness.py holds these numbers to real GLib and Gdk.

Stdlib only: callers pass the GLib module, so importing this needs no native dependency.
"""

from __future__ import annotations

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
GDK_PRIORITY_REDRAW = 120
"""GDK's paint (G_PRIORITY_HIGH_IDLE + 20), which every queue_render feeds."""
MIN_GAP_MS = 8
"""The least idle time a Tick leaves after its work, so lower-priority paint still runs."""


class DispatchRefused(ValueError):
    """The dispatcher's slots are all queued; the callback was not posted."""


class MainLoopDispatcher:
    """At most `slots` queued callbacks at `priority`; the caller awaits each, never blocking GLib.

    `priority` must be numerically below both TICK and GDK_PRIORITY_REDRAW, so neither an
    overrunning tick nor continuous paint can starve a handoff."""

    def __init__(self, glib, slots: int = 4, *, priority: int = CONTROL):
        if not (priority < TICK and priority < GDK_PRIORITY_REDRAW):
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


class Tick:
    """Runs `work` every `interval_ms`, re-armed one-shot after the work finishes.

    The next run is max(MIN_GAP_MS, interval_ms - elapsed) after the work ends, so an overrun
    leaves a gap instead of running back to back. `work` returning False, raising, or stop()
    ends the tick."""

    def __init__(self, glib, interval_ms: int, work: Callable[[], object], *,
                 priority: int = TICK, clock: Callable[[], float] = time.monotonic):
        if not CONTROL < priority < GDK_PRIORITY_REDRAW:
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


def watch_bus(bus) -> None:
    """Emit a GStreamer bus's messages as signals on the main loop, at BUS priority."""
    bus.add_signal_watch_full(BUS)


__all__ = ["BUS", "CONTROL", "GDK_PRIORITY_REDRAW", "MIN_GAP_MS", "TICK", "DispatchRefused",
           "MainLoopDispatcher", "Tick", "watch_bus"]
