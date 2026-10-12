"""The fit analysis's reads over PostgreSQL (follow-ups 2026-10-11, slice B2; run ledger
.claude/runs/photo-5a.md "Follow-ups (owner 2026-10-11)").

`PgFitAnalysisReader` implements central.wall_plan.ports.FitAnalysisReader. A selection's photos
are the in-pool rows of its Sources' and drafts' pools, each revision (`asset_id`) counted once
however many pools hold it, with its kind from its Immich asset (`library_assets`) and its
upright size from its revision (`asset_revisions`): owner item 6, the facts live once. Key content
counts as measured only at the current ANALYSIS_VERSION. Read-only; each read is one statement
in its own transaction. The answers carry no Immich id (R22).

Follow-up S1: a photo is judged at its readable size, the largest copy of it Photo Wall can read
(central/photo_library/rendition.py `readable_of`, the one home of that rule). The reads here
select the rule's inputs (`READABLE_FACTS`: the revision's size and edit state, its original's
type, its connection's full-size copies setting and its measured copies) and apply it in Python;
`readable_sizes_in` is the same read for named photos (the Planner's hydration and its
measurement requests, central/media_repository.py).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from pydantic import ValidationError

from central.db import Database, TransactionClock
from central.infra.display_store import frame_outputs_in
from central.infra.photo_library_store import PgPhotoLibrary
from central.installation_models import FrameOutput
from central.photo_library.rendition import Size, readable_of
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
# The inputs of `readable_of` for a revision `a` joined to its Immich asset `l`. `edited` is
# `LibraryAsset.edited`'s test (an edit-list suffix on the revision) in SQL.
READABLE_FACTS: Final = (
    "l.kind, a.width, a.height, strpos(a.revision, '-') > 0 AS edited, l.original_media_type, "
    "c.full_size_copies, pv.width AS preview_width, pv.height AS preview_height, "
    "fs.width AS fullsize_width, fs.height AS fullsize_height ")
READABLE_JOINS: Final = (
    "LEFT JOIN library_connections c ON c.connection_ref = a.connection_ref "
    "LEFT JOIN asset_copies pv ON pv.asset_id = a.asset_id AND pv.copy = 'preview' "
    "LEFT JOIN asset_copies fs ON fs.asset_id = a.asset_id AND fs.copy = 'fullsize' ")
_FACT_COLUMNS: Final = ("kind", "width", "height", "edited", "original_media_type",
                        "full_size_copies", "preview_width", "preview_height", "fullsize_width",
                        "fullsize_height")
_GROUP_BY: Final = "GROUP BY " + ", ".join(str(n) for n in range(1, len(_FACT_COLUMNS) + 1))
# Each member's kind, size, readable-size facts and (current version, measured) key content.
_FACTS: Final = ("FROM members m JOIN asset_revisions a USING (asset_id) "
                 "JOIN library_assets l ON l.connection_ref = a.connection_ref "
                 "AND l.upstream_id = a.upstream_id " + READABLE_JOINS +
                 "LEFT JOIN photo_key_content k ON k.asset_id = m.asset_id "
                 "AND k.analysis_version = %(version)s AND k.status = 'measured' ")
# A row's facts equal to one of the arrays' (`%(f_<column>)s`; NULL equals NULL).
_FACTS_IN: Final = (
    "EXISTS (SELECT 1 FROM unnest(%(f_kind)s::text[], %(f_width)s::int[], %(f_height)s::int[], "
    "%(f_edited)s::bool[], %(f_original_media_type)s::text[], %(f_full_size_copies)s::bool[], "
    "%(f_preview_width)s::int[], %(f_preview_height)s::int[], %(f_fullsize_width)s::int[], "
    "%(f_fullsize_height)s::int[]) AS w(kind, width, height, edited, original_type, full_size, "
    "preview_width, preview_height, fullsize_width, fullsize_height) "
    "WHERE w.kind = l.kind AND w.width = a.width AND w.height = a.height "
    "AND w.edited = (strpos(a.revision, '-') > 0) "
    "AND w.original_type IS NOT DISTINCT FROM l.original_media_type "
    "AND w.full_size IS NOT DISTINCT FROM c.full_size_copies "
    "AND w.preview_width IS NOT DISTINCT FROM pv.width "
    "AND w.preview_height IS NOT DISTINCT FROM pv.height "
    "AND w.fullsize_width IS NOT DISTINCT FROM fs.width "
    "AND w.fullsize_height IS NOT DISTINCT FROM fs.height) ")


def readable_of_row(row: Any) -> Size | None:
    """`readable_of` over a row selected with READABLE_FACTS."""
    def pair(width: str, height: str) -> Size | None:
        return None if row[width] is None else (row[width], row[height])
    return readable_of(row["kind"], bool(row["edited"]), row["original_media_type"],
                       row["full_size_copies"], (row["width"], row["height"]),
                       pair("preview_width", "preview_height"),
                       pair("fullsize_width", "fullsize_height"))


def readable_sizes_in(conn: Any, asset_ids: Iterable[str]
                      ) -> dict[str, tuple[MediaKind, Size | None]]:
    """Each named revision's kind and readable size (None: not measured yet), in the caller's
    transaction; an id with no revision is absent."""
    ids = sorted(set(asset_ids))
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT a.asset_id, " + READABLE_FACTS + "FROM asset_revisions a "
        "JOIN library_assets l ON l.connection_ref = a.connection_ref "
        "AND l.upstream_id = a.upstream_id " + READABLE_JOINS + "WHERE a.asset_id = ANY(%s)",
        (ids,)).fetchall()
    return {row["asset_id"]: (row["kind"], readable_of_row(row)) for row in rows}


class PgFitAnalysisReader:
    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        """`times` stamps `read_at` (the database's clock in production, G11)."""
        self.db, self.times = db, times

    def selection_sizes(self, selection: FitSelection) -> SelectionSizes:
        """FitAnalysisReader.selection_sizes."""
        with self.db.transaction() as conn:
            keys, reading = self._pools_in(conn, selection)
            facts = conn.execute(
                _MEMBERS + "SELECT " + READABLE_FACTS + ", count(*) AS photos, "
                "count(k.asset_id) AS measured " + _FACTS + _GROUP_BY,
                {"keys": keys, "version": ANALYSIS_VERSION}).fetchall()
            others = conn.execute(
                "SELECT outcome, count(DISTINCT (p.connection_ref, r.upstream_id)) AS n "
                "FROM photo_pool_rows r JOIN photo_pools p USING (pool_key) "
                "WHERE r.pool_key = ANY(%s) AND r.outcome <> 'in_pool' GROUP BY outcome",
                (keys,)).fetchall()
            read_at = self.times.now_in(conn)
        outside = {row["outcome"]: row["n"] for row in others}
        grouped: dict[tuple[str, int, int], list[int]] = {}
        unsized = 0
        for row in facts:
            size = readable_of_row(row)
            if size is None:
                unsized += row["photos"]
                continue
            counts = grouped.setdefault((row["kind"], *size), [0, 0])
            counts[0] += row["photos"]
            counts[1] += row["measured"]
        return SelectionSizes(
            groups=tuple(SizeGroup(kind=kind, width=width, height=height, photos=photos,
                                   measured=measured)
                         for (kind, width, height), (photos, measured) in sorted(grouped.items())),
            size_unknown=unsized, waiting=outside.get("waiting", 0),
            left_out=outside.get("left_out", 0), reading=reading, read_at=read_at)

    def sample(self, selection: FitSelection, sizes: frozenset[tuple[MediaKind, int, int]], *,
               seed: int, limit: int, measured_only: bool) -> tuple[SamplePhoto, ...]:
        """FitAnalysisReader.sample: ordered by md5(asset id and seed), so the same seed and pool
        give the same photos. A stored key that no longer reads as KeyContent is not measured."""
        if not sizes or limit <= 0:
            return ()
        with self.db.transaction() as conn:
            keys, _ = self._pools_in(conn, selection)
            params = {"keys": keys, "version": ANALYSIS_VERSION}
            # The fact groups whose readable size is asked (`selection_sizes`' grouping, read
            # again in this transaction), so the sample filters by the rule's own answer.
            wanted = [row for row in conn.execute(
                _MEMBERS + "SELECT DISTINCT " + READABLE_FACTS + _FACTS, params).fetchall()
                if (size := readable_of_row(row)) is not None and (row["kind"], *size) in sizes]
            if not wanted:
                return ()
            arrays = {"f_" + column: [row[column] for row in wanted] for column in _FACT_COLUMNS}
            rows = conn.execute(
                _MEMBERS + "SELECT m.asset_id, " + READABLE_FACTS + ", k.key " + _FACTS +
                "WHERE " + _FACTS_IN + ("AND k.asset_id IS NOT NULL " if measured_only else "") +
                "ORDER BY md5(m.asset_id || ':' || %(seed)s::text), m.asset_id LIMIT %(limit)s",
                params | arrays | {"seed": seed, "limit": limit}).fetchall()
        photos = []
        for row in rows:
            size = readable_of_row(row)
            if size is not None:
                photos.append(SamplePhoto(asset_id=row["asset_id"], kind=row["kind"],
                                          width=size[0], height=size[1], key=_key(row["key"])))
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
