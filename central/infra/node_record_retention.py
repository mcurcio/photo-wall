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

from central.infra.batched_retention import Begin
from contracts.time import Clock

KEPT_SECONDS: Final = 7 * 86400
BATCH_ROWS: Final = 500
BATCHES: Final = 4


class NodeRecordRetention:
    """`begin` opens one transaction per batch (`central.db.Database.transaction`); `clock` is
    the clock that stamps `received_at`. `ValueError` when `kept_seconds` is negative or
    `batch_rows` or `batches` is under 1."""

    def __init__(self, begin: Begin, clock: Clock, *, kept_seconds: float = KEPT_SECONDS,
                 batch_rows: int = BATCH_ROWS, batches: int = BATCHES) -> None:
        raise NotImplementedError("decision 0033, slice 2")

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
        raise NotImplementedError("decision 0033, slice 2")
