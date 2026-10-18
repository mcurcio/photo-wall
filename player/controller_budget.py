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
from dataclasses import dataclass, replace
from typing import Final

from contracts.models import Layer
from player.planes import Composition
from player.visibility import shed


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
    if len({composition.screen.output_id for composition in compositions}) != len(compositions):
        raise ValueError("budget_screens")
    planes = sum(1 + len(composition.planes) for composition in compositions)
    return planes, sum(_line_pixels(composition) for composition in compositions)


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
    """Whether `load(compositions)` is within `budget` on both counts. Pure; `load`'s errors."""
    planes, line_pixels = load(compositions)
    return planes <= budget.planes and line_pixels <= budget.line_pixels


def within(budget: ControllerBudget,
           compositions: Sequence[Composition]) -> tuple[tuple[Composition, ...], tuple[str, ...]]:
    """`compositions` cut to fit `budget` (module rule): the compositions in the order given, and
    the assignment ids of the planes that gave up their place, in the order they did. When even one
    plane per Frame does not fit, the cut stops there and the caller, finding `fits` false, refuses
    the update (readiness failure "capacity"). Pure; `load`'s errors."""
    kept = [list(composition.planes) for composition in compositions]
    gone: list[str] = []
    while not fits(budget, [replace(c, planes=tuple(p)) for c, p in zip(compositions, kept)]):
        frames: dict[str, list[Layer]] = {}          # screens in order, so shed's ties pick the earlier
        owner: dict[int, tuple[int, int]] = {}
        for screen, planes in enumerate(kept):
            for index, plane in enumerate(planes):
                frames.setdefault(f"{screen}/{plane.frame_id}", []).append(plane.layer.layer)
                owner[id(plane.layer.layer)] = (screen, index)
        given_up = shed(frames)
        if given_up is None:
            break
        screen, index = owner[id(given_up)]
        kept[screen].pop(index)
        gone.append(given_up.assignment_id)
    return tuple(replace(c, planes=tuple(p)) for c, p in zip(compositions, kept)), tuple(gone)


__all__ = ["PI5", "ControllerBudget", "fits", "load", "within"]
