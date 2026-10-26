"""Key content: what a photo holds that a crop must keep (roadmap 5a; run ledger
.claude/runs/photo-5a.md). Frame-independent, measured once per photo (its asset id already pins
its revision) and analysis version by the media worker (media/key_content.py), stored by
central/infra/key_content_store.py and hydrated onto `central.catalog.Candidate.analysis`.

Owner, 2026-10-11: "Use immich face detection"; "If immich face detection is unavailable, then
smart crop doesn't take into account faces." So `faces` is empty when Immich found none, has
machine learning off, or the key lacks `face.read`; the interest grid is always measured.

Every box and grid cell is a fraction of the upright, edited image Immich shows: fractions hold
through any scaling, and preparation fetches that same image (roadmap 4, C10).
"""
from __future__ import annotations

import math
from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from contracts.models import Fraction, Model

# A new value re-measures every photo (the store keeps one row per asset; a row at an older
# version is still read, marked `due`, and the Planner requests it again). Bump it when the
# measurement's method or constants change. "2" (follow-ups B1): raw face boxes and the saliency
# spread, padding and thresholds at judge time. "3" (subject detection, 2026-10-26): the interest
# grid is U-2-Netp's "what stands out" map, no longer spectral-residual saliency.
ANALYSIS_VERSION: Final = "3"
# The interest grid's columns; its rows follow the thumbnail's aspect (`interest_rows`).
INTEREST_COLUMNS: Final = 16
# The most rows a grid has (InterestGrid's bound): a very tall photo's rows stop here.
INTEREST_MAX_ROWS: Final = 64


def interest_rows(width: int, height: int) -> int:
    """The rows of a `width` x `height` image's interest grid: INTEREST_COLUMNS scaled by its
    aspect, rounded, 1 to INTEREST_MAX_ROWS (the one home of the rows rule). Pure."""
    return max(1, min(INTEREST_MAX_ROWS, round(INTEREST_COLUMNS * height / width)))


class UnitBox(Model):
    """A box in fractions of the upright image (0..1, origin top-left); left < right and
    top < bottom (validators, slice R1)."""

    left: Fraction
    top: Fraction
    right: Fraction
    bottom: Fraction

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not (self.left < self.right and self.top < self.bottom):
            raise ValueError("a box needs left < right and top < bottom")
        return self


class InterestGrid(Model):
    """A coarse map of where the eye goes: `columns` x `rows` cells over the whole image,
    row-major `weights`, each >= 0 and finite, summing to 1 within 1e-6 (validators, slice R1:
    `len(weights) == columns * rows`). An image with no salient part is the uniform grid."""

    columns: int = Field(ge=1, le=64)
    rows: int = Field(ge=1, le=INTEREST_MAX_ROWS)
    weights: tuple[float, ...]

    @classmethod
    def uniform(cls, columns: int, rows: int) -> InterestGrid:
        """The even grid of `columns` x `rows`: no part stands out, every cell the same share."""
        cells = columns * rows
        return cls(columns=columns, rows=rows, weights=(1.0 / cells,) * cells)

    @model_validator(mode="after")
    def distribution(self) -> Self:
        if len(self.weights) != self.columns * self.rows:
            raise ValueError("an interest grid needs one weight per cell")
        if not all(math.isfinite(w) and w >= 0 for w in self.weights):
            raise ValueError("interest weights must be finite and non-negative")
        if abs(math.fsum(self.weights) - 1) > 1e-6:
            raise ValueError("interest weights must sum to 1")
        return self


class KeyContent(Model):
    """One photo's key content at `analysis_version`, as measured, with no tuning value applied
    (follow-ups 2026-10-11, slice B1): `faces` are Immich's boxes as fractions, clamped and
    unpadded, every non-degenerate box kept; `interest` is the grid of where the photo's subjects
    stand out (media/key_content.py); `spread` is that map's max minus its min. The fit rule pads faces, drops
    small ones and treats a flat map as even when it judges (central/wall_plan/fit.py
    `faces_judged`, `interest_judged`), so a settings change needs no new measurement."""

    analysis_version: str = Field(min_length=1, max_length=16)
    faces: tuple[UnitBox, ...] = ()
    interest: InterestGrid
    spread: float = Field(ge=0, le=1, allow_inf_nan=False)


class Measured(Model):
    """A measured key, and whether it is `due` a new measurement (the key-content store's due
    rule, its one home: an older analysis version, or a provisional measurement past its retry).
    A due key is still judged and placed until a new one replaces it; the Planner requests it."""

    status: Literal["measured"] = "measured"
    key: KeyContent
    due: bool = False


MeasureFailure = Literal["decode", "timeout"]


class MeasureFailed(Model):
    """The measurement failed; the photo is dropped from plans until its retry (the store's
    `retry_at`), then measured again."""

    status: Literal["failed"] = "failed"
    reason: MeasureFailure


Analysis = Annotated[Measured | MeasureFailed, Field(discriminator="status")]
