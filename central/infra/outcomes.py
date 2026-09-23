"""`job_outcomes`: the latest attempt's STATUS per lock key (design §10.2).

The asset record holds what was produced; this table holds only how the latest run of each
`(job type, subject)` ended, because a late waiter misses a NOTIFY. `record` upserts, takes a
new global sequence number (`since` compares against it), and sends `NOTIFY job_outcome,
<lock key>` in the same transaction, so a waiter is woken only by a committed outcome. The table's
CHECKs (migration 020) refuse an inconsistent status/reason/retry row.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from central.infra.transactions import pg_connection
from central.kernel.jobs import Job, job_keys
from central.kernel.transactions import Transaction

OutcomeStatus: TypeAlias = Literal["ok", "transient", "terminal"]
OUTCOME_CHANNEL = "job_outcome"
_COLUMNS = "lock_key, job_name, status, reason, retry_not_before, seq, updated_at"


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    lock_key: str
    job_name: str
    status: OutcomeStatus
    reason: str | None
    retry_not_before: float | None
    seq: int
    updated_at: float


def _row(values: dict[str, Any]) -> OutcomeRow:
    return OutcomeRow(**{name: values[name] for name in OutcomeRow.__slots__})


def _finite(value: object, what: str) -> float:
    """DOUBLE PRECISION stores NaN and infinity, which no retry window or purge can compare."""
    if type(value) not in (int, float) or not math.isfinite(value):  # type: ignore[arg-type]
        raise ValueError(f"invalid_{what}")
    return float(value)  # type: ignore[arg-type]


class JobOutcomes:
    """The PostgreSQL repository of `job_outcomes` (migration 020)."""

    def get(self, tx: Transaction, lock_key: str) -> OutcomeRow | None:
        row = pg_connection(tx).execute(
            f"SELECT {_COLUMNS} FROM job_outcomes WHERE lock_key = %s", (lock_key,)
        ).fetchone()
        return None if row is None else _row(row)

    def get_many(self, tx: Transaction, lock_keys: Collection[str]) -> dict[str, OutcomeRow]:
        if not lock_keys:
            return {}
        rows = pg_connection(tx).execute(
            f"SELECT {_COLUMNS} FROM job_outcomes WHERE lock_key = ANY(%s)", (list(lock_keys),)
        ).fetchall()
        return {row["lock_key"]: _row(row) for row in rows}

    def record(self, tx: Transaction, job: Job[Any], *, status: OutcomeStatus,
               reason: str | None, retry_not_before: float | None, now: float) -> OutcomeRow:
        """Upsert the key's latest outcome and NOTIFY it, in the caller's transaction."""
        if retry_not_before is not None:
            _finite(retry_not_before, "retry_not_before")
        now = _finite(now, "now")
        lock = job_keys(job).lock
        conn = pg_connection(tx)
        row = conn.execute(
            f"""INSERT INTO job_outcomes ({_COLUMNS})
                VALUES (%(lock)s, %(name)s, %(status)s, %(reason)s, %(retry_not_before)s,
                        nextval('job_outcome_seq'), %(now)s)
                ON CONFLICT (lock_key) DO UPDATE SET
                    job_name = EXCLUDED.job_name,
                    status = EXCLUDED.status,
                    reason = EXCLUDED.reason,
                    retry_not_before = EXCLUDED.retry_not_before,
                    seq = EXCLUDED.seq,
                    updated_at = EXCLUDED.updated_at
                RETURNING {_COLUMNS}""",
            {"lock": lock, "name": type(job).job_name, "status": status, "reason": reason,
             "retry_not_before": retry_not_before, "now": now},
        ).fetchone()
        conn.execute("SELECT pg_notify(%s, %s)", (OUTCOME_CHANNEL, lock))
        return _row(row)

    def purge(self, tx: Transaction, *, older_than: float) -> int:
        """Delete rows not written since `older_than` (epoch seconds); returns how many."""
        cursor = pg_connection(tx).execute(
            "DELETE FROM job_outcomes WHERE updated_at < %s",
            (_finite(older_than, "older_than"),),
        )
        return cursor.rowcount
