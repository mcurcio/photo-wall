"""The display controller's budget: what one Pi's controller can show without glitching, checked
before every screen update (render redesign design rule 3, run ledger .claude/runs/render-planes/
s1-page.md). Pure: no GPU, Wayland or clock.

The Pi 5 never refuses an overload; it glitches (bench T2, /Volumes/Dock/tmp/pi-render-bench/test2):
so the Player never asks for one. Measured at the controller's fixed 500 MHz on one 4K port:
16 planes for both ports together, and about 15,360 source pixels crossing one output line (a
screen's black backdrop, Weston's primary plane, counting its full width). Shared by two ports is
an estimate: the test Pi has no second screen (owner, gate answer Q6, 2026-10-18).

Rules this module owns (one home each):
- **The limits** (`PI5`): the measured numbers, nowhere else.
- **The load** (`load`): planes = each screen's primary plus its pictures; line pixels = for each
  screen, the most source pixels on any one of its physical scanlines (the primary's physical
  width plus every picture crossing that line, along the line), summed over the screens. A 90 or
  270 turn lays a picture's hanging height along the physical line.
- **The cut** (`within`): over budget, the bottom-most plane of the Frame drawing the most planes
  (ties: the lower bottom plane, contracts/models.py `stack_order`, then the earlier screen) gives
  up its place, never a Frame's only plane: player/visibility.py `shed`'s order, for planes.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from player.planes import Composition


@dataclass(frozen=True)
class ControllerBudget:
    """One Pi's display controller limits, for every screen together: `planes` scanned out (each
    screen's primary included) and `line_pixels` (module rule)."""
    planes: int
    line_pixels: int


PI5: Final = ControllerBudget(planes=16, line_pixels=15_360)


def load(compositions: Sequence[Composition]) -> tuple[int, int]:
    """(planes, line pixels) of `compositions`, one per screen (module rule). Pure.
    ValueError("budget_screens") when two compositions share an output_id."""
    raise NotImplementedError("render-planes S1 bead B1")


def fits(budget: ControllerBudget, compositions: Sequence[Composition]) -> bool:
    """Whether `load(compositions)` is within `budget` on both counts. Pure; `load`'s errors."""
    raise NotImplementedError("render-planes S1 bead B1")


def within(budget: ControllerBudget,
           compositions: Sequence[Composition]) -> tuple[tuple[Composition, ...], tuple[str, ...]]:
    """`compositions` cut to fit `budget` (module rule): the compositions in the order given, and
    the assignment ids of the planes that gave up their place, in the order they did. When even one
    plane per Frame does not fit, the cut stops there and the caller, finding `fits` false, refuses
    the update (readiness failure "capacity"). Pure; `load`'s errors."""
    raise NotImplementedError("render-planes S1 bead B1")


__all__ = ["PI5", "ControllerBudget", "fits", "load", "within"]
