"""Frame lighting's rules, each with one home. Pure: no I/O, no clock."""
from __future__ import annotations

from collections.abc import Iterable

from central.frame_lighting.model import FrameLightingRefusal, FramesNaming, LightingHouse
from contracts.frame_lighting import FrameLighting
from contracts.node_picture import DisplayLight


def check(lighting: FrameLighting, house: LightingHouse) -> FrameLightingRefusal | None:
    """Whether `lighting` fits `house`: every Frame light names one of its room lights
    (UNKNOWN_ROOM_LIGHT) with the reference kind that light needs (REFERENCE_MISMATCH), its paper
    profile, if any, is known (UNKNOWN_PAPER_PROFILE), and it fits the trial envelope
    (LIGHTING_TOO_LARGE). The first refusal in that order; None when it fits."""
    ...


def stamped(lighting: FrameLighting, recipe_id: str) -> FrameLighting:
    """`lighting` with its paper profile, if any, named under `recipe_id` (the recipe Central bakes
    with today), so the Player fetches the LUT Central serves. Unchanged without a profile."""
    ...


def upgrade_lighting(display: DisplayLight, mat_depth_mm: float) -> FrameLighting:
    """The lighting that shows today's picture: one WashLight of the upgrade's fixed room light at
    `display.matched_lux`, AtFull, the mat's depth kept, and a paper matching the panel
    (whiteness SHEET_WHITENESS, ink density -log10(display.black), no profile). Migration 099's data
    is held to it by a test."""
    ...


def new_frame_lighting(display: DisplayLight, house: LightingHouse) -> FrameLighting:
    """The one lighting a new Frame starts with, which always passes `check` against `house`:
    `upgrade_lighting(display, 0)` with its wash viewing the first "at-full" room light of `house`
    in its order that nobody reports (a fixed light), or no light when there is none."""
    ...


def fits_trial_envelope(lighting: FrameLighting) -> bool:
    """Whether `lighting` fits Position's trial envelope (contracts/node_calibration.py
    MAX_LIGHTING_BYTES of `lighting_text`), so the display host's buffers always carry it."""
    ...


def naming(lightings: Iterable[tuple[str, FrameLighting]]) -> FramesNaming:
    """Which Frames name each room light and paper profile, from (Frame id, lighting) pairs (a Frame
    may appear twice: its saved lighting and its open trial's)."""
    ...


__all__ = ["check", "fits_trial_envelope", "naming", "new_frame_lighting", "stamped", "upgrade_lighting"]
