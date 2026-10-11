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

from typing import Annotated, Final, Literal

from pydantic import Field

from contracts.models import Fraction, Model

# A new value re-measures every photo (the store keeps one row per asset; hydration reads a row
# only at this version). Bump it when the measurement's method or constants change.
ANALYSIS_VERSION: Final = "1"
# The interest grid's columns; its rows follow the thumbnail's aspect (at least 1).
INTEREST_COLUMNS: Final = 16


class UnitBox(Model):
    """A box in fractions of the upright image (0..1, origin top-left); left < right and
    top < bottom (validators, slice R1)."""

    left: Fraction
    top: Fraction
    right: Fraction
    bottom: Fraction


class InterestGrid(Model):
    """A coarse map of where the eye goes: `columns` x `rows` cells over the whole image,
    row-major `weights`, each >= 0 and finite, summing to 1 within 1e-6 (validators, slice R1:
    `len(weights) == columns * rows`). An image with no salient part is the uniform grid."""

    columns: int = Field(ge=1, le=64)
    rows: int = Field(ge=1, le=64)
    weights: tuple[float, ...]


class KeyContent(Model):
    """One photo's key content at `analysis_version`: faces (padded, each kept whole by any
    crop) and the interest grid."""

    analysis_version: str = Field(min_length=1, max_length=16)
    faces: tuple[UnitBox, ...] = ()
    interest: InterestGrid


class Measured(Model):
    status: Literal["measured"] = "measured"
    key: KeyContent


MeasureFailure = Literal["decode", "timeout"]


class MeasureFailed(Model):
    """The measurement failed; the photo is dropped from plans until its retry (the store's
    `retry_at`), then measured again."""

    status: Literal["failed"] = "failed"
    reason: MeasureFailure


Analysis = Annotated[Measured | MeasureFailed, Field(discriminator="status")]
