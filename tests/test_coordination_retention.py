"""Execution events' retention can never block or fail the coordinator's cycle (2026-10-28).

The outage: the 17-hour planning outage stopped the old prune, which was one unbounded DELETE in
the coordinator's settle transaction, while Players' reports kept writing events (1,042,147
rows). Once planning ran again, that DELETE was cancelled by the 10 s statement timeout every
second, held the Coordination and Runtime locks for those 10 s (290 other transactions timed out
waiting), rolled the step back and wrote WAL each time until the database's disk filled.

These tests drive the real app's Scheduler on PostgreSQL: retention is its own step, its own
short bounded transactions, holding no advisory lock, and a prune that cannot finish leaves the
coordinator's steps ok."""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_registry import ADMIN

from central.app import create_app
from central.infra.execution_event_retention import (
    BATCH_ROWS,
    BATCHES_PER_STEP,
    EVENTS_KEPT,
    ExecutionEventRetention,
)

pytestmark = pytest.mark.db


@pytest.fixture
def wall(registry):
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app):
        yield app


def seed(db, rows: int) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO execution_events(occurred_at,kind) "
                     "SELECT 1000, 'observation' FROM generate_series(1, %s)", (rows,))


def events(db) -> int:
    with db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM execution_events").fetchone()["n"]


def oldest(db) -> int:
    with db.transaction() as conn:
        return conn.execute("SELECT min(sequence) AS s FROM execution_events").fetchone()["s"]


def steps(app):
    return {name: step.status for name, step in app.state.scheduler.health().steps.items()}


def runtime_now(db) -> float:
    with db.transaction() as conn:
        return conn.execute("SELECT snapshot FROM runtime_state").fetchone()["snapshot"]["now"]


def test_a_backlog_of_any_size_drains_in_bounded_steps_while_the_cycle_commits(registry, wall):
    """A backlog of more than one step's budget: each tick's coordinator steps are ok and the
    Runtime moves on, each tick deletes at most `BATCH_ROWS x BATCHES_PER_STEP` rows, and the
    ticks after bring the table to exactly `EVENTS_KEPT`. Mutation probe: drop the `batches`
    bound in `ExecutionEventRetention.prune` (the first tick deletes the whole backlog)."""
    budget = BATCH_ROWS * BATCHES_PER_STEP
    backlog = EVENTS_KEPT + budget + BATCH_ROWS // 2
    seed(registry.db, backlog)
    registry.clock.advance(5)
    wall.state.scheduler.tick()
    assert {step: steps(wall)[step] for step in ("runtime", "planning", "settle", "retention")} \
        == {"runtime": "ok", "planning": "ok", "settle": "ok", "retention": "ok"}
    assert runtime_now(registry.db) == registry.clock.utc()
    assert events(registry.db) == backlog - budget
    wall.state.scheduler.tick()
    assert events(registry.db) == EVENTS_KEPT
    wall.state.scheduler.tick()
    assert events(registry.db) == EVENTS_KEPT


def test_each_retention_transaction_stays_within_its_row_budget(registry):
    """One batch is one transaction of at most `batch_rows` of the oldest rows; a step stops at
    its batch count and the next step goes on where it stopped, never past the newest `kept`.
    Mutation probe: drop the `LIMIT` (the first batch deletes everything below the cut)."""
    seed(registry.db, 100)
    first = oldest(registry.db)
    retention = ExecutionEventRetention(registry.db, kept=10, batch_rows=7, batches=2)
    assert retention.prune() == 14
    assert (oldest(registry.db), events(registry.db)) == (first + 14, 86)
    while retention.prune():
        pass
    assert events(registry.db) == 10


def test_a_prune_that_cannot_finish_holds_no_lock_and_never_fails_the_cycle(registry, wall):
    """The oldest event is held by another transaction, so no prune can finish (as when the old
    DELETE ran into its statement timeout). The coordinator's steps stay ok, an expired pin is
    still released (settle commits), while the prune waits no advisory lock is held, and only
    the retention facet fails; once the row is free the next tick drains. Red before the fix:
    settle failed with `LockNotAvailable` while it held the Coordination and Runtime locks.
    Mutation probe: run `ExecutionEventRetention.prune` inside `Coordinator._settle`."""
    seed(registry.db, EVENTS_KEPT + 50)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO media_references(owner,variant_key,expires_at) "
                     "VALUES('expired-offer','asset.recipe',1001)")
    registry.clock.advance(5)
    holder_ready, release = threading.Event(), threading.Event()

    def hold_the_oldest_event():
        with registry.db.transaction() as conn:
            conn.execute("SELECT 1 FROM execution_events ORDER BY sequence LIMIT 1 FOR UPDATE")
            holder_ready.set()
            release.wait(30)

    holder = threading.Thread(target=hold_the_oldest_event)
    holder.start()
    assert holder_ready.wait(10)
    tick = threading.Thread(target=wall.state.scheduler.tick)
    tick.start()
    held_while_waiting = None
    deadline = time.monotonic() + 15
    while held_while_waiting is None and time.monotonic() < deadline:
        # One transaction per look: pg_stat_activity is read once per transaction.
        with registry.db.transaction() as conn:
            waiting = conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() "
                "AND wait_event_type='Lock' AND query LIKE 'DELETE FROM execution_events%%'"
            ).fetchone()["n"]
            if waiting:
                held_while_waiting = conn.execute(
                    "SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory' AND granted "
                    "AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
                ).fetchone()["n"]
        time.sleep(.05)
    tick.join(30)
    release.set()
    holder.join(30)
    assert held_while_waiting == 0
    assert {step: steps(wall)[step] for step in ("runtime", "planning", "settle", "retention")} \
        == {"runtime": "ok", "planning": "ok", "settle": "ok", "retention": "failing"}
    assert runtime_now(registry.db) == registry.clock.utc()
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM media_references "
                            "WHERE owner='expired-offer'").fetchone()["n"] == 0
    wall.state.scheduler.tick()
    assert steps(wall)["retention"] == "ok"
    assert events(registry.db) == EVENTS_KEPT
