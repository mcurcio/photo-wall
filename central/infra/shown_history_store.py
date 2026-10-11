"""Shown history over PostgreSQL (roadmap 5a; run ledger .claude/runs/photo-5a.md, slice O1).

`PgShownHistory` implements central.wall_plan.ports.ShownHistory and is the one home of migration
083's tables:
- `photo_selections`: what each offered layer shows (one row per assignment id, first write
  wins), written by coordination when it offers a plan;
- `shown_photos`: one row per layer its Player reported presented (owner, 2026-10-11: "“Shown”
  means it was displayed on a frame"), copied from its selection, written by coordination's
  `observe`.
Times are the layers' planned intervals on Central's clock (the coordinator's), as the Planner
compares them; `presented_at` is the observation's receipt time on that same clock.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from central.wall_plan.order import PhotoShowing, WallHistory

# How long a shown row is kept. Longer than the longest no-repeat window (365 days), so a pass
# older than this restarts from what remains (design parked note "Shown-history retention").
SHOWN_KEEP_SECONDS: Final = 400 * 86400
# How long a selection is kept after its layer ends (its presented report may arrive late).
SELECTION_KEEP_SECONDS: Final = 86400


class PgShownHistory:
    """Stateless; every method runs in the caller's transaction (coordination's)."""

    def record_selected_in(self, conn: Any, showings: Iterable[PhotoShowing]) -> None:
        raise NotImplementedError

    def record_presented_in(self, conn: Any, assignment_id: str, presented_at: float) -> bool:
        raise NotImplementedError

    def wall_history_in(self, conn: Any, scene_ids: Iterable[str], *, now: float,
                        window_seconds: float) -> WallHistory:
        raise NotImplementedError

    def prune_in(self, conn: Any, *, now: float) -> None:
        raise NotImplementedError
