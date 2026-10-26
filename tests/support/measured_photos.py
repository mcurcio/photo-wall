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
    interest_rows,
)
from support.key_content_builders import uniform_grid


def measure(registry, photo: LibraryAsset, *, faces: tuple[UnitBox, ...] = (),
            grid: InterestGrid | None = None) -> None:
    """`photo` measured at the current analysis version (an even interest grid unless named),
    final: recorded twice, as a faceless first measurement is once retaken, so it is never due
    again whichever clock a test's Planner reads it by (the manual one it is written with, or
    Central's database time). The photo must already have its asset row (seed its pool first)."""
    store = PgKeyContent(registry.db, times=ProcessTransactionClock(registry.clock))
    key = KeyContent(analysis_version=ANALYSIS_VERSION, faces=faces,
                     interest=grid or uniform_grid(INTEREST_COLUMNS,
                                                       interest_rows(photo.width, photo.height)), spread=1.0)
    for _ in range(2):
        store.record_measured(photo.asset_id, key)


def measure_all(registry, photos: Iterable[LibraryAsset]) -> None:
    for photo in photos:
        measure(registry, photo)
