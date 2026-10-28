"""Execution events' retention: the one owner of how many `execution_events` rows Central keeps.

Players' reports write events in their own transactions, at their own rate, whether or not
Central's scheduler step commits. So the backlog this step clears can be any size: the
2026-10-28 outage left 1,042,147 rows after the planning outage stopped the old prune, which was
one unbounded DELETE inside the coordinator's settle transaction, under the Coordination and
Runtime locks and a 10 s statement timeout. That DELETE was cancelled every second, rolled the
whole step back, held both locks for the full 10 s and wrote WAL each time until the database's
disk filled.

Here retention is its own scheduler step (`central/scheduler.py`, "retention"), never inside a
coordinator transaction: each batch is its own short transaction that takes no advisory lock and
deletes at most `batch_rows` of the oldest rows at or below the cut, the newest row beyond the
`kept` newest (so exactly `kept` rows stay, whatever gaps rolled-back inserts left in the
sequence). A step deletes at most `batches` batches, so a tick stays short, and the next tick
goes on where it stopped: deleting a row twice is impossible and a cancelled batch loses
nothing, so the step is idempotent and a backlog of any size drains at `batch_rows x batches` rows a tick.
"""

from __future__ import annotations

from typing import Any

EVENTS_KEPT = 10_000
BATCH_ROWS = 5_000
BATCHES_PER_STEP = 4

# The cut is the (`kept` + 1)-th newest row, counted by rows, not sequence numbers: a rolled-back
# insert leaves a gap in the BIGSERIAL, so `max(sequence) - kept` would keep fewer. Rows at or below
# it go, so exactly `kept` stay. Both scans walk the primary key's index (bounded: `kept` + 1 rows
# from the top, `batch_rows` from the bottom), never the whole table. With `kept` or fewer rows the
# cut is NULL and nothing is deleted.
_PRUNE_BATCH = (
    "DELETE FROM execution_events WHERE sequence IN ("
    "SELECT sequence FROM execution_events "
    "WHERE sequence <= (SELECT sequence FROM execution_events "
    "ORDER BY sequence DESC OFFSET %s LIMIT 1) "
    "ORDER BY sequence LIMIT %s)"
)


class ExecutionEventRetention:
    """`prune()` is the scheduler's "retention" step; `db` is a `central.db.Database`."""

    def __init__(self, db: Any, *, kept: int = EVENTS_KEPT, batch_rows: int = BATCH_ROWS,
                 batches: int = BATCHES_PER_STEP):
        if kept < 0 or batch_rows < 1 or batches < 1:
            raise ValueError("kept must be >= 0, batch_rows and batches >= 1")
        self.db, self.kept, self.batch_rows, self.batches = db, kept, batch_rows, batches

    def prune(self) -> int:
        """Delete up to `batches` batches of the oldest events beyond `kept`, each in its own
        transaction; stop early once a batch finds fewer than `batch_rows`. Returns the rows
        deleted."""
        deleted = 0
        for _ in range(self.batches):
            with self.db.transaction() as conn:
                rows = conn.execute(_PRUNE_BATCH, (self.kept, self.batch_rows)).rowcount
            deleted += rows
            if rows < self.batch_rows:
                break
        return deleted
