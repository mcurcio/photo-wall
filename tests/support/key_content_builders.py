"""Test builders for key content (roadmap 5a, run ledger .claude/runs/photo-5a.md, slice R1):
interest grids, face boxes and a measured candidate. Test-only; shared by the fit rule's tests,
the wall assignment's and the tracer."""
from __future__ import annotations

from central.catalog import Candidate
from central.wall_plan.key_content import (
    ANALYSIS_VERSION,
    INTEREST_COLUMNS,
    InterestGrid,
    KeyContent,
    Measured,
    UnitBox,
)
from contracts.models import Variant


def uniform_grid(columns: int = 16, rows: int = 9) -> InterestGrid:
    """No salient part: every cell holds the same share."""
    cells = columns * rows
    return InterestGrid(columns=columns, rows=rows, weights=(1 / cells,) * cells)


def _band_share(x0: float, x1: float, y0: float, y1: float, *, left: float, right: float,
                top: float, bottom: float) -> float:
    """The share of the cell [x0, x1] x [y0, y1] inside the union of the edge bands: one less
    the share inside the middle rectangle the bands leave."""
    wide = max(0.0, min(x1, 1 - right) - max(x0, left)) / (x1 - x0)
    tall = max(0.0, min(y1, 1 - bottom) - max(y0, top)) / (y1 - y0)
    return 1 - wide * tall


def banded_grid(*, columns: int = 16, rows: int = 9, left: float = 0, right: float = 0,
                top: float = 0, bottom: float = 0, weight: float = 0.9) -> InterestGrid:
    """`weight` of the interest spread evenly over the named edge bands (each a fraction of the
    width or height), the rest spread evenly over the whole image."""
    cells = columns * rows
    band = [_band_share(c / columns, (c + 1) / columns, r / rows, (r + 1) / rows,
                        left=left, right=right, top=top, bottom=bottom)
            for r in range(rows) for c in range(columns)]
    total = sum(band)
    if total <= 0:
        raise ValueError("name at least one edge band")
    weights = [weight * b / total + (1 - weight) / cells for b in band]
    scale = sum(weights)
    return InterestGrid(columns=columns, rows=rows, weights=tuple(w / scale for w in weights))


def face(left: float, top: float, right: float, bottom: float) -> UnitBox:
    return UnitBox(left=left, top=top, right=right, bottom=bottom)


def measured(asset_id: str, width: int, height: int, *, faces: tuple[UnitBox, ...] = (),
             grid: InterestGrid | None = None, kind: str = "image",
             captured_at: float = 0, variant: Variant | None = None,
             spread: float = 1.0) -> Candidate:
    """A hydrated candidate with `Measured` key content at ANALYSIS_VERSION: `faces` as Immich
    found them (the fit rule pads them when it judges); `spread` the saliency map's (1.0: the
    grid counts as it is)."""
    return Candidate(asset_id=asset_id, kind=kind, original_width=width, original_height=height,
                     captured_at=captured_at, variant=variant,
                     analysis=Measured(key=key_content(width, height, faces=faces, grid=grid,
                                                       spread=spread)))


def key_content(width: int, height: int, *, faces: tuple[UnitBox, ...] = (),
                grid: InterestGrid | None = None, spread: float = 1.0) -> KeyContent:
    """Key content at ANALYSIS_VERSION for a `width` x `height` photo (uniform grid by default)."""
    if grid is None:
        grid = uniform_grid(INTEREST_COLUMNS, max(1, min(64, round(INTEREST_COLUMNS * height / width))))
    return KeyContent(analysis_version=ANALYSIS_VERSION, faces=faces, interest=grid, spread=spread)
