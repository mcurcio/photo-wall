"""The fit analysis's reads over PostgreSQL (follow-ups 2026-10-11, slice B2; run ledger
.claude/runs/photo-5a.md "Follow-ups (owner 2026-10-11)").

`PgFitAnalysisReader` implements central.wall_plan.ports.FitAnalysisReader. A selection's photos
are the in-pool rows of its Sources' and drafts' pools, each revision (`asset_id`) counted once
however many pools hold it, with its kind from its Immich asset (`library_assets`) and its
upright size from its revision (`asset_revisions`): owner item 6, the facts live once. Key content
counts as measured only at the current ANALYSIS_VERSION. Read-only; each read is one statement
in its own transaction. The answers carry no Immich id (R22).
"""
from __future__ import annotations

from typing import Any, Final

from pydantic import ValidationError

from central.db import Database, TransactionClock
from central.infra.display_store import frame_outputs_in
from central.infra.photo_library_store import PgPhotoLibrary
from central.installation_models import FrameOutput
from central.wall_plan.fit import MediaKind
from central.wall_plan.fit_analysis import (
    AnalysisRefused,
    FitSelection,
    SamplePhoto,
    SelectionSizes,
    SizeGroup,
)
from central.wall_plan.key_content import ANALYSIS_VERSION, KeyContent

# The selection's in-pool revisions, once each (`%(keys)s`: its pools' keys).
_MEMBERS: Final = ("WITH members AS (SELECT DISTINCT asset_id FROM photo_pool_rows "
                   "WHERE pool_key = ANY(%(keys)s) AND outcome = 'in_pool' AND asset_id IS NOT NULL) ")
# Each member's kind, size and (current version, measured) key content.
_FACTS: Final = ("FROM members m JOIN asset_revisions a USING (asset_id) "
                 "JOIN library_assets l ON l.connection_ref = a.connection_ref "
                 "AND l.upstream_id = a.upstream_id "
                 "LEFT JOIN photo_key_content k ON k.asset_id = m.asset_id "
                 "AND k.analysis_version = %(version)s AND k.status = 'measured' ")


class PgFitAnalysisReader:
    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        """`times` stamps `read_at` (the database's clock in production, G11)."""
        self.db, self.times = db, times

    def selection_sizes(self, selection: FitSelection) -> SelectionSizes:
        """FitAnalysisReader.selection_sizes."""
        with self.db.transaction() as conn:
            keys, reading = self._pools_in(conn, selection)
            groups = conn.execute(
                _MEMBERS + "SELECT l.kind, a.width, a.height, count(*) AS photos, "
                "count(k.asset_id) AS measured " + _FACTS +
                "GROUP BY l.kind, a.width, a.height ORDER BY l.kind, a.width, a.height",
                {"keys": keys, "version": ANALYSIS_VERSION}).fetchall()
            others = conn.execute(
                "SELECT outcome, count(DISTINCT (p.connection_ref, r.upstream_id)) AS n "
                "FROM photo_pool_rows r JOIN photo_pools p USING (pool_key) "
                "WHERE r.pool_key = ANY(%s) AND r.outcome <> 'in_pool' GROUP BY outcome",
                (keys,)).fetchall()
            read_at = self.times.now_in(conn)
        outside = {row["outcome"]: row["n"] for row in others}
        return SelectionSizes(
            groups=tuple(SizeGroup(kind=row["kind"], width=row["width"], height=row["height"],
                                   photos=row["photos"], measured=row["measured"])
                         for row in groups),
            waiting=outside.get("waiting", 0), left_out=outside.get("left_out", 0),
            reading=reading, read_at=read_at)

    def sample(self, selection: FitSelection, sizes: frozenset[tuple[MediaKind, int, int]], *,
               seed: int, limit: int, measured_only: bool) -> tuple[SamplePhoto, ...]:
        """FitAnalysisReader.sample: ordered by md5(asset id and seed), so the same seed and pool
        give the same photos. A stored key that no longer reads as KeyContent is not measured."""
        if not sizes or limit <= 0:
            return ()
        kinds, widths, heights = zip(*sorted(sizes), strict=True)
        with self.db.transaction() as conn:
            keys, _ = self._pools_in(conn, selection)
            rows = conn.execute(
                _MEMBERS + "SELECT m.asset_id, l.kind, a.width, a.height, k.key " + _FACTS +
                "WHERE (l.kind, a.width, a.height) IN (SELECT * FROM unnest("
                "%(kinds)s::text[], %(widths)s::int[], %(heights)s::int[])) "
                + ("AND k.asset_id IS NOT NULL " if measured_only else "") +
                "ORDER BY md5(m.asset_id || ':' || %(seed)s::text), m.asset_id LIMIT %(limit)s",
                {"keys": keys, "version": ANALYSIS_VERSION, "kinds": list(kinds),
                 "widths": list(widths), "heights": list(heights), "seed": seed,
                 "limit": limit}).fetchall()
        photos = (SamplePhoto(asset_id=row["asset_id"], kind=row["kind"], width=row["width"],
                              height=row["height"], key=_key(row["key"])) for row in rows)
        return tuple(photo for photo in photos if photo.key is not None or not measured_only)

    def frame_outputs(self) -> tuple[FrameOutput, ...]:
        """FitAnalysisReader.frame_outputs."""
        with self.db.transaction() as conn:
            return frame_outputs_in(conn)

    @staticmethod
    def _pools_in(conn: Any, selection: FitSelection) -> tuple[list[str], bool]:
        """The selection's pool keys, and whether any of them is still being read (a check
        running, never completed, or no pool yet). AnalysisRefused for an unknown Source or
        draft."""
        keys: list[str] = []
        reading = False
        sources = {row["source_ref"]: row for row in conn.execute(
            "SELECT source_ref, query, pool_key FROM media_sources WHERE source_ref = ANY(%s)",
            (list(selection.sources),)).fetchall()}
        if len(sources) != len(set(selection.sources)):
            raise AnalysisRefused("source_unknown", 404)
        for row in sources.values():
            # The one rule for a Source revision's pool key (a pre-4 row's is its query's).
            key = PgPhotoLibrary._query_key(row["query"], row["pool_key"])
            if key is None:
                reading = True
            else:
                keys.append(key)
        drafts = {row["draft_ref"]: row["pool_key"] for row in conn.execute(
            "SELECT draft_ref, pool_key FROM photo_drafts WHERE draft_ref = ANY(%s)",
            (list(selection.drafts),)).fetchall()}
        if len(drafts) != len(set(selection.drafts)):
            raise AnalysisRefused("draft_unknown", 404)
        keys.extend(drafts.values())
        keys = sorted(set(keys))
        pools = conn.execute("SELECT check_state, last_complete_at FROM photo_pools "
                             "WHERE pool_key = ANY(%s)", (keys,)).fetchall()
        reading = reading or len(pools) != len(keys) or any(_reading(pool) for pool in pools)
        return keys, reading


def _reading(pool: dict) -> bool:
    return pool["check_state"] == "checking" or pool["last_complete_at"] is None


def _key(stored: object) -> KeyContent | None:
    if stored is None:
        return None
    try:
        return KeyContent.model_validate(stored)
    except ValidationError:
        return None
