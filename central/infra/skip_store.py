"""Skips over PostgreSQL (decision 0036, slice 3): the one repository of migration 133's `skips`
table, called inside Coordination's transaction (central/coordination.py `Coordinator.skip`), and
its batched retention, outside every lock (central/infra/batched_retention.py).

A row is one request, written once and never updated. Its covers are live while they show
(`live_in`); a row past its last cover's end by `KEEP_SECONDS` goes in the retention.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from psycopg.types.json import Jsonb

from central.everyday.model import Cover, SkipOutcome, SkipScope
from central.everyday.ports import SkipRecord
from central.infra.batched_retention import Begin, drain
from contracts.time import Clock

KEEP_SECONDS: Final = 86400          # a row is kept a day past its last cover's end
BATCH_ROWS: Final = 500
BATCHES: Final = 4
# Held to central/infra/batched_retention.py's contract: bounded (the oldest `batch_rows` keys from
# `skips_ends_at`, which passes no kept row) and idempotent. The step runs every RETENTION_EVERY,
# never each tick, so it may delete a short batch.
_PAST_KEEP: Final = (
    "DELETE FROM skips WHERE request_id IN (SELECT request_id FROM skips "
    "WHERE ends_at <= %(before)s ORDER BY ends_at LIMIT %(batch_rows)s)")


def _record(row: Mapping[str, Any]) -> SkipRecord:
    return SkipRecord(request_id=row["request_id"], scope=SkipScope(row["scope"]),
                      target_id=row["target_id"], cause=row["cause"],
                      outcome=SkipOutcome.model_validate(row["outcome"]), by=row["created_by"])


class PgSkips:
    """Stateless; every method runs in the caller's transaction."""

    def get_in(self, conn: Any, request_id: str) -> SkipRecord | None:
        row = conn.execute("SELECT * FROM skips WHERE request_id=%s", (request_id,)).fetchone()
        return None if row is None else _record(row)

    def add_in(self, conn: Any, record: SkipRecord, *, now: float) -> None:
        conn.execute(
            "INSERT INTO skips(request_id,scope,target_id,cause,at,outcome,ends_at,created_at,"
            "created_by,changed_at,changed_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (record.request_id, record.scope.value, record.target_id, record.cause,
             record.outcome.at, Jsonb(record.outcome.model_dump(mode="json")), record.outcome.ends_at,
             now, record.by, now, record.by))

    def live_in(self, conn: Any, now: float) -> tuple[Cover, ...]:
        """Every cover still showing or still to show at `now`, oldest request first."""
        return tuple(cover for row in conn.execute(
            "SELECT outcome FROM skips WHERE ends_at>%s ORDER BY at, request_id", (now,)).fetchall()
            for cover in SkipOutcome.model_validate(row["outcome"]).covers if cover.end > now)

    def recent_in(self, conn: Any, frame_ids: Sequence[str] | None, now: float,
                  ) -> dict[str, tuple[tuple[SkipRecord, Cover | None], ...]]:
        """Each Frame's skips kept, newest first, with its cover in each (None: it stayed)."""
        found: dict[str, list[tuple[SkipRecord, Cover | None]]] = {}
        wanted = None if frame_ids is None else set(frame_ids)
        for row in conn.execute("SELECT * FROM skips ORDER BY at DESC, request_id DESC").fetchall():
            record = _record(row)
            covers = {cover.frame_id: cover for cover in record.outcome.covers}
            for frame_id in [*covers, *(stay.frame_id for stay in record.outcome.stayed)]:
                if wanted is None or frame_id in wanted:
                    found.setdefault(frame_id, []).append((record, covers.get(frame_id)))
        return {frame_id: tuple(rows) for frame_id, rows in found.items()}


class SkipRetention:
    """`prune()` is the scheduler's "skip-retention" step: rows a day past their last cover."""

    def __init__(self, begin: Begin, clock: Clock, *, batch_rows: int = BATCH_ROWS,
                 batches: int = BATCHES) -> None:
        if batch_rows < 1 or batches < 1:
            raise ValueError("batch_rows and batches must be >= 1")
        self.begin, self.clock, self.batch_rows, self.batches = begin, clock, batch_rows, batches

    def prune(self) -> dict[str, int]:
        return {"skips": drain(self.begin, _PAST_KEEP, {"before": self.clock.utc() - KEEP_SECONDS},
                               batch_rows=self.batch_rows, batches=self.batches)}


__all__ = ["KEEP_SECONDS", "PgSkips", "SkipRetention"]
