"""Frame lighting's vocabulary: the house as a Frame's lighting sees it, which Frames name what, and
the one refusal vocabulary. Pure; frozen values only."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from contracts.room_light import RoomLightId

ReferenceKind = Literal["at-full", "at-reading"]     # contracts/frame_lighting.py AtFull, AtReading


@dataclass(frozen=True)
class LightingHouse:
    """What a Frame's lighting may name: each room light with the reference kind its reporter needs
    (a Sensor's light "at-reading", every other "at-full"), and the paper profiles known (by their
    profile's sha256)."""
    lights: Mapping[RoomLightId, ReferenceKind]
    paper_profiles: frozenset[str]


@dataclass(frozen=True)
class FramesNaming:
    """Which Frames' lighting (saved, or an open Position trial's draft) names each room light and
    each paper profile, by Frame id in order."""
    lights: Mapping[RoomLightId, tuple[str, ...]]
    paper_profiles: Mapping[str, tuple[str, ...]]


class FrameLightingRefusal(StrEnum):
    """Why a Frame's lighting was refused. One casing, one code per concept: `unknown_room_light`
    is the same code House › Lights answers for a room light that is not there."""
    UNKNOWN_ROOM_LIGHT = "unknown_room_light"          # a Frame light names no room light
    REFERENCE_MISMATCH = "reference_mismatch"          # AtFull on a Sensor's light, AtReading on any other
    UNKNOWN_PAPER_PROFILE = "unknown_paper_profile"    # the paper names a profile not on the list
    LIGHTING_TOO_LARGE = "lighting_too_large"          # past the trial envelope (`policy.fits_trial_envelope`)


class FrameLightingRefused(Exception):
    """A refusal the routes answer as {"error": code} with `status`: 422 for every code (the draft
    does not fit the house); one exception handler maps it."""

    def __init__(self, code: FrameLightingRefusal, status: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


__all__ = ["FrameLightingRefusal", "FrameLightingRefused", "FramesNaming", "LightingHouse", "ReferenceKind"]
