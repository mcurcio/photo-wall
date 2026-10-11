"""What depends on the wall plan's stores (roadmap 5a; run ledger .claude/runs/photo-5a.md): one
protocol per side. PostgreSQL implementations: central/infra/shown_history_store.py
(`PgShownHistory`, slice O1) and central/infra/key_content_store.py (`PgKeyContent`, slice M1).
Every `*_in` method runs in the caller's transaction.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from central.photo_library.model import LibraryAsset
from central.wall_plan.key_content import Analysis, KeyContent, MeasureFailure
from central.wall_plan.order import PhotoShowing, WallHistory


class ShownHistory(Protocol):
    """Coordination's side of shown history (slice O1)."""

    def record_selected_in(self, conn: Any, showings: Iterable[PhotoShowing]) -> None:
        """Record what each newly offered layer shows: one row per assignment id, the first
        write wins (a re-offer of a locked layer changes nothing)."""
        ...

    def record_presented_in(self, conn: Any, assignment_id: str, presented_at: float) -> bool:
        """Append the shown row for a layer its Player reported presented, from its recorded
        selection; idempotent per assignment id. False when no selection names it (a layer
        offered before 5a, or a black layer): nothing is written."""
        ...

    def wall_history_in(self, conn: Any, scene_ids: Iterable[str], *, now: float,
                        window_seconds: float) -> WallHistory:
        """Shown rows ending at or after now - window_seconds, plus every row of each named
        Scene's current pass; selections of layers ending after `now`; each Frame's kept still."""
        ...

    def prune_in(self, conn: Any, *, now: float) -> None:
        """Delete selections that ended a day ago and shown rows older than SHOWN_KEEP_SECONDS."""
        ...


class KeyContentReader(Protocol):
    """Hydration's side (slice M1): `MediaRepository._hydrate_candidates` sets
    `Candidate.analysis` from it."""

    def analyses_in(self, conn: Any, asset_ids: Iterable[str], *,
                    now: float) -> dict[str, Analysis]:
        """Each asset's analysis at the current ANALYSIS_VERSION: measured, or failed while its
        retry is not yet due. An asset with neither is absent (not measured)."""
        ...

    def request_in(self, conn: Any, asset_ids: Iterable[str], *, now: float) -> int:
        """Queue a measurement for each asset that has a pool row and no current analysis (a
        failed one only once its retry is due); one queued measurement per asset at a time.
        Returns how many were queued."""
        ...


class KeyContentWriter(Protocol):
    """The media worker's side (slice M1)."""

    def asset_to_measure(self, asset_id: str) -> LibraryAsset | None:
        """The asset row a measurement reads (None: the asset is gone)."""
        ...

    def record_measured(self, asset_id: str, key: KeyContent) -> None: ...

    def record_failed(self, asset_id: str, reason: MeasureFailure) -> None:
        """Record a failure; its retry is due MEASURE_RETRY_SECONDS later (database time)."""
        ...
