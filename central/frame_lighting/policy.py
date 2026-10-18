"""Frame lighting's rules, each with one home. Pure: no I/O, no clock."""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Final

from central.frame_lighting.model import FrameLightingRefusal, FramesNaming, LightingHouse
from central.room_light.model import FixedLight, MeasuredLight
from contracts.frame_lighting import (
    AT_FULL,
    SHEET_WHITENESS,
    AtFull,
    FrameLighting,
    Paper,
    WashLight,
)
from contracts.lut import LutRef
from contracts.node_calibration import MAX_LIGHTING_BYTES, lighting_text
from contracts.node_picture import DisplayLight

UPGRADE_ROOM_LIGHT_ID: Final = "room"       # the one fixed room light every upgraded Frame's wash views
UPGRADE_ROOM_LIGHT_NAME: Final = "Room"
UPGRADE_LUX_MIN: Final = 0.1                # a wash is never 0 lx: an old brightness of 0 % reads this


def check(lighting: FrameLighting, house: LightingHouse) -> FrameLightingRefusal | None:
    """Whether `lighting` fits `house`: every Frame light names one of its room lights
    (UNKNOWN_ROOM_LIGHT) with the reference its room light needs (REFERENCE_MISMATCH: AtReading for
    a light a Sensor measures, AtFull for every other; the one home of that pairing), its paper
    profile, if any, is known (UNKNOWN_PAPER_PROFILE), and it fits the trial envelope
    (LIGHTING_TOO_LARGE). The first refusal in that order; None when it fits."""
    for frame_light in lighting.lights:
        room_light = house.lights.get(frame_light.light)
        if room_light is None:
            return FrameLightingRefusal.UNKNOWN_ROOM_LIGHT
        if isinstance(room_light, MeasuredLight) == isinstance(frame_light.reference, AtFull):
            return FrameLightingRefusal.REFERENCE_MISMATCH
    profile = lighting.paper.profile
    if profile is not None and profile.profile_sha256 not in house.paper_profiles:
        return FrameLightingRefusal.UNKNOWN_PAPER_PROFILE
    if not fits_trial_envelope(lighting):
        return FrameLightingRefusal.LIGHTING_TOO_LARGE
    return None


def stamped(lighting: FrameLighting, recipe_id: str) -> FrameLighting:
    """`lighting` with its paper profile, if any, named under `recipe_id` (the recipe Central bakes
    with today), so the Player fetches the LUT Central serves. Unchanged without a profile."""
    profile = lighting.paper.profile
    if profile is None or profile.recipe_id == recipe_id:
        return lighting
    return lighting.model_copy(update={"paper": lighting.paper.model_copy(
        update={"profile": LutRef(profile.profile_sha256, recipe_id)})})


def upgrade_lighting(display: DisplayLight, mat_depth_mm: float, brightness_share: float) -> FrameLighting:
    """The lighting that shows the picture a Frame showed before room light: one WashLight of
    UPGRADE_ROOM_LIGHT_ID at `display.matched_lux` x `brightness_share` (its Display's old software
    brightness, which multiplied the linear picture; at least UPGRADE_LUX_MIN), AtFull, the mat's
    depth kept, and a paper matching the panel (whiteness SHEET_WHITENESS, ink density
    -log10(display.black), no profile). Migration 099's data is held to it by a test."""
    return FrameLighting(
        mat_depth_mm=mat_depth_mm,
        lights=(WashLight(light=UPGRADE_ROOM_LIGHT_ID, reference=AT_FULL,
                          lux=max(UPGRADE_LUX_MIN, display.matched_lux * brightness_share)),),
        paper=Paper(whiteness=SHEET_WHITENESS, ink_density=-math.log10(display.black), profile=None))


def new_frame_lighting(display: DisplayLight, house: LightingHouse) -> FrameLighting:
    """The one lighting a new Frame starts with, which always passes `check` against `house`:
    `upgrade_lighting(display, 0, 1)` with its wash viewing the first light of `house` that nobody
    reports (a fixed light), in the house's order, or no light when there is none."""
    upgraded = upgrade_lighting(display, 0.0, 1.0)
    fixed = next((light_id for light_id, light in house.lights.items() if isinstance(light, FixedLight)), None)
    if fixed is None:
        return upgraded.model_copy(update={"lights": ()})
    return upgraded.model_copy(update={"lights": tuple(
        light.model_copy(update={"light": fixed}) for light in upgraded.lights)})


def fits_trial_envelope(lighting: FrameLighting) -> bool:
    """Whether `lighting` fits Position's trial envelope (contracts/node_calibration.py
    MAX_LIGHTING_BYTES of `lighting_text`), so the display host's buffers always carry it."""
    return len(lighting_text(lighting).encode()) <= MAX_LIGHTING_BYTES


def naming(lightings: Iterable[tuple[str, FrameLighting]]) -> FramesNaming:
    """Which Frames name each room light and paper profile, from (Frame id, lighting) pairs (a Frame
    may appear twice: its saved lighting and its open trial's)."""
    lights: dict[str, set[str]] = {}
    profiles: dict[str, set[str]] = {}
    for frame_id, lighting in lightings:
        for light in lighting.lights:
            lights.setdefault(light.light, set()).add(frame_id)
        if lighting.paper.profile is not None:
            profiles.setdefault(lighting.paper.profile.profile_sha256, set()).add(frame_id)
    return FramesNaming(lights={key: tuple(sorted(frames)) for key, frames in lights.items()},
                        paper_profiles={key: tuple(sorted(frames)) for key, frames in profiles.items()})


__all__ = ["UPGRADE_LUX_MIN", "UPGRADE_ROOM_LIGHT_ID", "UPGRADE_ROOM_LIGHT_NAME", "check", "fits_trial_envelope",
           "naming", "new_frame_lighting", "stamped", "upgrade_lighting"]
