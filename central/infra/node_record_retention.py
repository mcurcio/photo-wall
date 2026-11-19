"""Node records' retention: the one owner of how long Central keeps what a Node reported
(decisions 0033 and 0034).

A week is kept because the records are what a person reads to find out why a Pi misbehaved
(owner, 2026-10-05, guidance: "having Central throw away good debugging and observability data is
silly"). Since decision 0034 every Node record is an event (`events`, migration 119;
`central/infra/events.py`, the one home of its week, `EVENTS_KEPT_SECONDS`): the old path's
tables, and their rules here, went with its routes (S4c part 2a-iv).

`prune()` is the scheduler's "node-retention" step, wired as `Scheduler(coordinator,
measurements, clock, retentions={"node-retention": NodeRecordRetention(db.transaction).prune})`:
its own `/healthz` facet, so a failing trim never hides behind another step. It runs outside the
coordinator's transactions, through `central.infra.batched_retention.drain`, one whole-batch
statement per table.

| Result key (the rule's table) | Deleted | Never deleted | Index |
|---|---|---|---|
| `events` | every row recorded more than `EVENTS_KEPT_SECONDS` ago, oldest first | a row of the week | `recorded_at` (migration 119) |

Its rows are stamped by the database's clock, so its cut is that clock's too
(`clock_timestamp()` in the statement): a clock is compared only with itself. Its walk passes no
kept row.

Not here: `node_states` holds one row per Pi's state key and needs none
(`central/infra/node_link_store.py`).
"""

from __future__ import annotations

from typing import Final

from central.infra.batched_retention import Begin, drain
from central.infra.events import EVENTS_KEPT_SECONDS

BATCH_ROWS: Final = 500
BATCHES: Final = 4

# Each statement is held to central/infra/batched_retention.py's contract: bounded (the oldest
# `batch_rows` keys from `recorded_at`'s index), idempotent, a whole batch.
_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("events",
     "WITH due AS (SELECT id, recorded_at FROM events WHERE recorded_at < "
     f"EXTRACT(EPOCH FROM clock_timestamp()) - {EVENTS_KEPT_SECONDS} "
     "ORDER BY recorded_at LIMIT %(batch_rows)s), "
     "batch AS (SELECT * FROM due WHERE (SELECT count(*) FROM due) = %(batch_rows)s) "
     "DELETE FROM events e USING batch b WHERE e.id = b.id"),
)


class NodeRecordRetention:
    """`begin` opens one transaction per batch (`central.db.Database.transaction`). `ValueError`
    when `batch_rows` or `batches` is under 1."""

    def __init__(self, begin: Begin, *, batch_rows: int = BATCH_ROWS, batches: int = BATCHES) -> None:
        if batch_rows < 1 or batches < 1:
            raise ValueError("batch_rows and batches must be >= 1")
        self.begin, self.batch_rows, self.batches = begin, batch_rows, batches

    def prune(self) -> dict[str, int]:
        """One pass: each table's rule drained in the table's order above, at most
        `batch_rows * batches` rows a table. Returns the rows deleted per result key (every key
        present, 0 when nothing was due).

        A table deletes a whole batch or nothing: with fewer than `batch_rows` rows past its
        rule the pass writes nothing and takes no row lock, so a tick with nothing due costs
        one index read per table and no commit.

        Never deletes a row the table above lists as kept; never takes an advisory lock; never
        waits on the Coordination or Runtime lock (a test holds both while it runs).

        Errors: the first table whose batch fails raises the driver's error; the earlier
        tables' batches stay committed and the later tables wait for the next tick. Nothing
        needs undoing: the pass is idempotent.
        """
        return {table: drain(self.begin, statement, {}, batch_rows=self.batch_rows,
                             batches=self.batches)
                for table, statement in _RULES}
