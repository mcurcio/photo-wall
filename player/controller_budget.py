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
  screen, the most pixels on any one of its physical scanlines (the primary's physical width plus
  every picture crossing that line, along the line, at its placement's size), summed over the
  screens; enlarged = the planes whose picture the controller enlarges to its placement
  (player/planes.py `Plane.enlarged`), over every screen. A 90 or 270 turn lays a picture's hanging
  height along the physical line. An enlarged picture counts its placement along the line, not its
  smaller source (bench T2, case B: five 1080p pictures enlarged to full 4K width glitch as five 4K
  pictures do, though their sources cross only 9,600 pixels), and each one takes line buffer
  memory (LBM): 1080p enlarged to full 4K width, the kernel refused the sixth; four ran clean.

Who gives up a layer over budget is the executor's alone (player/visibility.py `shed`, player/
executor.py), never this module's: it only judges (`fits`).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from player.planes import Composition


@dataclass(frozen=True)
class ControllerBudget:
    """One Pi's display controller limits, for every screen together: `planes` scanned out (each
    screen's primary included), `line_pixels` and `enlarged` planes (module rule)."""
    planes: int
    line_pixels: int
    enlarged: int


PI5: Final = ControllerBudget(planes=16, line_pixels=15_360, enlarged=4)


def load(compositions: Sequence[Composition]) -> tuple[int, int, int]:
    """(planes, line pixels, enlarged planes) of `compositions`, one per screen (module rule). Pure.
    ValueError("budget_screens") when two compositions share an output_id."""
    if len({composition.screen.output_id for composition in compositions}) != len(compositions):
        raise ValueError("budget_screens")
    planes = sum(1 + len(composition.planes) for composition in compositions)
    enlarged = sum(plane.enlarged for composition in compositions for plane in composition.planes)
    return planes, sum(_line_pixels(composition) for composition in compositions), enlarged


def _line_pixels(composition: Composition) -> int:
    """One screen's most source pixels on one physical scanline (module rule): at 0 or 180 a
    physical line is a hanging row and each picture lays its width along it; at 90 or 270 it is a
    hanging column and each picture lays its height along it."""
    width, height = composition.screen.size
    sideways = composition.screen.turn in (90, 270)          # `size` is as the screen hangs
    edges = []                                   # (where, +pixels at a start / -pixels at an end)
    for plane in composition.planes:
        at = plane.at
        start, span, along = (at.x, at.width, at.height) if sideways else (at.y, at.height, at.width)
        edges += [(start, along), (start + span, -along)]
    crossing = most = 0
    for _, change in sorted(edges, key=lambda edge: (edge[0], edge[1] > 0)):   # ends before starts
        crossing += change
        most = max(most, crossing)
    return (height if sideways else width) + most


def fits(budget: ControllerBudget, compositions: Sequence[Composition]) -> bool:
    """Whether `load(compositions)` is within `budget` on every count. Pure; `load`'s errors."""
    planes, line_pixels, enlarged = load(compositions)
    return planes <= budget.planes and line_pixels <= budget.line_pixels and enlarged <= budget.enlarged


__all__ = ["PI5", "ControllerBudget", "fits", "load"]
