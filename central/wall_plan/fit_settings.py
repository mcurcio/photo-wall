"""The photo fit rule's tuning values (follow-ups 2026-10-11, slice B1; run ledger
.claude/runs/photo-5a.md "Follow-ups (owner 2026-10-11)").

Owner, 2026-10-11: "Agreed that these should be configurable, and not hardcoded." One typed home,
house-wide, one stored row (migration 086, central/infra/fit_settings_store.py), read by Central
on each planning pass, the authoring check, the candidates read and the fit analysis. Each range
is the one home of what the API accepts and the console's control offers (Settings › Photo fit,
slice C2); each default here is the one home of its value (the console reads it as `defaults`).

Defaults loosened 2026-10-12 (owner, verbatim: "the default values are very conservative and
exclude many photos"): `face_padding` 0.20 -> 0.10 and `max_lost_interest_density` 1.25 -> 1.75.
`crop_limit` stays 0.35: it is what keeps a portrait photo off a landscape Frame (the nearest
pair, 4:5 on 5:4, cuts 36 %; owner, 2026-10-11: a portrait photo never lands on a landscape
Frame), so a square on 16:9 (44 %) stays out. A saved row keeps the values it was saved with;
each row's Reset brings the new default.

`crop_limit` is the house crop limit: a Scene whose own crop limit is unset follows it
(`central.wall_plan.selection.SceneSelection.crop_limit`), so the value lives in one place.
`face_padding`, `min_face_share`, `min_salience_spread` and `interest_skew` are applied when a photo is judged
(central/wall_plan/fit.py), never when it is measured, so a change takes effect at the next
planning pass with nothing measured again.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Final

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
    "interest_skew": (0.0, 1.0),
    "large_frame_inches": (1.0, 200.0),
    "large_frame_video_short_side": (1, 4320),
    "window_steps": (8, 256),
    "max_still_edge": (1024, 16384),
}
# Each step of `FitSettings.copy_ladder_px`, inclusive (a tuple has no one range the console's
# controls offer, so it is not in FIT_SETTING_RANGES).
COPY_STEP_RANGE: Final[tuple[int, int]] = (640, 16384)
_R = FIT_SETTING_RANGES


class FitSettings(Model):
    """Every tuning value of the photo fit rule. Frozen and hashable, so the rule's memo is keyed
    by it (central/wall_plan/assignment.py `_judged`)."""

    # The most of a photo's area a Frame may cut away (rule 1), for every Scene without its own.
    crop_limit: float = Field(default=0.35, ge=_R["crop_limit"][0], le=_R["crop_limit"][1],
                              allow_inf_nan=False)
    # Room kept beside each face, as a share of the face's own width (height) on each side: a
    # tenth keeps a face whole with a margin without a close-up's face claiming most of the photo.
    face_padding: float = Field(default=0.10, ge=_R["face_padding"][0], le=_R["face_padding"][1],
                                allow_inf_nan=False)
    # A face smaller than this share of the photo's area does not decide a crop.
    min_face_share: float = Field(default=0.002, ge=_R["min_face_share"][0],
                                  le=_R["min_face_share"][1], allow_inf_nan=False)
    # Rule 4: how much denser in interest than the photo's average the removed part may be. At
    # 1.75 the texture spectral-residual saliency found at a photo's edges (grass, sky, foliage)
    # no longer refuses a crop, while a subject on both cut sides (a 4:3 photo on 16:9 with 90 %
    # of its interest in its top and bottom quarters, 1.9 times its share) still does.
    max_lost_interest_density: float = Field(default=1.75,
                                             ge=_R["max_lost_interest_density"][0],
                                             le=_R["max_lost_interest_density"][1],
                                             allow_inf_nan=False)
    # A map of what stands out spanning less than this has no part that does: judged as even.
    min_salience_spread: float = Field(default=0.05, ge=_R["min_salience_spread"][0],
                                       le=_R["min_salience_spread"][1], allow_inf_nan=False)
    # How far a photo with faces leans toward what stands out (central/wall_plan/fit.py `place`):
    # the share of the gap between the faces' centre and the interest's that the window moves,
    # and of the room each face keeps, so every face keeps at least 1 - this of the room the
    # faces-only window gave it. 0: the faces alone place the window. Owner, 2026-10-25: "It
    # could bias towards face centering but also allow a skew to include extra content"; the
    # default is the design's recommendation (subject detection, 2026-10-26), a revisable choice.
    # A row saved before this field reads it at its default (`Model` fills a missing field).
    interest_skew: float = Field(default=0.33, ge=_R["interest_skew"][0],
                                 le=_R["interest_skew"][1], allow_inf_nan=False)
    # requirements :186: on a Frame this large or larger a video needs this many rows.
    large_frame_inches: float = Field(default=40.0, ge=_R["large_frame_inches"][0],
                                      le=_R["large_frame_inches"][1], allow_inf_nan=False)
    large_frame_video_short_side: int = Field(default=1080,
                                              ge=_R["large_frame_video_short_side"][0],
                                              le=_R["large_frame_video_short_side"][1])
    # How many steps the crop window is slid across its free axis (precision, not taste).
    window_steps: int = Field(default=64, ge=_R["window_steps"][0], le=_R["window_steps"][1])
    # Follow-up to 5a, slice S1: the longest edge a Player shows a still at (a placeholder, to be
    # measured on the bench Pi 5 from its renderer's texture limit). A Frame whose need for a
    # photo's copy is longer refuses the photo ("above_player_limit"); no copy is longer.
    max_still_edge: int = Field(default=4096, ge=_R["max_still_edge"][0],
                                le=_R["max_still_edge"][1])
    # The coarse steps of a photo's one copy (owner, 2026-10-12: one copy per photo, the Player
    # composes): a copy's long edge is the smallest step holding the wall's largest need for it,
    # so a smaller Frame added, or a photo moved between Frames, prepares nothing again.
    copy_ladder_px: tuple[Annotated[int, Field(ge=COPY_STEP_RANGE[0], le=COPY_STEP_RANGE[1])],
                          ...] = Field(default=(1920, 2560, 3840), min_length=1, max_length=8)

    @property
    def copy_steps(self) -> tuple[int, ...]:
        """The steps a copy's long edge takes, ascending: each of `copy_ladder_px` below
        `max_still_edge`, then `max_still_edge` (central/photo_library/rendition.py
        `wall_need`)."""
        return (*sorted({step for step in self.copy_ladder_px if step < self.max_still_edge}),
                self.max_still_edge)


class FitSettingsView(Model):
    """GET /v1/operator/settings/photo-fit (slice B1): the saved values, the defaults (Reset) and
    each field's range (FIT_SETTING_RANGES), so the console never copies a bound."""

    settings: FitSettings
    defaults: FitSettings
    ranges: dict[str, tuple[float, float]]


__all__ = ["COPY_STEP_RANGE", "FIT_SETTING_RANGES", "FitSettings", "FitSettingsView"]
