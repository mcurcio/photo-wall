"""The Display turn: how the Frames on one screen hang (render redesign, run ledger
.claude/runs/render-planes/s1-page.md). Stdlib only, so Display Host (which sets Weston's output
transform) and the Player (which draws its pictures already turned) share one vocabulary and one
mapping to Wayland's `wl_output.transform`.

The turn belongs to the spot on the wall, not to the screen (owner, gate answer Q3, 2026-10-17;
"the angle of rotation is a quality of the way frames are hung on the wall", 2026-10-11): quarter
turns only, and every Frame on one screen shares its screen's turn.

Rules this module owns (one home each):
- **The turns** (`TURNS`, `is_turn`): 0, 90, 180 and 270 degrees clockwise, as the screen hangs; no
  mirror and no other angle. `is_turn` is the one check of a turn value: an int, never a bool, a
  float or a string; every reader of a turn (this module, the Output document, `ExactTurn`) refuses
  by it.
- **The Wayland transform** (`wayland_transform`, `turn_of`): a screen hung turned `turn` degrees
  clockwise shows upright pictures when Weston turns its content `turn` degrees counter-clockwise,
  which is `wl_output.transform` 90 (1) for 90, 180 (2) for 180, 270 (3) for 270 and normal (0)
  for 0. The flipped transforms (4 to 7) are no turn.
- **The screen as it hangs** (`hanging`): a 90 or 270 turn swaps the mode's width and height.
"""
from __future__ import annotations

from typing import Final, Literal, TypeGuard

DisplayTurn = Literal[0, 90, 180, 270]                  # degrees clockwise, as the screen hangs
TURNS: Final[tuple[DisplayTurn, ...]] = (0, 90, 180, 270)


def is_turn(value: object) -> TypeGuard[DisplayTurn]:
    """Whether `value` is one of TURNS: an int (never a bool) of 0, 90, 180 or 270. The one check of
    a turn: `wayland_transform` and `hanging` refuse by it, and so does the Output document
    (contracts/node_output.py `OutputDocument.turn`). Pure."""
    return type(value) is int and value in TURNS


def wayland_transform(turn: DisplayTurn) -> int:
    """The `wl_output.transform` value (0 to 3) for a screen hung turned `turn` (module rule).
    ValueError("display_turn") unless `turn` is one of TURNS (an int, never a bool). Pure."""
    if not is_turn(turn):
        raise ValueError("display_turn")
    return TURNS.index(turn)


def turn_of(transform: int) -> DisplayTurn:
    """The turn whose `wayland_transform` is `transform`. ValueError("display_turn") for a flipped
    transform (4 to 7), any other value, or a bool. Pure."""
    if type(transform) is not int or not 0 <= transform < len(TURNS):
        raise ValueError("display_turn")
    return TURNS[transform]


def hanging(size: tuple[int, int], turn: DisplayTurn) -> tuple[int, int]:
    """The screen's (width, height) in pixels as it hangs, from its mode's (width, height): swapped
    for 90 and 270, as given for 0 and 180. ValueError("display_turn") as `wayland_transform`;
    ValueError("screen_size") unless both sides are ints (never bools) of at least 1. Pure."""
    quarter = wayland_transform(turn) % 2
    width, height = size
    if not (type(width) is int and type(height) is int and width >= 1 and height >= 1):
        raise ValueError("screen_size")
    return (height, width) if quarter else (width, height)


__all__ = ["TURNS", "DisplayTurn", "hanging", "is_turn", "turn_of", "wayland_transform"]
