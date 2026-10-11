"""Key content over PostgreSQL (roadmap 5a; run ledger .claude/runs/photo-5a.md, slice M1).

`PgKeyContent` implements central.wall_plan.ports.KeyContentReader (Central: hydration and the
scheduler's measurement requests) and KeyContentWriter (the media worker), and is the one home of
migration 082's `photo_key_content` table: one row per asset id (the id already pins the photo's
revision) at one analysis version. Every time written or compared here is `times`' (the
database's, G11): the worker writes `retry_at`, Central compares it.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from central.db import Database, TransactionClock
from central.media_queue import MediaTaskQueue
from central.photo_library.model import LibraryAsset
from central.wall_plan.key_content import Analysis, KeyContent, MeasureFailure

# A failed measurement's photo is left out of plans this long, then measured again.
MEASURE_RETRY_SECONDS: Final = 3600


class PgKeyContent:
    def __init__(self, db: Database, *, times: TransactionClock,
                 queue: MediaTaskQueue | None = None) -> None:
        """`queue` queues measurements in the requesting transaction (None: a composition that
        never requests, whose `request_in` refuses with media_queue_unconfigured)."""
        self.db, self.times, self.queue = db, times, queue

    def analyses_in(self, conn: Any, asset_ids: Iterable[str], *,
                    now: float) -> dict[str, Analysis]:
        raise NotImplementedError

    def request_in(self, conn: Any, asset_ids: Iterable[str], *, now: float) -> int:
        raise NotImplementedError

    def asset_to_measure(self, asset_id: str) -> LibraryAsset | None:
        raise NotImplementedError

    def record_measured(self, asset_id: str, key: KeyContent) -> None:
        raise NotImplementedError

    def record_failed(self, asset_id: str, reason: MeasureFailure) -> None:
        raise NotImplementedError
