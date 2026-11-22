"""The scheduler ticks when something is due, not every second (decision 0034, class F: timers where
events exist; slice S3b part 1), and Central's hub looks wake on events.

The loop (`central/scheduler.py` `run_scheduler`) is driven with a ManualClock and a rest that
stands in for time, over a fake coordinator (unit) and over the real app's Scheduler and Coordinator
on PostgreSQL (db): an idle wall rests SCHEDULER_IDLE_SECONDS, a Program window opening 30 s on
wakes it then, a Player with layers wakes it at the next renewal boundary, a Scene saved through the
API wakes the app's own loop at once, and retention runs every RETENTION_EVERY, never in a tick.
An Install written by the console wakes FleetHub's look through the worker's LISTEN.

Mutation probes: (1) return `now + 1` from `Scheduler.tick` -> the idle test counts 600 ticks;
(2) drop `runtime.next_event_at` from `Coordinator._advance_runtime` -> the window opens at the
60 s ceiling; (3) call `retain` from `tick` -> the cadence test counts a retention per tick;
(6) drop `notify_releases_in` from `PgPlayerVersions._set_in` -> FleetHub waits for its ceiling;
(7) `PgStateWakes.wake` drops a wake while no `changed` waits -> the early-wake test waits 60 s.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient
from support.house_time import UTC_HOUSE, set_house, window_program
from test_planning_isolation import players, scene
from test_player_versions import _install
from test_player_versions import wall as versions_wall  # noqa: F401 - the Install test's fixture
from test_registry import ADMIN

from central.app import create_app
from central.coordination import Advance
from central.db import Database
from central.infra.central_state import PgStateWakes
from central.infra.display_store import DisplayWakes
from central.node_bus_wiring import NodeBus, RoomLightsWall
from central.runtime import Scene
from central.scheduler import (
    RETENTION_EVERY,
    SCHEDULER_IDLE_SECONDS,
    SCHEDULER_MIN_SECONDS,
    Scheduler,
    run_scheduler,
)
from contracts.time import ManualClock

BEARER = {"Authorization": "Bearer " + ADMIN}


class Idle:
    """A coordinator with nothing due (`wake_at` None), or due at `wake_at`."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.wake_at: float | None = None

    def advance(self) -> Advance:
        projection = type("Projection", (), {"measurements": ()})()
        return Advance(projection=projection, failures=(), wake_at=self.wake_at)


class NoWakes:
    """No commit ever wakes the loop."""

    def count(self) -> int:
        return 0

    async def after(self, seen: int) -> int:
        await asyncio.Event().wait()
        return seen


class Stop(Exception):
    pass


def drive(scheduler: Scheduler, clock: ManualClock, seconds: float) -> list[tuple[str, float]]:
    """Run the loop for `seconds` of the ManualClock's time; each rest advances the clock. Answers
    what happened, in order: ("tick", at) and ("rest", seconds) entries."""
    log: list[tuple[str, float]] = []
    end = clock.monotonic() + seconds
    tick = scheduler.tick

    def ticked() -> float:
        log.append(("tick", clock.utc()))
        return tick()

    scheduler.tick = ticked  # type: ignore[method-assign]

    async def resting(woken, rest: float) -> None:
        woken.close()
        log.append(("rest", rest))
        if clock.monotonic() + rest > end:
            raise Stop
        clock.advance(rest)

    with pytest.raises(Stop):
        asyncio.run(run_scheduler(scheduler, NoWakes(), resting=resting))
    return log


def ticks(log: list[tuple[str, float]]) -> list[float]:
    return [at for kind, at in log if kind == "tick"]


def test_an_idle_wall_rests_the_idle_ceiling_between_ticks():
    clock = ManualClock(1000)
    scheduler = Scheduler(Idle(clock), lambda _requests: 0, clock, status=lambda: 0,
                          retentions={})
    log = drive(scheduler, clock, 600)
    assert ticks(log) == [1000 + 60 * n for n in range(11)]
    assert {rest for kind, rest in log if kind == "rest"} == {SCHEDULER_IDLE_SECONDS}


def test_retention_runs_every_retention_period_never_in_a_tick():
    clock = ManualClock(1000)
    retained: list[float] = []
    scheduler = Scheduler(Idle(clock), lambda _requests: 0, clock, status=lambda: 0,
                          retentions={"node-retention": lambda: retained.append(clock.utc())})
    log = drive(scheduler, clock, 3 * RETENTION_EVERY)
    assert retained == [1000 + RETENTION_EVERY * n for n in range(4)]
    assert len(ticks(log)) == 3 * RETENTION_EVERY / SCHEDULER_IDLE_SECONDS + 1
    scheduler.tick()
    assert len(retained) == 4   # a tick alone never retains
    assert scheduler.health().steps["node-retention"].status == "ok"


def test_a_failing_step_tries_again_after_the_shortest_rest():
    clock = ManualClock(1000)

    def failing(_requests) -> int:
        raise RuntimeError("private measurement failure")

    scheduler = Scheduler(Idle(clock), failing, clock, status=lambda: 0, retentions={})
    assert scheduler.tick() == 1000 + SCHEDULER_MIN_SECONDS


def test_central_states_look_takes_a_wake_that_came_before_it_waited():
    """A commit's NOTIFY that reaches Central's hub session before its look first waits (or while
    it reads) is looked at at once, never at the CENTRAL_STATE_LOOK_SECONDS ceiling (the install
    tracer's lost wake: an Install committed while the session's first read ran)."""
    async def run() -> None:
        wakes = PgStateWakes(look_seconds=60)
        wakes.wake()   # before any `changed`: no loop known yet
        await asyncio.wait_for(wakes.changed(), 1)
        waiting = asyncio.ensure_future(wakes.changed())
        await asyncio.sleep(0.05)
        assert not waiting.done()   # the wake was taken once
        await asyncio.to_thread(wakes.wake)   # from another thread, while it waits
        await asyncio.wait_for(waiting, 1)
        wakes.wake()   # while the caller reads, between two waits
        await asyncio.wait_for(wakes.changed(), 1)
    asyncio.run(run())


def test_central_states_look_waits_its_ceiling_with_no_wake():
    async def run() -> None:
        wakes = PgStateWakes(look_seconds=0.1)
        started = time.monotonic()
        await wakes.changed()
        assert time.monotonic() - started >= 0.09
    asyncio.run(run())


# Through the real app's Scheduler and Coordinator on PostgreSQL.

@pytest.fixture
def app(registry):
    set_house(registry.db, UTC_HOUSE)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app):
        yield app


@pytest.mark.db
def test_an_idle_wall_with_no_run_rests_the_idle_ceiling(registry, app):
    log = drive(app.state.scheduler, registry.clock, 150)
    assert ticks(log) == [1000, 1060, 1120]


@pytest.mark.db
def test_a_program_window_wakes_the_scheduler_when_it_opens(registry, app):
    registry.clock.advance(50)                                      # 1050
    runtime = app.state.coordinator.runtime
    runtime.command("set_scene", Scene(scene_id="show", loop=True), by="console")
    runtime.command_current("create_program", window_program("evening", "show", 1080, 1200),
                            by="console")
    log = drive(app.state.scheduler, registry.clock, 40)
    assert ticks(log)[:2] == [1050, 1080]                          # 30 s on, not the ceiling
    state = runtime.read().export_state()
    assert [run["started_at"] for run in state["runs"].values()
            if run.get("program_id") == "evening"] == [1080]


@pytest.mark.db
def test_a_player_with_layers_wakes_the_scheduler_at_the_next_renewal_boundary(registry, app):
    (player,) = players(registry, 1)
    runtime = app.state.coordinator.runtime
    runtime.command("set_scene", scene("black", "frame-0", 3600), by="console")
    runtime.command("activate", "black", "black-1", 1000, by="console")
    assert app.state.coordinator.limits.renewal_seconds == 30
    assert app.state.scheduler.tick() <= 1020          # its cue's grant may come first
    registry.clock.advance(10)                          # 1010: the cue has started
    assert app.state.scheduler.tick() == 1020           # the next renewal boundary


@pytest.mark.db
def test_a_scene_saved_through_the_api_wakes_the_loop_at_once(registry):
    """The app's own loop (real rests, the real LISTEN): idle, it rests the 60 s ceiling, and a
    Scene saved through the API ticks it within a few seconds."""
    set_house(registry.db, UTC_HOUSE)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=True)
    ticked: list[float] = []
    tick = app.state.scheduler.tick

    def counted() -> float:
        ticked.append(time.monotonic())
        return tick()

    app.state.scheduler.tick = counted
    with TestClient(app) as client:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (not ticked or time.monotonic() - ticked[-1] < 3):
            time.sleep(.1)                                         # settled: its own writes woke it
        assert ticked and time.monotonic() - ticked[-1] >= 3, "the loop settles into its rest"
        before = len(ticked)
        saved = client.put("/v1/operator/scenes/woken", headers=BEARER, json={
            "scene_id": "woken", "revision": 1, "cycle_seconds": 30, "loop": True})
        assert saved.status_code == 200, saved.text
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(ticked) == before:
            time.sleep(.05)
        assert len(ticked) > before, "the save's commit wakes the loop"


# The worker's LISTEN wakes FleetHub's look.

class FleetLook:
    """FleetHub as NodeBus sees it: it runs until stopped and counts its wakes."""

    wall = None

    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()


class Links:
    """A link supervisor that runs until stopped."""

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def track(self, _serials) -> None:
        pass


class Documents:
    def current(self):
        raise AssertionError("no WALL here: the document is never read")


@pytest.mark.db
def test_an_install_wakes_fleethubs_look_through_the_workers_listen(registry, versions_wall):  # noqa: F811
    client, _world, _uploads, _upstream = versions_wall
    fleet = FleetLook()

    async def hub_session(stop: asyncio.Event) -> None:
        await stop.wait()

    bus = NodeBus(Database(registry.db.dsn, application_name="photo-wall-test"), fleet, (Links(),),
                  DisplayWakes(), RoomLightsWall(Documents()), hub_session=hub_session,
                  state_wakes=PgStateWakes())
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    worker = threading.Thread(target=lambda: loop.run_until_complete(bus.run(stop)))
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and fleet.wakes == 0:
            time.sleep(.05)
        assert fleet.wakes >= 1, "the LISTEN connects (and wakes once for what it missed)"
        time.sleep(.5)
        before = fleet.wakes
        assert _install(client, "pi-a", "v2.0.0").status_code == 200
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and fleet.wakes == before:
            time.sleep(.05)
        assert fleet.wakes > before, "the Install's commit wakes FleetHub's look"
    finally:
        loop.call_soon_threadsafe(stop.set)
        worker.join(30)
