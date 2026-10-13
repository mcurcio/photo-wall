"""Display tuning's vocabulary shared by both picture wires (decision 0024, display tuning). Stdlib
only: the Pi runs it.

A test pattern rides two wires: Central's picture document asks for it (contracts/node_picture.py
`TestPatternAsk`) and the Pi's picture operations tell the Player to draw it
(contracts/node_frame.py `PictureOperations.test_pattern`). Its vocabulary lives here, apart from
both, as a LUT's name lives in contracts/lut.py: contracts/node_picture.py imports
contracts/kelvin.py, which imports contracts/node_frame.py.

Rules this module owns (one home each): which test patterns exist and their wire values.

Rules it does NOT own: when one is up and its bound (contracts/node_picture.py, Central's
central/displays/picture.py, the Pi's appliance/display_host/picture.py), how the Player lays one
out and where it enters the picture steps (player/geometry.py `test_pattern_signal`, `final_pixel`).
"""
from __future__ import annotations

from enum import StrEnum


class TestPattern(StrEnum):
    """What a Frame shows instead of photos while its Display is tuned. WHITE and GREY_RAMP are
    look patterns: they pass through every Photo Wall step and the Frame's mat, so they show what
    photos get. BLACK_LEVEL is the raw signal: black (0) with steps 1..20 beside it, through no
    Photo Wall step and no mat, so no setting can hide or fake a signal range fault. Outside every
    Frame's opening stays black for all three."""
    __test__ = False        # a domain name, not a pytest class

    WHITE = "white"                 # the paper-sheet test (owner, 2026-10-13)
    GREY_RAMP = "grey-ramp"         # roadmap B5: steps of the signal from 0 to 100 % and a smooth ramp
    BLACK_LEVEL = "black-level"     # PLUGE-like (ITU-R BT.814): raw 0 with raw steps 1..20


__all__ = ["TestPattern"]
