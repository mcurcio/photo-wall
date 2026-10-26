"""Key content over PostgreSQL (roadmap 5a; run ledger .claude/runs/photo-5a.md, slice M1).

`PgKeyContent` implements central.wall_plan.ports.KeyContentReader (Central: hydration and the
scheduler's measurement requests) and KeyContentWriter (the media worker), and is the one home of
migration 082's `photo_key_content` table: one row per asset id (the id already pins the photo's
revision) at one analysis version. Every time written or compared here is `times`' (the
database's, G11): the worker writes `retry_at`, Central compares it. A stored key that no longer
reads as KeyContent counts as not measured, so it is measured again and replaced.

Content first (owner, 2026-10-11: frames always have content; C7, the pool plays on while Immich
is down): a measurement at an older analysis version is still read until a new one replaces it,
marked `due` (`Measured.due`), and the Planner requests it again. A photo's first measurement
that found no face is provisional: Immich detects faces in a background job some time after the
upload, so it is measured once more after FACELESS_REMEASURE_SECONDS (`retry_at` on a measured
row), and that measurement is final. A failed measurement of a photo that already has a measured
key keeps that key, and only its retry time is recorded (MEASURE_RETRY_SECONDS ahead), so the
photo stays placeable.

The due rule has one home, `due_sql`: a row is due a measurement when its retry time has come,
or, with no retry time set, when it is at an older analysis version. Hydration, the measurement
requests and the fit analysis's "checked" count (central/infra/fit_analysis_store.py) all read it.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from central.db import Database, TransactionClock
from central.infra.photo_library_store import library_assets_in
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
# A first measurement with no face is taken again this long after (Immich's face detection runs
# after the upload; after a large import it can lag by hours). A placeholder to tune.
FACELESS_REMEASURE_SECONDS: Final = 6 * 3600



def due_sql(alias: str) -> str:
    """The due rule (module docstring) over `photo_key_content` row `alias`, as an SQL boolean;
    its parameters are `due_params`'."""
    return (f"COALESCE({alias}.retry_at <= %(due_now)s, "
            f"{alias}.analysis_version <> %(due_version)s)")


def due_params(now: float) -> dict[str, Any]:
    """The named parameters `due_sql` reads, at database time `now`."""
    return {"due_now": now, "due_version": ANALYSIS_VERSION}


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
        """KeyContentReader.analyses_in: a measurement at any analysis version (an older one
        holds until it is replaced), with whether it is due; a failure until its retry."""
        return {asset_id: analysis for asset_id, (analysis, _) in
                self._read(conn, asset_ids, now=now).items() if analysis is not None}

    def _read(self, conn: Any, asset_ids: Iterable[str], *,
              now: float) -> dict[str, tuple[Analysis | None, bool]]:
        """Each stored row's analysis (None: none to use) and whether it is due a measurement."""
        rows = conn.execute(
            "SELECT k.asset_id,k.status,k.key,k.failure," + due_sql("k") + " AS due "
            "FROM photo_key_content k WHERE k.asset_id = ANY(%(ids)s)",
            {"ids": list(dict.fromkeys(asset_ids))} | due_params(now)).fetchall()
        found: dict[str, tuple[Analysis | None, bool]] = {}
        for row in rows:
            due = bool(row["due"])
            if row["status"] == "failed":
                found[row["asset_id"]] = (
                    None if due else MeasureFailed(reason=row["failure"]), due)
                continue
            try:
                key = KeyContent.model_validate(row["key"])
            except ValidationError:
                found[row["asset_id"]] = (None, True)
                continue
            found[row["asset_id"]] = (Measured(key=key, due=due), due)
        return found

    def request_in(self, conn: Any, asset_ids: Iterable[str], *, now: float) -> int:
        """KeyContentReader.request_in, queued in the requested order (highest-ranked first).
        Refuses with media_queue_unconfigured when something is due and there is no queue."""
        wanted = list(dict.fromkeys(asset_ids))
        if not wanted:
            return 0
        present = {row["asset_id"] for row in conn.execute(
            "SELECT asset_id FROM asset_revisions WHERE asset_id = ANY(%s)", (wanted,)).fetchall()}
        stored = self._read(conn, present, now=now)
        due = [asset_id for asset_id in wanted
               if asset_id in present and (asset_id not in stored or stored[asset_id][1])]
        if not due:
            return 0
        if self.queue is None:
            raise RegistryError("media_queue_unconfigured", 503)
        return sum(not self.queue.enqueue_measure_in(conn, asset_id).coalesced
                   for asset_id in due)

    def asset_to_measure(self, asset_id: str) -> LibraryAsset | None:
        """KeyContentWriter.asset_to_measure: the revision joined to its Immich asset, through
        the one read of a photo as a LibraryAsset (`library_assets_in`)."""
        with self.db.transaction() as conn:
            return library_assets_in(conn, (asset_id,)).get(asset_id)

    def record_measured(self, asset_id: str, key: KeyContent) -> None:
        """KeyContentWriter.record_measured; nothing when the asset is gone. A photo's first
        measurement with no face is measured again after FACELESS_REMEASURE_SECONDS."""
        with self.db.transaction() as conn:
            now = self.times.now_in(conn)
            earlier = conn.execute(
                "SELECT 1 FROM photo_key_content WHERE asset_id=%s AND status='measured'",
                (asset_id,)).fetchone()
            again = None if key.faces or earlier else now + FACELESS_REMEASURE_SECONDS
            conn.execute(_WRITE, (key.analysis_version, "measured",
                                  Jsonb(key.model_dump(mode="json")), None, now, again,
                                  asset_id))

    def record_failed(self, asset_id: str, reason: MeasureFailure) -> None:
        """KeyContentWriter.record_failed; nothing when the asset is gone. A photo with a
        measured key keeps it: only its retry time is recorded (module docstring)."""
        with self.db.transaction() as conn:
            now = self.times.now_in(conn)
            kept = conn.execute(
                "UPDATE photo_key_content SET retry_at=%s WHERE asset_id=%s AND status='measured'",
                (now + MEASURE_RETRY_SECONDS, asset_id)).rowcount
            if not kept:
                conn.execute(_WRITE, (ANALYSIS_VERSION, "failed", None, reason, now,
                                      now + MEASURE_RETRY_SECONDS, asset_id))
