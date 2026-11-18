"""Bounded batch deletes: the one home of Central's retention loop (decision 0033).

Every retention deletes through `drain`: node records and events
(`central/infra/node_record_retention.py`) and finished queue jobs
(`central/infra/queue_ops.py` `FinishedJobs`). The loop was written once, for execution events
(a table gone since decision 0034), after the 2026-10-28 outage: one unbounded DELETE inside the coordinator's transaction was
cancelled every second, rolled the whole step back and wrote log until the disk filled. Its
rules are this module's contract, so no retention can repeat that.

What a retention statement must be (each caller's statement is held to it by a DB-tier test):

- **Bounded.** It deletes at most `%(batch_rows)s` rows, found from the oldest end of an index,
  never by reading the whole table. The walk passes the rows its rule keeps that lie at that
  end (each caller states how many and why they are bounded). One exception, an hourly
  statement: finished queue jobs are judged by their newest event, which no index orders, so
  when nothing is due `FinishedJobs` walks the kept week of finished jobs (and every job with
  no event) once an hour, one event-index probe each, as procrastinate's own purge does.
- **Idempotent.** Running it again deletes other rows or none; a cancelled run loses nothing.
- **A whole batch, where it runs every tick.** A statement the scheduler runs each second
  deletes exactly `%(batch_rows)s` rows or none: a delete of one row costs a commit (an fsync),
  and a per-tick retention that deletes whatever is due commits every tick (execution events'
  did, before decision 0034 removed them: 3,600 commits an hour). So its table holds between its rule's bound and that bound plus
  `batch_rows` rows. A statement run by an hourly job (finished queue jobs) may delete a short
  last batch.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

Begin = Callable[[], AbstractContextManager[Any]]
"""Opens one transaction and yields its psycopg connection: a normal exit commits, an exception
rolls back and re-raises. `central.db.Database.transaction` is one (lock timeout 5 s, statement
timeout 10 s)."""


def drain(begin: Begin, statement: str, params: Mapping[str, object], *, batch_rows: int,
          batches: int) -> int:
    """Run `statement` up to `batches` times, each run in its own transaction from `begin`, with
    `params` plus `batch_rows`; stop at the first run that deletes fewer than `batch_rows` rows.
    Returns the rows deleted, counted from each run's `rowcount` (the statement's own DELETE: a
    statement that removes dependent rows in a CTE counts its rule's table only).

    Never:
    - takes an advisory lock, or runs inside a caller's transaction: a batch can never hold the
      Coordination, Runtime or Node lock;
    - retries or catches: a failed batch raises the driver's error (a statement or lock timeout
      included) with the earlier batches committed. The caller's scheduler step turns failing
      and the next run goes on where this one stopped;
    - deletes more than `batch_rows * batches` rows in one call.

    `ValueError` when `batch_rows` or `batches` is under 1, or `params` names `batch_rows`.
    """
    if batch_rows < 1 or batches < 1:
        raise ValueError("batch_rows and batches must be >= 1")
    if "batch_rows" in params:
        raise ValueError("params must not name batch_rows: drain supplies it")
    bound = {**params, "batch_rows": batch_rows}
    deleted = 0
    for _ in range(batches):
        with begin() as conn:
            rows = conn.execute(statement, bound).rowcount
        deleted += rows
        if rows < batch_rows:
            break
    return deleted
