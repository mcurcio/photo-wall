"""The Runtime is saved only when it changed apart from its processed instant (decision 0034, slice
S1, classes A and D), through the real app, its Coordinator and its Scheduler on PostgreSQL.

Before: every scheduler tick rewrote runtime_state (49 rows a minute in production), and its
planning and settle passes row-locked every Player's session, Frames and bindings, so each tick took
a transaction id, flushed the WAL and woke every Player socket (central/infra/player_state_wakes.py
notifies only a transaction that wrote). Now an idle tick writes nothing and wakes nobody, a change
still saves the whole state with `now`, and Settings › House saves the processed instant before it
moves a Program's windows, so none is admitted backdated (R10).

Mutation probes: (A1) remove the `_apart_from_now` comparison in `RuntimeStore.edit` (always save)
-> the idle tick and the only-now edit fail; (A2) save the snapshot without `now` -> the changed
row's `now` is stale; (A3) delete the `save_processed_in` call in `PgHouse.save_house_settings` ->
the moved window is admitted at 11:30; (D1) restore `FOR SHARE` in `active_sessions_in`, or pass
`lock=True` in `Coordinator._configurations` -> the idle tick notifies."""
from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient
from support.house_time import UTC_HOUSE, at, daily, set_house, window_program
from test_planning_isolation import players
from test_registry import ADMIN

from central.app import create_app
from central.infra.player_state_wakes import PLAYER_STATE_CHANNEL
from central.runtime import Program, Scene
from central.runtime_store import RuntimeStore

pytestmark = pytest.mark.db

BEARER = {"Authorization": "Bearer " + ADMIN}


@pytest.fixture
def wall(registry):
    set_house(registry.db, UTC_HOUSE)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        yield app, client


@contextmanager
def listening(registry):
    """A LISTEN on the Player wake channel; yields a function answering the notifications seen."""
    with psycopg.connect(registry.db.dsn, autocommit=True) as conn:
        conn.execute(f"LISTEN {PLAYER_STATE_CHANNEL}")
        yield lambda: list(conn.notifies(timeout=1))


def stored(registry) -> dict | None:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT revision, changed_at, changed_by, snapshot FROM runtime_state"
                            ).fetchone()


def program_runs(app, program_id: str) -> list[dict]:
    state = app.state.coordinator.runtime.read().export_state()
    return [run for run in state["runs"].values() if run.get("program_id") == program_id]


def test_an_idle_tick_writes_no_runtime_row_and_wakes_no_player(registry, wall):
    app, _ = wall
    players(registry, 2)
    runtime = app.state.coordinator.runtime
    runtime.command("set_scene", Scene(scene_id="idle", loop=True), by="console")   # a row to compare
    app.state.scheduler.tick()                     # settles the two Players' configurations
    before = stored(registry)
    with listening(registry) as notifications:
        app.state.scheduler.tick()
        app.state.scheduler.tick()
        registry.clock.advance(1)
        app.state.scheduler.tick()
        assert notifications() == []
    after = stored(registry)
    assert (after["revision"], after["changed_at"]) == (before["revision"], before["changed_at"])


def test_a_tick_that_changes_the_runtime_saves_now_with_it(registry, wall):
    app, _ = wall
    runtime = app.state.coordinator.runtime
    runtime.command("set_scene", Scene(scene_id="show", loop=True), by="console")
    runtime.command_current("create_program", window_program("evening", "show", 1020, 1080),
                            by="console")
    app.state.scheduler.tick()                     # 1000: nothing opens
    registry.clock.advance(20)                     # 1020: the window opens at this tick
    with listening(registry) as notifications:
        app.state.scheduler.tick()
        assert len(notifications()) >= 1
    row = stored(registry)
    assert (row["snapshot"]["now"], row["changed_at"], row["changed_by"]) == (1020, 1020, "scheduler")
    assert len(program_runs(app, "evening")) == 1


def test_a_house_zone_change_never_admits_a_window_moved_into_the_past(registry, wall):
    app, client = wall
    ten = at(UTC_HOUSE, "2026-10-12T10:00")
    registry.clock.advance(ten - registry.clock.utc())
    runtime = app.state.coordinator.runtime
    runtime.command("set_scene", Scene(scene_id="noon", loop=True), by="console")
    runtime.command_current("create_program", Program(
        program_id="noon", name="Noon", scene_id="noon", when=daily("12:00", "14:00")), by="console")
    assert stored(registry)["snapshot"]["now"] == ten          # the Runtime's last change
    while registry.clock.utc() < at(UTC_HOUSE, "2026-10-12T11:30"):
        registry.clock.advance(15 * 60)
        app.state.scheduler.tick()
    assert stored(registry)["snapshot"]["now"] == ten          # idle ticks saved nothing
    saved = client.put("/v1/operator/house", headers=BEARER,
                       json={"time": {"zone": "Africa/Lagos", "place": None}, "units": "cm",
                             "clock": "24h"})
    assert saved.status_code == 200, saved.text
    # Lagos is UTC+1: the window is now 11:00-13:00 UTC, begun half an hour before this tick.
    app.state.scheduler.tick()
    assert program_runs(app, "noon") == []
    registry.clock.advance(at(UTC_HOUSE, "2026-10-13T11:00") - registry.clock.utc())
    app.state.scheduler.tick()
    (run,) = program_runs(app, "noon")
    assert run["started_at"] == at(UTC_HOUSE, "2026-10-13T11:00")


def test_an_edit_that_moves_only_now_takes_no_transaction_id(registry):
    set_house(registry.db, UTC_HOUSE)
    store = RuntimeStore(registry.db, registry.clock)
    store.command("set_scene", Scene(scene_id="still", loop=True), by="console")
    before = stored(registry)
    registry.clock.advance(30)
    with registry.db.transaction() as conn:
        with store.edit(conn, by="scheduler", command="advance") as runtime:
            runtime.advance(registry.clock.utc())
        assert conn.execute("SELECT pg_current_xact_id_if_assigned() AS x").fetchone()["x"] is None
    assert stored(registry) == before
