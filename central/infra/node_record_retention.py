"""Node records' retention: the one owner of how long Central keeps what a Node reported
(decision 0033).

Until migration 113 nothing trimmed these tables (3.5 million rows, 4.5 GB of a 10 GiB volume
on 2026-11-15). A week is kept because the records are what a person reads to find out why a
Pi misbehaved (owner, 2026-10-05, guidance: "having Central throw away good debugging and
observability data is silly"); Central's own readers need far less, and each rule below keeps
what they read at any age.

`prune()` is the scheduler's "node-retention" step, wired as `Scheduler(reconciler, coordinator,
measurements, clock, retentions={"node-retention": NodeRecordRetention(db.transaction,
clock).prune})`: its own `/healthz` facet, so a failing trim never hides behind another step. It runs outside the coordinator's
transactions, through `central.infra.batched_retention.drain`, one whole-batch statement per
table.

The cut is `clock.utc() - kept_seconds`, compared with `received_at`, which the same clock
stamped (`NodeSessions.clock`): a clock is compared only with itself.

| Result key (the rule's table) | Deleted: rows received before the cut | Never deleted | Index |
|---|---|---|---|
| `node_reconciliation_work` | finished work, and with each row its `node_evidence` row | unfinished work; the evidence of an event (the reboot audit reads events at any age) | `received_at` (migration 113) |
| `node_display_exchanges` | exchanges, and with each its `node_display_handoffs` and `node_display_withdrawals` rows | of a producer of its device's current boot, the newest `RECENT_EXCHANGES` (128) per Output by its boot clock, at any age: `node_display.py` reads the newest per Output (`DISPLAY_OUTPUTS_SQL`) and a withdrawal looks through the newest 128 | `received_at` (migration 113) |
| `node_host_observations` | samples | of a producer of its device's current or most recently superseded boot, the newest sample, at any age: `node_observations.py` reads the newest per producer (G12 shows both boots' "last heard") | `received_at` (migration 113) |
| `node_manager_observations` | samples | of a producer of its device's current or most recently superseded boot, the newest `PREPARATION_KEYS` (8), at any age (unread since S4c-2a-iii) | `received_at` (migration 113) |

So a Pi dark for over a week (powered off, its current boot silent) still shows its last sample
and its Outputs; a boot that is neither its device's current nor its previous one keeps nothing
past the week. Migration 113 kept the same at the deploy.

What the walk pays: every kept row past the cut (the guards above) stays at the head of
`received_at`'s index, and each pass walks past it before reaching a deletable row. That head is
bounded by the fleet, not by time: one or 8 samples per producer of two boots per device, 128
exchanges per current Output, plus unfinished work (the reconciler finishes it) and the work an
Output loss names. A partial index on finished work would skip the work table's share, but
completing work changes `completed_at`, so every completion would become a full (non-HOT)
update; decision 0033 keeps HOT completions (as it does for the reconciler's scan).

`events` (decision 0034, migration 119; `central/infra/events.py`, the one home of its week,
`EVENTS_KEPT_SECONDS`) keeps nothing past the week: every row recorded before the cut goes, oldest
first by `recorded_at`'s index, in whole batches like the rest. Its rows are stamped by the
database's clock, so its cut is that clock's too (`clock_timestamp()` in the statement), never
`clock`'s: a clock is compared only with itself. Its walk passes no kept row.

Not here: `node_app_links` holds one row per device generation and needs no trim
(`central/fleet/node_app_links.py`); `node_states` holds one row per Pi's state key and needs none
(`central/infra/node_link_store.py`).
"""

from __future__ import annotations

from typing import Final

from central.fleet.node_display import RECENT_EXCHANGES
from central.infra.batched_retention import Begin, drain
from central.infra.events import EVENTS_KEPT_SECONDS
from contracts.time import Clock

KEPT_SECONDS: Final = 7 * 86400
BATCH_ROWS: Final = 500
# The newest App Manager samples kept per producer: nothing reads them since S4c-2a-iii (the table and
# this rule go in S4c-2a-iv and 2a-v).
PREPARATION_KEYS: Final = 8
BATCHES: Final = 4
# `node_observations.py` `_NEWEST_HOST_SQL`: a producer's newest host sample is all it reads.
HOST_SAMPLES_KEPT: Final = 1


def _of_kept_boot(alias: str, *, previous: bool) -> str:
    """Whether `alias`'s producer belongs to its device's current boot (with `previous`, or to
    its device generation's most recently superseded one, as `_FLEET_HOSTS_SQL` picks it)."""
    boots = "b.superseded_at IS NULL"
    if previous:
        boots += (" OR b.admission_id = (SELECT s.admission_id FROM node_boot_admissions s "
                  "WHERE s.device_id = b.device_id AND s.device_generation = b.device_generation "
                  "AND s.superseded_at IS NOT NULL "
                  "ORDER BY s.superseded_at DESC, s.admission_id DESC LIMIT 1)")
    return (f"EXISTS (SELECT 1 FROM node_producers p JOIN node_boot_admissions b "
            f"USING (admission_id) WHERE p.producer_id = {alias}.producer_id AND ({boots}))")


def _past_newest(alias: str, table: str, order: str, kept: int, *, key: str = "producer_id",
                 previous: bool) -> str:
    """Whether `alias`'s row may go by age: its producer is of no kept boot, or `kept` newer
    rows of its `key` (by `order`, on the key's index) stand before it. With fewer than `kept`
    rows the comparison is NULL and the row stays."""
    same = " AND ".join(f"n.{column} = {alias}.{column}" for column in key.split(","))
    return (f"(NOT {_of_kept_boot(alias, previous=previous)} OR {alias}.{order} < ("
            f"SELECT n.{order} FROM {table} n WHERE {same} "  # noqa: S608
            f"ORDER BY n.{order} DESC OFFSET {kept - 1} LIMIT 1))")


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
         "AND w.completed_at IS NOT NULL")
     + ", evidence AS (DELETE FROM node_evidence e USING batch b "
     "WHERE e.producer_id = b.producer_id AND e.kind = b.kind AND e.evidence_id = b.evidence_id "
     "AND b.kind <> 'event') "
     "DELETE FROM node_reconciliation_work w USING batch b WHERE w.work_id = b.work_id"),
    ("node_display_exchanges",
     _whole_batch(
         "SELECT e.producer_id, e.request_id, e.decision_id, e.received_at "
         "FROM node_display_exchanges e WHERE e.received_at < %(cut)s AND "
         + _past_newest("e", "node_display_exchanges", "sampled_boottime_ms", RECENT_EXCHANGES,
                        key="producer_id,output_id", previous=False))
     + ", handoffs AS (DELETE FROM node_display_handoffs h USING batch b "
     "WHERE h.decision_id = b.decision_id), "
     "withdrawals AS (DELETE FROM node_display_withdrawals d USING batch b "
     "WHERE d.decision_id = b.decision_id) "
     "DELETE FROM node_display_exchanges e USING batch b "
     "WHERE e.producer_id = b.producer_id AND e.request_id = b.request_id"),
    ("node_host_observations",
     _whole_batch(
         "SELECT o.producer_id, o.sequence, o.received_at FROM node_host_observations o "
         "WHERE o.received_at < %(cut)s AND "
         + _past_newest("o", "node_host_observations", "sequence", HOST_SAMPLES_KEPT, previous=True))
     + " DELETE FROM node_host_observations o USING batch b "
     "WHERE o.producer_id = b.producer_id AND o.sequence = b.sequence"),
    ("node_manager_observations",
     _whole_batch(
         "SELECT o.producer_id, o.sequence, o.received_at FROM node_manager_observations o "
         "WHERE o.received_at < %(cut)s AND "
         + _past_newest("o", "node_manager_observations", "sequence", PREPARATION_KEYS, previous=True))
     + " DELETE FROM node_manager_observations o USING batch b "
     "WHERE o.producer_id = b.producer_id AND o.sequence = b.sequence"),
    ("events",
     "WITH due AS (SELECT id, recorded_at FROM events WHERE recorded_at < "
     f"EXTRACT(EPOCH FROM clock_timestamp()) - {EVENTS_KEPT_SECONDS} "
     "ORDER BY recorded_at LIMIT %(batch_rows)s), "
     "batch AS (SELECT * FROM due WHERE (SELECT count(*) FROM due) = %(batch_rows)s) "
     "DELETE FROM events e USING batch b WHERE e.id = b.id"),
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
        """One pass: each table's rule drained in the table's order above (`events` last), at most
        `batch_rows * batches` rows a table. Returns the rows deleted per result key (every key
        present, 0 when nothing was due).

        A table deletes a whole batch or nothing: with fewer than `batch_rows` rows past its
        rule the pass writes nothing and takes no row lock, so a tick with nothing due costs
        five index reads and no commit.

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
