"""A Scene's selection settings (roadmap 5a; run ledger .claude/runs/photo-5a.md): how its Frames
pick from its photos. Authored on the Scene (`central.runtime.Scene.selection`), carried on each
Intent, read by the Planner. Console rows: #43 Order, #44 Don't repeat within N days, and the one
Crop limit row that replaces #48-#52 (design "Text this corrects").
"""
from __future__ import annotations

from typing import Final, Literal

from pydantic import Field

from central.wall_plan.fit_settings import FIT_SETTING_RANGES, FitSettings
from contracts.models import Model

Order = Literal[
    "shuffle",          # default; a keyed hash of (scene, pass, asset), reshuffled once per pass
    "chronological",    # oldest first, continuing after the Scene's last shown photo
    "newest_first",
    "on_this_day",      # photos taken on today's month and day in earlier years first, then shuffle
]
DEFAULT_NO_REPEAT_DAYS: Final = 7          # #44's catalogue default
MAX_NO_REPEAT_DAYS: Final = 365
_CROP_LIMIT_RANGE: Final = FIT_SETTING_RANGES["crop_limit"]


class SceneSelection(Model):
    """No-repeat only ranks (owner, 2026-10-11: "this "no repeat" idea is secondary to always
    ensuring that frames have content"): a photo shown within `no_repeat_days` anywhere on the
    wall ranks after one that was not, and never leaves a Frame blank.

    `crop_limit` None follows the house crop limit (`FitSettings.crop_limit`, follow-ups
    2026-10-11, slice B1), so the value lives in one place; its bounds are the house setting's
    (`FIT_SETTING_RANGES`). `crop_limit_of` is the one resolver."""

    order: Order = "shuffle"
    no_repeat_days: int = Field(default=DEFAULT_NO_REPEAT_DAYS, ge=0, le=MAX_NO_REPEAT_DAYS)
    crop_limit: float | None = Field(default=None, ge=_CROP_LIMIT_RANGE[0],
                                     le=_CROP_LIMIT_RANGE[1], allow_inf_nan=False)


def crop_limit_of(selection: SceneSelection, settings: FitSettings) -> float:
    """The crop limit a Scene's photos are judged at: its own, else the house's. Pure."""
    return settings.crop_limit if selection.crop_limit is None else selection.crop_limit
