"""The photo fit rule's tuning values (follow-ups 2026-10-11, slice B1; run ledger
.claude/runs/photo-5a.md "Follow-ups (owner 2026-10-11)").

Owner, 2026-10-11: "Agreed that these should be configurable, and not hardcoded." One typed home,
house-wide, one stored row (migration 086, central/infra/fit_settings_store.py), read by Central
on each planning pass, the authoring check, the candidates read and the fit analysis. Each default
is the value the rule used before it was configurable; each range is the one home of what the
API accepts and the console's control offers (Settings › Photo fit, slice C2).

`crop_limit` is the house crop limit: a Scene whose own crop limit is unset follows it
(`central.wall_plan.selection.SceneSelection.crop_limit`), so the value lives in one place.
`face_padding`, `min_face_share` and `min_salience_spread` are applied when a photo is judged
(central/wall_plan/fit.py), never when it is measured, so a change takes effect at the next
planning pass with nothing measured again.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from pydantic import Field

from contracts.models import Model

# (lowest, highest) accepted for each field, inclusive: the one home of the ranges (the Field
# bounds below and the settings read's `ranges` both come from here).
FIT_SETTING_RANGES: Final[Mapping[str, tuple[float, float]]] = {
    "crop_limit": (0.05, 0.60),
    "face_padding": (0.0, 0.5),
    "min_face_share": (0.0, 0.05),
    "max_lost_interest_density": (1.0, 3.0),
    "min_salience_spread": (0.0, 0.5),
    "large_frame_inches": (1.0, 200.0),
    "large_frame_video_short_side": (1, 4320),
    "window_steps": (8, 256),
}
_R = FIT_SETTING_RANGES


class FitSettings(Model):
    """Every tuning value of the photo fit rule. Frozen and hashable, so the rule's memo is keyed
    by it (central/wall_plan/assignment.py `_judged`)."""

    # The most of a photo's area a Frame may cut away (rule 1), for every Scene without its own.
    crop_limit: float = Field(default=0.35, ge=_R["crop_limit"][0], le=_R["crop_limit"][1],
                              allow_inf_nan=False)
    # Room kept beside each face, as a share of the face's own width (height) on each side.
    face_padding: float = Field(default=0.20, ge=_R["face_padding"][0], le=_R["face_padding"][1],
                                allow_inf_nan=False)
    # A face smaller than this share of the photo's area does not decide a crop.
    min_face_share: float = Field(default=0.002, ge=_R["min_face_share"][0],
                                  le=_R["min_face_share"][1], allow_inf_nan=False)
    # Rule 4: how much denser in interest than the photo's average the removed part may be.
    max_lost_interest_density: float = Field(default=1.25,
                                             ge=_R["max_lost_interest_density"][0],
                                             le=_R["max_lost_interest_density"][1],
                                             allow_inf_nan=False)
    # A saliency map spanning less than this has no part that stands out: judged as even.
    min_salience_spread: float = Field(default=0.05, ge=_R["min_salience_spread"][0],
                                       le=_R["min_salience_spread"][1], allow_inf_nan=False)
    # requirements :186: on a Frame this large or larger a video needs this many rows.
    large_frame_inches: float = Field(default=40.0, ge=_R["large_frame_inches"][0],
                                      le=_R["large_frame_inches"][1], allow_inf_nan=False)
    large_frame_video_short_side: int = Field(default=1080,
                                              ge=_R["large_frame_video_short_side"][0],
                                              le=_R["large_frame_video_short_side"][1])
    # How many steps the crop window is slid across its free axis (precision, not taste).
    window_steps: int = Field(default=64, ge=_R["window_steps"][0], le=_R["window_steps"][1])


class FitSettingsView(Model):
    """GET /v1/operator/settings/photo-fit (slice B1): the saved values, the defaults (Reset) and
    each field's range (FIT_SETTING_RANGES), so the console never copies a bound."""

    settings: FitSettings
    defaults: FitSettings
    ranges: dict[str, tuple[float, float]]


__all__ = ["FIT_SETTING_RANGES", "FitSettings", "FitSettingsView"]
