"""The look in GLSL: the one shader copy of player/geometry.py's final steps, shared by the frame pass
(player/native.py, until S3 deletes it) and the fit pass (player/fit_pass.py) (render redesign,
run ledger .claude/runs/render-planes/s1-page.md). Moved from player/native.py, never rewritten:
the Frame's paper under the room's light and the mat shadow (`irradiance`, `paper_reflectance`,
`paper_signal`), the paper profile's LUT, the fixed highlight rolloff, white balance, sRGB encode,
contrast, gamma, clamp and dither, each step as player/geometry.py states it.

Rules this module owns (one home each): the look's GLSL functions (`LOOK_GLSL`) and their uniforms
(`look_uniforms`), and the LUT's texture unit. Rules it does NOT own: the formulas
(player/geometry.py); where a pixel sits in its opening (each pass's own).
"""
from __future__ import annotations

from typing import Any, Final

from contracts.node_frame import PictureOperations
from player.geometry import FrameLights

LUT_UNIT: Final = 1     # the LUT's texture unit; unit 0 is the picture being sampled

LOOK_GLSL: Final[str] = ""
"""GLSL ES 3.00 functions, no `main` and no `#version`: `vec3 look(vec3 linear, vec2 opening_uv,
ivec2 dither_at)` and the uniforms `look_uniforms` sets; filled by moving player/native.py's
`_FINAL` steps here (bead B4)."""


def look_uniforms(gl: Any, program: int, operations: PictureOperations, lights: FrameLights | None, *,
                  dither_height: int, lut_bound: bool) -> None:
    """Set `LOOK_GLSL`'s uniforms in `program` (current) for `operations` and the Frame's `lights`
    (None: no light, every pixel black as player/geometry.py `final_pixel` has it), the dither
    counting rows from the top of a `dither_height`-row image, the LUT step on when `lut_bound` (its
    texture bound on LUT_UNIT)."""
    raise NotImplementedError("render-planes S1 bead B4")


__all__ = ["LOOK_GLSL", "LUT_UNIT", "look_uniforms"]
