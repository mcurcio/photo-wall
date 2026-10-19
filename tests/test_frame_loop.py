"""The screens' frame turns on GLib (player/mainloop.py `FrameLoop`, render redesign S3 bead B5): a
turn runs after a read of the screens' connection, after a wake (coalesced) and at the time the last
turn answered; never otherwise, and every source at FRAME priority."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from player.mainloop import FRAME, FrameLoop


class FakeGLib:
    """Records every source added (kind, priority, interval, callback) and every removal."""
    IOCondition = SimpleNamespace(IN=1)

    def __init__(self) -> None:
        self.sources: dict[int, tuple[str, int, int | None, object]] = {}
        self.removed: list[int] = []
        self._next = 0

    def _add(self, kind, priority, interval, callback) -> int:
        self._next += 1
        self.sources[self._next] = (kind, priority, interval, callback)
        return self._next

    def unix_fd_add_full(self, priority, fd, condition, callback):
        assert condition == self.IOCondition.IN
        return self._add("fd", priority, None, callback)

    def idle_add(self, callback, *, priority):
        return self._add("idle", priority, None, callback)

    def timeout_add(self, interval, callback, *, priority):
        return self._add("timeout", priority, interval, callback)

    def source_remove(self, source):
        self.removed.append(source)
        del self.sources[source]

    def of(self, kind: str) -> list[int]:
        return [source for source, (found, *_rest) in self.sources.items() if found == kind]

    def dispatch(self, source: int) -> None:
        """Run `source` as GLib would: a False answer removes it."""
        kind, _priority, _interval, callback = self.sources[source]
        keep = callback(7, self.IOCondition.IN) if kind == "fd" else callback()
        if not keep and source in self.sources:
            del self.sources[source]


class Turns:
    def __init__(self, answer=None) -> None:
        self.count = 0
        self.answer = answer

    def __call__(self):
        self.count += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def loop(answer=None, now: float = 10.0):
    glib, turns = FakeGLib(), Turns(answer)
    return glib, turns, FrameLoop(glib, 7, turns, clock=lambda: now)


def test_every_source_is_at_frame_priority_and_the_read_stays_armed():
    glib, turns, frames = loop(answer=10.5)
    [watch] = glib.of("fd")
    glib.dispatch(watch)
    frames.wake()
    assert {priority for _kind, priority, _interval, _callback in glib.sources.values()} == {FRAME}
    assert watch in glib.sources                        # level: kept armed after a read


def test_a_read_runs_one_turn():
    glib, turns, _frames = loop()
    assert turns.count == 0                             # nothing runs on its own
    glib.dispatch(glib.of("fd")[0])
    assert turns.count == 1


def test_wakes_before_the_idle_run_one_turn():
    glib, turns, frames = loop()
    frames.wake()
    frames.wake()
    assert len(glib.of("idle")) == 1
    glib.dispatch(glib.of("idle")[0])
    assert turns.count == 1 and glib.of("idle") == []
    frames.wake()
    glib.dispatch(glib.of("fd")[0])                     # a read serves the pending wake too
    assert turns.count == 2 and glib.of("idle") == []


def test_the_answered_time_arms_one_timeout_replaced_by_the_next_answer():
    glib, turns, _frames = loop(answer=10.25)
    glib.dispatch(glib.of("fd")[0])
    [first] = glib.of("timeout")
    assert glib.sources[first][2] == 250
    turns.answer = 9.0                                  # already due: at once
    glib.dispatch(glib.of("fd")[0])
    [second] = glib.of("timeout")
    assert second != first and first in glib.removed and glib.sources[second][2] == 0
    glib.dispatch(second)                               # the timeout runs a turn
    assert turns.count == 3
    turns.answer = None
    glib.dispatch(glib.of("fd")[0])
    assert glib.of("timeout") == []                     # None: only an event runs the next


def test_a_raising_turn_is_logged_and_arms_nothing(caplog):
    glib, turns, _frames = loop(answer=10.5)
    glib.dispatch(glib.of("fd")[0])
    assert len(glib.of("timeout")) == 1
    turns.answer = RuntimeError("turn")
    with caplog.at_level(logging.ERROR, logger="player.mainloop"):
        glib.dispatch(glib.of("fd")[0])
    assert glib.of("timeout") == [] and glib.of("fd") != []
    assert any(record.exc_info for record in caplog.records)


def test_stop_removes_every_source():
    glib, turns, frames = loop(answer=11.0)
    glib.dispatch(glib.of("fd")[0])
    frames.wake()
    assert {kind for kind, *_rest in glib.sources.values()} == {"fd", "idle", "timeout"}
    frames.stop()
    assert glib.sources == {}
    frames.wake()
    assert glib.sources == {} and turns.count == 1
