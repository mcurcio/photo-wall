"""Display tuning's vocabulary shared by both picture wires (decision 0024, display tuning; the two
calibration targets, decision 0026). Stdlib only: the Pi runs it.

A test pattern rides two wires: Central's picture document asks for it (contracts/node_picture.py
`TestPatternAsk`) and the Pi's picture operations tell the Player to draw it
(contracts/node_frame.py `PictureOperations.test_pattern`). Its vocabulary lives here, apart from
both, as a LUT's name lives in contracts/lut.py: contracts/node_picture.py imports
contracts/kelvin.py, which imports contracts/node_frame.py.

Rules this module owns (one home each): which test patterns exist and their wire values; the
signal's top code a raw step is counted in (SIGNAL_LEVELS).

Rules it does NOT own: when one is up and its bound (contracts/node_picture.py, Central's
central/displays/picture.py, the Pi's appliance/display_host/picture.py), how the Player lays one
out and where it enters the picture steps (player/geometry.py `pattern_signal`, `board_signal`,
`final_pixel`), the calibration targets' layout (contracts/calibration_target.py).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Final


class TestPattern(StrEnum):
    """What a Frame shows instead of photos while its Display is tuned. WHITE and GREY_RAMP are
    look patterns: they pass through every Photo Wall step and the Frame's mat, so they show what
    photos get. BLACK_LEVEL is the raw signal: black (0) with steps 1..20 beside it, through no
    Photo Wall step and no mat, so no setting can hide or fake a signal range fault.

    The two calibration targets (decisions 0026, 0027) draw the screen board (contracts/
    calibration_target.py, an OpenCV ChArUco board) in each Frame's opening, SCREEN_ROWS squares
    across its short side, its QR copies naming the port's picture change. TARGET_PRE_SOFTWARE is
    raw everywhere, as BLACK_LEVEL (the display's own controls are judged); TARGET_POST_SOFTWARE
    passes every Photo Wall step but the mat shadow, everywhere (the residual Photo Wall's
    settings should cover). Either, in any order: the operator's choice (owner, 2026-10-13).
    Outside every Frame's opening stays black for all five."""
    __test__ = False        # a domain name, not a pytest class

    WHITE = "white"                 # the paper-sheet test (owner, 2026-10-13)
    GREY_RAMP = "grey-ramp"         # roadmap B5: steps of the signal from 0 to 100 % and a smooth ramp
    BLACK_LEVEL = "black-level"     # PLUGE-like (ITU-R BT.814): raw 0 with raw steps 1..20
    TARGET_PRE_SOFTWARE = "target-pre-software"     # the screen board, raw: the display alone
    TARGET_POST_SOFTWARE = "target-post-software"   # the screen board through Photo Wall's steps


SIGNAL_LEVELS: Final = 255     # an 8-bit output's top code (the test Pi: output_bpc 8): raw step k is k / SIGNAL_LEVELS

# The calibration targets (decisions 0026, 0027): the patterns that draw contracts/calibration_target.py's screen board.
TARGET_PATTERNS: Final[frozenset[TestPattern]] = frozenset(
    {TestPattern.TARGET_PRE_SOFTWARE, TestPattern.TARGET_POST_SOFTWARE})


__all__ = ["SIGNAL_LEVELS", "TARGET_PATTERNS", "TestPattern"]
