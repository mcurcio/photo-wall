"""Execution events' retention: the one owner of how many `execution_events` rows Central keeps.

Players' reports write events in their own transactions, at their own rate, whether or not
Central's scheduler step commits. So the backlog this step clears can be any size: the
2026-10-28 outage left 1,042,147 rows after the planning outage stopped the old prune, which was
one unbounded DELETE inside the coordinator's settle transaction, under the Coordination and
Runtime locks and a 10 s statement timeout. That DELETE was cancelled every second, rolled the
whole step back, held both locks for the full 10 s and wrote WAL each time until the database's
disk filled.

Here retention is its own scheduler step (`central/scheduler.py`, "retention"), never inside a
coordinator transaction, and its loop is `central/infra/batched_retention.drain` (decision 0033):
each batch is its own short transaction that takes no advisory lock and deletes exactly
`batch_rows` of the oldest rows beyond the `kept` newest, or none. A whole batch, because the step
runs every tick and Players report several times a second: a delete of whatever was due found
rows every tick, one commit (an fsync) each, 3,600 an hour on 2026-11-15. So the table holds
between `kept` and `kept + batch_rows` rows, and a step deletes at most `batches` batches: a
backlog of any size drains at `batch_rows x batches` rows a tick, the next tick going on where
this one stopped. Deleting a row twice is impossible and a cancelled batch loses nothing.
"""

from __future__ import annotations

from typing import Any

from central.infra.batched_retention import drain

EVENTS_KEPT = 10_000
BATCH_ROWS = 5_000
BATCHES_PER_STEP = 4

# A batch is due when the (`kept` + `batch_rows`)-th newest row exists, counted by rows, not
# sequence numbers (a rolled-back insert leaves a gap in the BIGSERIAL): then at least `batch_rows`
# rows lie beyond the `kept` newest, and the `batch_rows` oldest are all among them. Both scans
# walk the primary key's index (`kept + batch_rows` rows from the top, `batch_rows` from the
# bottom), never the whole table. With fewer rows nothing is deleted and no row is locked.
_PRUNE_BATCH = (
    "DELETE FROM execution_events WHERE sequence IN ("
    "SELECT sequence FROM execution_events "
    "WHERE EXISTS (SELECT 1 FROM execution_events "
    "ORDER BY sequence DESC OFFSET %(kept)s + %(batch_rows)s - 1 LIMIT 1) "
    "ORDER BY sequence LIMIT %(batch_rows)s)"
)


class ExecutionEventRetention:
    """`prune()` is the scheduler's "retention" step; `db` is a `central.db.Database`."""

    def __init__(self, db: Any, *, kept: int = EVENTS_KEPT, batch_rows: int = BATCH_ROWS,
                 batches: int = BATCHES_PER_STEP):
        if kept < 0 or batch_rows < 1 or batches < 1:
            raise ValueError("kept must be >= 0, batch_rows and batches >= 1")
        self.db, self.kept, self.batch_rows, self.batches = db, kept, batch_rows, batches

    def prune(self) -> int:
        """Delete up to `batches` whole batches of the oldest events beyond `kept`, each in its own
        transaction; stop at the first that is not due. Returns the rows deleted."""
        return drain(self.db.transaction, _PRUNE_BATCH, {"kept": self.kept},
                     batch_rows=self.batch_rows, batches=self.batches)
