"""Node records' retention: the one owner of how long Central keeps what a Node reported
(decision 0033).

Until migration 113 nothing trimmed these tables (3.5 million rows, 4.5 GB of a 10 GiB volume
on 2026-11-15). A week is kept because the records are what a person reads to find out why a
Pi misbehaved (owner, 2026-10-05, guidance: "having Central throw away good debugging and
observability data is silly"); Central's own readers need far less, and each rule below keeps
what they read at any age.

`prune()` is the scheduler's "node-retention" step, wired beside execution events' "retention"
as `Scheduler(reconciler, coordinator, measurements, clock, retentions={"retention": ...,
"node-retention": NodeRecordRetention(db.transaction, clock).prune})`: one `/healthz` facet per
key, so a failing trim never hides behind the other. It runs outside the coordinator's
transactions, through `central.infra.batched_retention.drain`, one whole-batch statement per
table.

The cut is `clock.utc() - kept_seconds`, compared with `received_at`, which the same clock
stamped (`NodeSessions.clock`): a clock is compared only with itself.

| Result key (the rule's table) | Deleted: rows received before the cut | Never deleted | Index |
|---|---|---|---|
| `node_reconciliation_work` | finished work, and with each row its `node_evidence` row | unfinished work; work whose evidence an Output loss names (`node_output_losses`' foreign key); the evidence of an event (the reboot audit reads events at any age) | `received_at` (migration 113) |
| `node_display_exchanges` | exchanges, and with each its `node_display_handoffs` and `node_display_withdrawals` rows | nothing younger than the cut: `node_display.py` reads the newest 128 per Output and a decision at most `decision_ms` old, `node_acceptance.py` the latest per Output and its handoff | `received_at` (migration 113) |
| `node_host_observations` | samples | nothing younger than the cut | `received_at` (migration 113) |
| `node_manager_observations` | samples | nothing younger than the cut | `received_at` (migration 113) |

Not here: `node_app_links` holds one row per device generation and needs no trim
(`central/fleet/node_app_links.py`); `node_link_records` is E5's (docs/runbook.md).
"""

from __future__ import annotations

from typing import Final

from central.infra.batched_retention import Begin, drain
from contracts.time import Clock

KEPT_SECONDS: Final = 7 * 86400
BATCH_ROWS: Final = 500
BATCHES: Final = 4


def _whole_batch(due: str) -> str:
    """The `due` CTE (at most `batch_rows` keys, oldest first by `received_at`'s index) cut to a
    whole batch: all of it when it holds `batch_rows` keys, else nothing."""
    return (f"WITH due AS ({due} ORDER BY received_at LIMIT %(batch_rows)s), "
            "batch AS (SELECT * FROM due WHERE (SELECT count(*) FROM due) = %(batch_rows)s)")


# Each statement is held to central/infra/batched_retention.py's contract: bounded (the oldest
# `batch_rows` keys from `received_at`'s index), idempotent, a whole batch. Dependent rows go in
# a CTE of the same statement, so the rule's own DELETE is the counted one and every foreign key
# is checked at the statement's end, after both deletes.
_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("node_reconciliation_work",
     _whole_batch(
         "SELECT w.work_id, w.producer_id, w.kind, w.evidence_id, w.received_at "
         "FROM node_reconciliation_work w WHERE w.received_at < %(cut)s "
         "AND w.completed_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM node_output_losses l "
         "WHERE l.cause_producer_id = w.producer_id AND l.cause_kind = w.kind "
         "AND l.cause_evidence_id = w.evidence_id)")
     + ", evidence AS (DELETE FROM node_evidence e USING batch b "
     "WHERE e.producer_id = b.producer_id AND e.kind = b.kind AND e.evidence_id = b.evidence_id "
     "AND b.kind <> 'event') "
     "DELETE FROM node_reconciliation_work w USING batch b WHERE w.work_id = b.work_id"),
    ("node_display_exchanges",
     _whole_batch(
         "SELECT producer_id, request_id, decision_id, received_at FROM node_display_exchanges "
         "WHERE received_at < %(cut)s")
     + ", handoffs AS (DELETE FROM node_display_handoffs h USING batch b "
     "WHERE h.decision_id = b.decision_id), "
     "withdrawals AS (DELETE FROM node_display_withdrawals d USING batch b "
     "WHERE d.decision_id = b.decision_id) "
     "DELETE FROM node_display_exchanges e USING batch b "
     "WHERE e.producer_id = b.producer_id AND e.request_id = b.request_id"),
    ("node_host_observations",
     _whole_batch(
         "SELECT producer_id, sequence, received_at FROM node_host_observations "
         "WHERE received_at < %(cut)s")
     + " DELETE FROM node_host_observations o USING batch b "
     "WHERE o.producer_id = b.producer_id AND o.sequence = b.sequence"),
    ("node_manager_observations",
     _whole_batch(
         "SELECT producer_id, sequence, received_at FROM node_manager_observations "
         "WHERE received_at < %(cut)s")
     + " DELETE FROM node_manager_observations o USING batch b "
     "WHERE o.producer_id = b.producer_id AND o.sequence = b.sequence"),
)


class NodeRecordRetention:
    """`begin` opens one transaction per batch (`central.db.Database.transaction`); `clock` is
    the clock that stamps `received_at`. `ValueError` when `kept_seconds` is negative or
    `batch_rows` or `batches` is under 1."""

    def __init__(self, begin: Begin, clock: Clock, *, kept_seconds: float = KEPT_SECONDS,
                 batch_rows: int = BATCH_ROWS, batches: int = BATCHES) -> None:
        if kept_seconds < 0 or batch_rows < 1 or batches < 1:
            raise ValueError("kept_seconds must be >= 0, batch_rows and batches >= 1")
        self.begin, self.clock = begin, clock
        self.kept_seconds, self.batch_rows, self.batches = kept_seconds, batch_rows, batches

    def prune(self) -> dict[str, int]:
        """One pass: each table's rule drained in the table's order above, at most
        `batch_rows * batches` rows a table. Returns the rows deleted per result key (every key
        present, 0 when nothing was due).

        A table deletes a whole batch or nothing: with fewer than `batch_rows` rows past its
        rule the pass writes nothing and takes no row lock, so a tick with nothing due costs
        four index reads and no commit.

        Never deletes a row the table above lists as kept; never takes an advisory lock; never
        waits on the Coordination or Runtime lock (a test holds both while it runs).

        Errors: the first table whose batch fails raises the driver's error; the earlier
        tables' batches stay committed and the later tables wait for the next tick. Nothing
        needs undoing: the pass is idempotent.
        """
        params = {"cut": self.clock.utc() - self.kept_seconds}
        return {table: drain(self.begin, statement, params, batch_rows=self.batch_rows,
                             batches=self.batches)
                for table, statement in _RULES}
