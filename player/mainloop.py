"""The Player's one GLib main-loop policy: every source it adds, at a priority owned here.

GLib dispatches only the highest-priority ready sources in each iteration, and a repeating
timeout re-arms from the iteration's start. So a 33 ms tick that costs more than 33 ms, with
GDK paint (priority 120) behind it, is ready on every iteration and starves every source below
it. Control dispatch therefore runs ABOVE the tick and paint (refused at construction
otherwise), and the tick re-arms only after its work, with a gap, so it never runs back to
back. The tick plans what to show; a picture that changes over time is drawn on its display's
frame clock (`FrameFollower`), never at the tick's pace, and a change arriving from Display Host
is read from the display's own events (`DisplayEvents`), never polled.
tests/test_player_mainloop.py walks player/ and fails on any GLib source added outside this
module; tests/native_player_mainloop_harness.py holds these numbers to real GLib and Gdk.

Stdlib only: callers pass the GLib module, so importing this needs no native dependency.
"""

from __future__ import annotations

import logging
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
WAYLAND_EVENTS = 0
"""GDK_PRIORITY_EVENTS (G_PRIORITY_DEFAULT): GDK's Wayland event source, whose check reads the
display's socket into every event queue. A watch on that socket runs no higher: GLib skips the
check of lower-priority sources once a higher one is ready, so nothing would read it."""
GDK_PRIORITY_REDRAW = 120
"""GDK's paint (G_PRIORITY_HIGH_IDLE + 20), which every queue_render feeds."""
MIN_GAP_MS = 8
"""The least idle time a Tick leaves after its work, so lower-priority paint still runs."""


class DispatchRefused(ValueError):
    """The dispatcher's slots are all queued; the callback was not posted."""


class LoopLate(DispatchRefused):
    """Not posted: an earlier post was abandoned at its caller's deadline and the loop has
    not reached it yet, so the loop is late and a new post would only queue behind it."""


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


def frame_time(glib, frame_clock, clock: Callable[[], float] = time.monotonic) -> float:
    """A GdkFrameClock's frame time on `clock`'s timeline, in seconds. GDK counts microseconds of
    g_get_monotonic_time; one paired reading maps it, never an assumed shared epoch."""
    return clock() - (glib.get_monotonic_time() - frame_clock.get_frame_time()) / 1e6


class FrameFollower:
    """Calls `frame(at)` on each frame of `widget`'s frame clock (gtk_widget_add_tick_callback,
    paced by the display's refresh), `at` the frame time on `clock`'s timeline, from `follow()`
    until `frame` answers False (or raises) or stop(). `frame` queues the widget's render itself
    when the frame owes a draw; nothing here draws or times a change, and an unfollowed widget
    costs its frame clock nothing."""

    def __init__(self, glib, widget, frame: Callable[[float], bool], *,
                 clock: Callable[[], float] = time.monotonic):
        self.glib, self.widget = glib, widget
        self._frame, self._clock = frame, clock
        self._callback: int | None = None

    @property
    def following(self) -> bool:
        return self._callback is not None

    def follow(self) -> None:
        if self._callback is None:
            self._callback = self.widget.add_tick_callback(self._tick)

    def stop(self) -> None:
        if self._callback is not None:
            self.widget.remove_tick_callback(self._callback)
            self._callback = None

    def _tick(self, _widget, frame_clock) -> bool:
        keep = False
        try:
            keep = bool(self._frame(frame_time(self.glib, frame_clock, self._clock)))
        finally:
            if not keep:
                self._callback = None
        return keep


class DisplayEvents:
    """Calls `arrived()` on the main loop after each read of the Wayland display's socket `fd`
    (g_unix_fd_add, at WAYLAND_EVENTS): GDK's event source reads the socket in its check, before
    any source of the same iteration dispatches, so `arrived` finds every event read since it last
    ran queued on its own event queue (wl_display_dispatch_queue_pending). Nothing runs while no
    event arrives. Events read outside the main loop's poll (a roundtrip) wait for the next event;
    the planning tick still compares what they change. A raising `arrived` is logged and the
    watch kept: the next event calls it again."""

    def __init__(self, glib, fd: int, arrived: Callable[[], object], *,
                 priority: int = WAYLAND_EVENTS):
        if priority < WAYLAND_EVENTS:
            raise ValueError("display_events_priority")
        self.glib, self._arrived = glib, arrived
        self._source: int | None = glib.unix_fd_add_full(
            priority, fd, glib.IOCondition.IN, self._readable)

    def _readable(self, _fd, _condition) -> bool:
        try:
            self._arrived()
        except Exception:
            logging.getLogger(__name__).exception("display event handling failed")
        return True

    def stop(self) -> None:
        if self._source is not None:
            self.glib.source_remove(self._source)
            self._source = None


def watch_bus(bus) -> None:
    """Emit a GStreamer bus's messages as signals on the main loop, at BUS priority."""
    bus.add_signal_watch_full(BUS)


__all__ = ["BUS", "CONTROL", "DisplayEvents", "GDK_PRIORITY_REDRAW", "MIN_GAP_MS", "TICK",
           "DispatchRefused", "FrameFollower", "LatePosts", "LoopLate", "MainLoopDispatcher", "Tick",
           "WAYLAND_EVENTS", "frame_time", "watch_bus"]
