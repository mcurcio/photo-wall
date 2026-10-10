"""G8: one module owns every GLib source the Player adds, and refuses a starvable priority.

The real-GLib proof (control answers on a saturated loop; a static photo is not redrawn every
tick) is tests/native_player_mainloop_harness.py, run by scripts/run_display_harness.py.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from player.mainloop import (
    BUS,
    CONTROL,
    GDK_PRIORITY_REDRAW,
    MIN_GAP_MS,
    TICK,
    MainLoopDispatcher,
    Tick,
    watch_bus,
)

PLAYER = Path(__file__).resolve().parents[1] / "player"
OWNER = PLAYER / "mainloop.py"
# Every GLib/GStreamer/GTK name that adds a main-loop source. A source added anywhere else has
# a priority no one chose, which is how the control queue starved (G8). The guard flags any
# REFERENCE to one (attribute, name, import, or a getattr string), not only a direct call, so an
# alias (`add = GLib.idle_add`), `from GLib import timeout_add`, a source object
# (`GLib.Idle().attach()`), a context invoke or a widget tick callback is refused too.
SOURCE_NAMES = frozenset({
    "idle_add", "idle_add_full", "timeout_add", "timeout_add_full", "timeout_add_seconds",
    "timeout_add_seconds_full", "add_signal_watch", "add_signal_watch_full", "io_add_watch",
    "io_add_watch_full", "child_watch_add", "child_watch_add_full", "unix_signal_add",
    "unix_signal_add_full", "unix_fd_add", "unix_fd_add_full", "add_watch", "add_watch_full",
    "Idle", "Timeout", "Source", "invoke", "invoke_full", "add_tick_callback", "threads_add_idle",
    "threads_add_idle_full", "threads_add_timeout", "threads_add_timeout_full"})


def _source_name(name: str | None) -> bool:
    return name is not None and (name in SOURCE_NAMES or name.endswith("_source_new"))


def stray_sources(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]
        else:
            continue
        found += [f"{path.name}:{node.lineno} {name}" for name in names if _source_name(name)]
    return found


def test_no_glib_source_is_added_outside_the_main_loop_module():
    assert OWNER.exists()
    strays = [found for path in sorted(PLAYER.rglob("*.py")) if path != OWNER
              for found in stray_sources(path)]
    assert strays == []


def test_the_guard_sees_a_stray_source(tmp_path):
    stray = tmp_path / "stray.py"
    stray.write_text("from gi.repository import GLib\nGLib.idle_add(print)\n"
                     "s = GLib.timeout_source_new(5)\nbus.add_signal_watch()\n")
    assert stray_sources(stray) == ["stray.py:2 idle_add", "stray.py:3 timeout_source_new",
                                    "stray.py:4 add_signal_watch"]


@pytest.mark.parametrize("source", [
    "add = GLib.idle_add\nadd(print)",
    "from gi.repository.GLib import timeout_add as later\nlater(5, print)",
    "GLib.Idle().attach(None)",
    "GLib.MainContext.default().invoke_full(200, print)",
    "area.add_tick_callback(print)",
    "getattr(GLib, 'idle_add')(print)",
])
def test_the_guard_sees_an_aliased_or_indirect_source(tmp_path, source):
    stray = tmp_path / "stray.py"
    stray.write_text(source + "\n")
    assert stray_sources(stray) != []


class FakeGLib:
    def __init__(self):
        self.added: list[tuple[str, int, int, object]] = []
        self.removed: list[int] = []

    def idle_add(self, callback, *, priority):
        self.added.append(("idle", 0, priority, callback))
        return len(self.added)

    def timeout_add(self, interval, callback, *, priority):
        self.added.append(("timeout", interval, priority, callback))
        return len(self.added)

    def source_remove(self, source):
        self.removed.append(source)


@pytest.mark.parametrize("priority", [200, GDK_PRIORITY_REDRAW, TICK, 1])
def test_a_dispatcher_below_the_tick_or_paint_is_refused_at_construction(priority):
    # 200 is GLib.PRIORITY_DEFAULT_IDLE: the priority that starved control on origin/main.
    with pytest.raises(ValueError, match="dispatch_priority"):
        MainLoopDispatcher(FakeGLib(), priority=priority)


def test_dispatcher_and_its_lanes_post_at_control_priority():
    glib = FakeGLib()
    dispatcher = MainLoopDispatcher(glib)
    dispatcher(lambda: None)
    dispatcher.lane(1)(lambda: None)
    assert [priority for _, _, priority, _ in glib.added] == [CONTROL, CONTROL]


@pytest.mark.parametrize("priority", [CONTROL, GDK_PRIORITY_REDRAW, 200])
def test_a_tick_outside_control_and_paint_is_refused(priority):
    with pytest.raises(ValueError, match="tick_priority"):
        Tick(FakeGLib(), 33, lambda: None, priority=priority)


def test_an_overrunning_tick_rearms_after_its_work_with_a_gap():
    glib = FakeGLib()
    now = [0.0]
    cost = [0.040]

    def work():
        now[0] += cost[0]

    tick = Tick(glib, 33, work, clock=lambda: now[0])
    assert glib.added[-1][:3] == ("timeout", 33, TICK)
    assert glib.added[-1][3]() is False             # one-shot: GLib drops it, Tick re-arms
    assert glib.added[-1][1] == MIN_GAP_MS          # 40 ms of work in a 33 ms tick: a gap
    cost[0] = 0.010
    glib.added[-1][3]()
    assert glib.added[-1][1] == 23                  # under budget: the rest of the interval
    tick.stop()
    assert glib.removed == [len(glib.added)]
    count = len(glib.added)
    glib.added[-1][3]()                             # a stopped tick never runs or re-arms
    assert len(glib.added) == count and now[0] == pytest.approx(0.050)


def test_a_tick_whose_work_returns_false_or_raises_ends():
    glib = FakeGLib()
    Tick(glib, 33, lambda: False)
    glib.added[-1][3]()
    assert len(glib.added) == 1

    def fail():
        raise RuntimeError("tick")

    glib = FakeGLib()
    Tick(glib, 33, fail)
    with pytest.raises(RuntimeError):
        glib.added[-1][3]()
    assert len(glib.added) == 1


def test_a_bus_watch_runs_at_tick_priority_never_below_paint():
    """A GStreamer bus watch at the idle default (200) would sit behind continuous paint, so an
    EOS or error would wait on the display; watch_bus pins it at BUS, beside the tick."""
    class Bus:
        priorities = []

        def add_signal_watch_full(self, priority):
            self.priorities.append(priority)
    bus = Bus()
    watch_bus(bus)
    assert bus.priorities == [BUS] and BUS == TICK
