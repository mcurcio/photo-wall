"""What depends on the wall plan's stores (roadmap 5a; run ledger .claude/runs/photo-5a.md): one
protocol per side. PostgreSQL implementations: central/infra/shown_history_store.py
(`PgShownHistory`, slice O1), central/infra/key_content_store.py (`PgKeyContent`, slice M1), and
for the follow-ups (2026-10-11) central/infra/fit_settings_store.py (`PgFitSettings`, slice B1)
and central/infra/fit_analysis_store.py (`PgFitAnalysisReader`, slice B2).
Every `*_in` method runs in the caller's transaction.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from central.installation_models import FrameOutput
from central.photo_library.model import LibraryAsset
from central.wall_plan.fit import MediaKind
from central.wall_plan.fit_analysis import FitSelection, SamplePhoto, SelectionSizes
from central.wall_plan.fit_settings import FitSettings
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
        """Each asset's analysis: measured at any analysis version, with whether it is `due` a
        new measurement (the store's due rule: an older version, or a retry time come), or
        failed while its retry is not yet due. An asset with neither is absent (not measured)."""
        ...

    def request_in(self, conn: Any, asset_ids: Iterable[str], *, now: float) -> int:
        """Queue a measurement for each asset that has a pool row and no analysis, or one due a
        measurement (the same due rule); one queued measurement per asset at a time. Returns how
        many were queued."""
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


class FitSettingsStore(Protocol):
    """The photo fit settings' one stored row (slice B1, migration 086). A missing row reads as
    `FitSettings()` (today's values), so correctness holds with the table empty."""

    def fit_settings_in(self, conn: Any) -> FitSettings:
        """The saved settings, in the caller's transaction (the Planner's pass, the authoring
        check, the candidates read)."""
        ...

    def fit_settings(self) -> FitSettings:
        """The saved settings, in its own transaction (the routes and the analysis)."""
        ...

    def save_fit_settings(self, settings: FitSettings) -> None:
        """Replace the row (a complete, validated FitSettings; last write wins). The next
        planning pass reads it; nothing is measured again."""
        ...


class FitAnalysisReader(Protocol):
    """The fit analysis's reads (slice B2), each in its own transaction. A selection's photos are
    the union of its Sources' and drafts' in-pool rows, each asset counted once."""

    def selection_sizes(self, selection: FitSelection) -> SelectionSizes:
        """One grouped read: in-pool photos by (kind, width, height) with how many have key
        content not due a new measurement (the key-content store's due rule); the waiting and left-out counts; whether any
        pool is still being read. AnalysisRefused "source_unknown" or "draft_unknown" (404)."""
        ...

    def sample(self, selection: FitSelection, sizes: frozenset[tuple[MediaKind, int, int]], *,
               seed: int, limit: int, measured_only: bool) -> tuple[SamplePhoto, ...]:
        """Up to `limit` of the selection's in-pool photos whose (kind, width, height) is in
        `sizes`, in an order fixed by `seed` (the same seed and pool give the same photos), with
        their key content (`measured_only`: only those measured). () for empty `sizes`."""
        ...

    def frame_outputs(self) -> tuple[FrameOutput, ...]:
        """Every Frame's Frame output (`central.infra.display_store.frame_outputs_in`)."""
        ...

    def display_sizes(self) -> Mapping[str, tuple[int, int] | None]:
        """Each Frame's seen Display's size in mm as built (`display_store.frame_display_mm`;
        None: none seen, or none stated), by frame id: a Frame draft's physical size."""
        ...
