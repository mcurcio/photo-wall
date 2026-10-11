"""Key content over PostgreSQL (roadmap 5a; run ledger .claude/runs/photo-5a.md, slice M1).

`PgKeyContent` implements central.wall_plan.ports.KeyContentReader (Central: hydration and the
scheduler's measurement requests) and KeyContentWriter (the media worker), and is the one home of
migration 082's `photo_key_content` table: one row per asset id (the id already pins the photo's
revision) at one analysis version. Every time written or compared here is `times`' (the
database's, G11): the worker writes `retry_at`, Central compares it. A stored key that no longer
reads as KeyContent counts as not measured, so it is measured again and replaced.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from central.db import Database, TransactionClock
from central.media_queue import MediaTaskQueue
from central.photo_library.model import LibraryAsset
from central.registry import RegistryError
from central.wall_plan.key_content import (
    ANALYSIS_VERSION,
    Analysis,
    KeyContent,
    Measured,
    MeasureFailed,
    MeasureFailure,
)

# A failed measurement's photo is left out of plans this long, then measured again.
MEASURE_RETRY_SECONDS: Final = 3600

# One row per asset, replaced by each new measurement; nothing when the asset row is gone.
_WRITE = ("INSERT INTO photo_key_content(asset_id,analysis_version,status,key,failure,"
          "measured_at,retry_at) SELECT asset_id,%s,%s,%s,%s,%s,%s FROM asset_revisions "
          "WHERE asset_id=%s ON CONFLICT (asset_id) DO UPDATE SET "
          "analysis_version=EXCLUDED.analysis_version,status=EXCLUDED.status,key=EXCLUDED.key,"
          "failure=EXCLUDED.failure,measured_at=EXCLUDED.measured_at,retry_at=EXCLUDED.retry_at")


class PgKeyContent:
    def __init__(self, db: Database, *, times: TransactionClock,
                 queue: MediaTaskQueue | None = None) -> None:
        """`queue` queues measurements in the requesting transaction (None: a composition that
        never requests, whose `request_in` refuses with media_queue_unconfigured)."""
        self.db, self.times, self.queue = db, times, queue

    def analyses_in(self, conn: Any, asset_ids: Iterable[str], *,
                    now: float) -> dict[str, Analysis]:
        """KeyContentReader.analyses_in."""
        rows = conn.execute(
            "SELECT asset_id,status,key,failure,retry_at FROM photo_key_content "
            "WHERE asset_id = ANY(%s) AND analysis_version=%s",
            (list(dict.fromkeys(asset_ids)), ANALYSIS_VERSION)).fetchall()
        found: dict[str, Analysis] = {}
        for row in rows:
            if row["status"] == "failed":
                if row["retry_at"] > now:
                    found[row["asset_id"]] = MeasureFailed(reason=row["failure"])
                continue
            try:
                found[row["asset_id"]] = Measured(key=KeyContent.model_validate(row["key"]))
            except ValidationError:
                continue
        return found

    def request_in(self, conn: Any, asset_ids: Iterable[str], *, now: float) -> int:
        """KeyContentReader.request_in, queued in the requested order (highest-ranked first).
        Refuses with media_queue_unconfigured when something is due and there is no queue."""
        wanted = list(dict.fromkeys(asset_ids))
        if not wanted:
            return 0
        present = {row["asset_id"] for row in conn.execute(
            "SELECT asset_id FROM asset_revisions WHERE asset_id = ANY(%s)", (wanted,)).fetchall()}
        current = self.analyses_in(conn, present, now=now)
        due = [asset_id for asset_id in wanted if asset_id in present and asset_id not in current]
        if not due:
            return 0
        if self.queue is None:
            raise RegistryError("media_queue_unconfigured", 503)
        return sum(not self.queue.enqueue_measure_in(conn, asset_id).coalesced
                   for asset_id in due)

    def asset_to_measure(self, asset_id: str) -> LibraryAsset | None:
        """KeyContentWriter.asset_to_measure. The row's metadata is a LibraryAsset: the rule's
        one home is central/media_repository.py `lease_asset_of` (central.infra cannot import
        the repository, which builds this store), repeated here in one line."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT metadata FROM asset_revisions WHERE asset_id=%s",
                               (asset_id,)).fetchone()
        return None if row is None else LibraryAsset.model_validate(row["metadata"])

    def record_measured(self, asset_id: str, key: KeyContent) -> None:
        """KeyContentWriter.record_measured; nothing when the asset is gone."""
        with self.db.transaction() as conn:
            conn.execute(_WRITE, (key.analysis_version, "measured",
                                  Jsonb(key.model_dump(mode="json")), None,
                                  self.times.now_in(conn), None, asset_id))

    def record_failed(self, asset_id: str, reason: MeasureFailure) -> None:
        """KeyContentWriter.record_failed; nothing when the asset is gone."""
        with self.db.transaction() as conn:
            now = self.times.now_in(conn)
            conn.execute(_WRITE, (ANALYSIS_VERSION, "failed", None, reason, now,
                                  now + MEASURE_RETRY_SECONDS, asset_id))
