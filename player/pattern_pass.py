"""The pattern pass: what a screen shows in place of a Frame's pictures while its Display is tuned or
its Position is tried with nothing playing (render redesign S3, run ledger .claude/runs/render-planes/
s3-page.md, bead B2): the Output's test pattern (decision 0024 as 0028 amends it), a calibration
target (decisions 0026, 0027), or Position's commissioning grid, drawn once into the screen's ground,
a picture of the whole screen as it hangs and turned for it, black outside the Frame.

A pattern is never a plane of its own: it is the screen's ground, shown with no planes above it
(player/plane_renderer.py), so the display controller still scans out one opaque picture and the
look's mask stays last ("outside every Frame's opening stays black", contracts/tuning.py). GL on the
current context only, like player/fit_pass.py: no EGL, GBM or window system here, so the harness
draws it on software GL.

Rules this module owns (one home each):
- **Which pattern** (`PatternRequest`): the Output's `operations.test_pattern` when it has one, else
  the commissioning grid (player/geometry.py `final_pixel` reads the same field, so its precedence
  has one home there).
- **What a draw computes**: per target pixel (x, y) as laid out, player/geometry.py's scalar
  reference: `buffer_to_picture` over the whole screen's target gives the screen point, `placement_uv`
  the Frame's picture point (u, v) or None outside the Frame; then `final_pixel` with `inside` = the
  point is in the Frame, `opening_uv` = (u, v), the photo `commissioning_grid(u, v)` for the grid
  ((0, 0, 0) for a test pattern, which replaces it), the Frame's `lights`, its `lut`, `board` and
  `dither_at` = (x, y); written as stored 8-bit values, opaque. Within one level of 256.
- **The shader's pattern codes** (`PATTERN_CODES`): each TestPattern's code in the GLSL, 0 for the
  commissioning grid.

Rules it does NOT own: the patterns, the board and the grid (player/geometry.py `pattern_signal`,
`board_signal`, `board_point`, `commissioning_grid`), the look (player/look_shader.py), the board's
raster and QR layout (contracts/calibration_target.py), the QR's modules (player/calibration_target.py
`target_modules`), when a pattern is drawn again (player/plane_renderer.py).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from contracts.display_turn import DisplayTurn
from contracts.frame_fit import Placement
from contracts.node_frame import PictureOperations
from contracts.tuning import TestPattern
from player.fit_pass import DrawTarget
from player.geometry import BoardDraw, FrameLights

PATTERN_CODES: Final[Mapping[TestPattern | None, int]] = {
    None: 0, TestPattern.WHITE: 1, TestPattern.GREY_RAMP: 2, TestPattern.BLACK_LEVEL: 3,
    TestPattern.TARGET_PRE_SOFTWARE: 4, TestPattern.TARGET_POST_SOFTWARE: 5}
"""Each pattern's code in the pass's GLSL (`#define PATTERN_<NAME> <code>`); None, 0: the commissioning
grid. The one home of the codes (the frame pass's copy in player/native.py goes with that module)."""


@dataclass(frozen=True)
class PatternRequest:
    """One screen ground with a Frame's pattern: the screen's `size` as it hangs (the target is
    player/fit_pass.py `target_size(Placement(0, 0, *size), turn)`), the Frame's `placement` on it
    (contracts/frame_fit.py `place`; black outside it), the screen's `turn`, the Output's picture
    `operations` (their `test_pattern` picks the pattern: module rule), the Frame's `lights`
    (player/geometry.py `picture_lights`; the commissioning grid's look), its paper profile's `lut`
    body (None: no LUT step) and, for a calibration target, its `board` (player/geometry.py
    `picture_board`; None otherwise)."""
    size: tuple[int, int]
    placement: Placement
    turn: DisplayTurn
    operations: PictureOperations
    lights: FrameLights
    lut: bytes | None
    board: BoardDraw | None


class PatternPass:
    """The pattern pass's programs on the current GL context (module rules)."""

    def __init__(self, gl: Any) -> None:
        """Compile and link the programs (one per step set: the LUT step a compile-time constant, as
        player/fit_pass.py) and upload the screen board's raster (contracts/calibration_target.py
        `board_raster(Surface.SCREEN)`, R8, once) with `gl`, PyOpenGL's GLES 3 module or one like it.
        player/fit_pass.py `FitFailed("gpu_error")` when a program does not link."""
        raise NotImplementedError("render-planes S3 bead B2")

    def draw(self, request: PatternRequest, target: DrawTarget) -> None:
        """Draw `request`'s ground into `target`, whose size must be the whole screen's target
        (ValueError("pattern_target_size") otherwise); a calibration target without `board` is
        ValueError("board"). The QR's modules are uploaded when `request.board.modules` differs from
        the last drawn. Submits the draw; the caller fences it (player/gpu.py `Gpu.fence`).
        FitFailed("gpu_error") when GL reports an error."""
        raise NotImplementedError("render-planes S3 bead B2")

    def close(self) -> None:
        """Delete the programs and the board's and QR's textures."""
        raise NotImplementedError("render-planes S3 bead B2")


__all__ = ["PATTERN_CODES", "PatternPass", "PatternRequest"]
