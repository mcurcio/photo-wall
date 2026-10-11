"""When a planned photo may be on screen (roadmap 5a; run ledger .claude/runs/photo-5a.md, slice
R1). One definition for Central's wall plan, which never lets two Frames' extents of one photo
overlap (design rule 1), and the Player, which applies a kept still from the same instant.

Rules this module owns (one home each):
- **A kept still takes effect** (`kept_from`) at its layer's start plus its fade-in: the first
  draw at full strength. The Player's after-state handling (player/executor.py
  `_apply_after_end`) and the extent below both use it.
- **The on-screen extent** (`on_screen_extent`): from the layer's start to its end, or, for a
  layer whose after-state keeps it ("keep_this_photo"), until a later layer on the same Frame
  replaces what the Frame keeps (`replaces_kept_at`); open-ended when none does. Both ends widen
  by the hand-over guard, 2 x the clock uncertainty bound: two Pis' clocks may each be off by it
  in opposite directions. Every layer counts, covered or see-through.
- **Overlap** (`Extent.overlaps`): half-open intervals; an open end is +infinity.

Rules it does NOT own: which photo a layer shows, or which Frame (Central's wall plan); the clock
uncertainty bound's value (central/coordination.py `CoordinationLimits.max_uncertainty`).
Still open (design "Costs and deferrals"): a layer that fails to draw leaves the old photo up past
its extent, and a rebooted Pi shows its local fallback.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Protocol, Self

from pydantic import model_validator

from contracts.models import AfterEnd, Instant, Model


class Showing(Protocol):
    """What the extent reads of a layer or a planned slot (`contracts.models.Layer` is one)."""

    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...

    @property
    def fade_in(self) -> float: ...

    @property
    def after_end(self) -> AfterEnd: ...


class Extent(Model):
    """When a photo may be visible on one Frame; `end` None is open-ended (a kept still nothing
    replaces within what was planned)."""

    start: Instant
    end: Instant | None = None

    @model_validator(mode="after")
    def forward(self) -> Self:
        if self.end is not None and self.end <= self.start:
            raise ValueError("an extent's end must follow its start")
        return self

    def overlaps(self, other: Extent) -> bool:
        """Half-open overlap: start < other's end and other's start < end (None = +infinity).
        Two extents that only touch do not overlap."""
        return ((other.end is None or self.start < other.end)
                and (self.end is None or other.start < self.end))


def kept_from(showing: Showing) -> float:
    """The instant a "keep_this_photo" after-state takes effect: start + fade_in. Pure."""
    return showing.start + showing.fade_in


def replaces_kept_at(showing: Showing) -> float | None:
    """When `showing` replaces what its Frame keeps: "keep_this_photo" at `kept_from`,
    "keep_nothing" at its start, "leave_as_is" never (None). Pure."""
    if showing.after_end == "keep_this_photo":
        return kept_from(showing)
    if showing.after_end == "keep_nothing":
        return showing.start
    return None


def on_screen_extent(showing: Showing, later_on_same_frame: Iterable[Showing],
                     max_clock_uncertainty: float) -> Extent:
    """The extent of `showing` on its Frame.

    End before the guard: `showing.end` when its after_end is not "keep_this_photo". For a kept
    still: max(`showing.end`, R), where R is the earliest non-None `replaces_kept_at` among the
    `later_on_same_frame` whose start is at or after `showing.start` (the layer itself draws until
    its end; what the Frame keeps holds until R); open-ended (None) when none replaces it.
    Guard g = 2 x `max_clock_uncertainty`: Extent(start=showing.start - g, end=end + g, or None).
    Pure; ValueError for a negative or non-finite uncertainty."""
    if not math.isfinite(max_clock_uncertainty) or max_clock_uncertainty < 0:
        raise ValueError("a non-negative finite clock uncertainty is required")
    guard = 2 * max_clock_uncertainty
    end: float | None = showing.end
    if showing.after_end == "keep_this_photo":
        replaced = [at for later in later_on_same_frame
                    if later is not showing and later.start >= showing.start
                    and (at := replaces_kept_at(later)) is not None]
        end = max(showing.end, min(replaced)) if replaced else None
    return Extent(start=showing.start - guard, end=None if end is None else end + guard)
