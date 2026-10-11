"""A Scene's selection settings (roadmap 5a; run ledger .claude/runs/photo-5a.md): how its Frames
pick from its photos. Authored on the Scene (`central.runtime.Scene.selection`), carried on each
Intent, read by the Planner. Console rows: #43 Order, #44 Don't repeat within N days, and the one
Crop limit row that replaces #48-#52 (design "Text this corrects").
"""
from __future__ import annotations

from typing import Final, Literal

from pydantic import Field

from central.wall_plan.fit import DEFAULT_CROP_LIMIT
from contracts.models import Model

Order = Literal[
    "shuffle",          # default; a keyed hash of (scene, pass, asset), reshuffled once per pass
    "chronological",    # oldest first, continuing after the Scene's last shown photo
    "newest_first",
    "on_this_day",      # photos taken on today's month and day in earlier years first, then shuffle
]
DEFAULT_NO_REPEAT_DAYS: Final = 7          # #44's catalogue default
MAX_NO_REPEAT_DAYS: Final = 365
# The crop limit's bounds: a limit of 0 would refuse every photo whose aspect differs at all; one
# of 1 would accept any crop. The console's percent field takes the same range.
MIN_CROP_LIMIT: Final = 0.05
MAX_CROP_LIMIT: Final = 0.60


class SceneSelection(Model):
    """No-repeat only ranks (owner, 2026-10-11: "this "no repeat" idea is secondary to always
    ensuring that frames have content"): a photo shown within `no_repeat_days` anywhere on the
    wall ranks after one that was not, and never leaves a Frame blank."""

    order: Order = "shuffle"
    no_repeat_days: int = Field(default=DEFAULT_NO_REPEAT_DAYS, ge=0, le=MAX_NO_REPEAT_DAYS)
    crop_limit: float = Field(default=DEFAULT_CROP_LIMIT, ge=MIN_CROP_LIMIT, le=MAX_CROP_LIMIT,
                              allow_inf_nan=False)
