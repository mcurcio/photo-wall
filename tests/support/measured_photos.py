"""Record photos' key content through the store the media worker writes it with (roadmap 5a, run
ledger .claude/runs/photo-5a.md): the Planner places a photo only once it is measured, so the
database tests that drive a real advance measure their pool photos here first."""

from __future__ import annotations

from collections.abc import Iterable

from central.db import ProcessTransactionClock
from central.infra.key_content_store import PgKeyContent
from central.photo_library.model import LibraryAsset
from central.wall_plan.key_content import (
    ANALYSIS_VERSION,
    INTEREST_COLUMNS,
    InterestGrid,
    KeyContent,
    UnitBox,
)
from support.key_content_builders import uniform_grid


def measure(registry, photo: LibraryAsset, *, faces: tuple[UnitBox, ...] = (),
            grid: InterestGrid | None = None) -> None:
    """`photo` measured at the current analysis version (an even interest grid unless named).
    The photo must already have its asset row (seed its pool first)."""
    rows = max(1, min(64, round(INTEREST_COLUMNS * photo.height / photo.width)))
    PgKeyContent(registry.db, times=ProcessTransactionClock(registry.clock)).record_measured(
        photo.asset_id, KeyContent(analysis_version=ANALYSIS_VERSION, faces=faces,
                                   interest=grid or uniform_grid(INTEREST_COLUMNS, rows)))


def measure_all(registry, photos: Iterable[LibraryAsset]) -> None:
    for photo in photos:
        measure(registry, photo)
