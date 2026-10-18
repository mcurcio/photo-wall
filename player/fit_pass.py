"""The fit pass: one picture finished once, at its Frame's pixels, for the display controller to show
unscaled (render redesign, run ledger .claude/runs/render-planes/s1-page.md). Photos once; a video
once per decoded frame (owner, gate answer Q1, 2026-10-17: video carries the same look as photos).

One GPU draw from the source to the target: Central's crop window, scaled to the Frame's pixels,
turned for the screen, decoded to linear light (a photo's sRGB, a video's BT.709), then the look
(player/look_shader.py: the Frame's paper under the room's light and its mat shadow, the paper
profile's LUT, the Display's software settings, encode, contrast, gamma, dither), written as stored
8-bit sRGB values; a picture with transparency (a PNG) pre-multiplied. GL on the current context
only: no EGL, GBM, window system or decoder here, so the harness draws it on software GL.

Rules this module owns (one home each):
- **The target's layout** (`target_size`): the picture's pixels as it hangs, turned for the screen,
  so 90 and 270 swap width and height; the screen shows the buffer with a matching buffer transform
  and never copies it (bench T5).
- **What a draw computes**: per target pixel, player/geometry.py's scalar reference
  (`buffer_to_picture`, `fit_source_uv`, then `final_pixel` with the Frame's `picture_lights` and
  the pixel's place in the picture as the opening's), within one level of 256.

Rules it does NOT own: the look's formulas (player/geometry.py, mirrored by player/look_shader.py);
where the target's memory comes from (player/gpu.py `Targets`); when a picture is drawn again
(player/plane_renderer.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from contracts.display_turn import DisplayTurn
from contracts.frame_fit import Placement
from contracts.models import PhotoCrop
from contracts.node_frame import PictureOperations
from player.geometry import FrameLights

SourceEncoding = Literal["srgb", "bt709"]


class FitFailed(RuntimeError):
    """A draw the GPU did not finish: args[0] is "gpu_error". The Frame keeps its last picture."""


@dataclass(frozen=True)
class FitSource:
    """What the fit pass samples: a GL texture (`external`: GL_TEXTURE_EXTERNAL_OES, a decoder frame
    from player/gpu.py `import_frame`; else GL_TEXTURE_2D RGBA8, a still from `upload`), its `size`
    in pixels, its `encoding`, and whether it may carry transparency (`alpha`: a PNG)."""
    texture: int
    external: bool
    size: tuple[int, int]
    encoding: SourceEncoding
    alpha: bool


@dataclass(frozen=True)
class FitRequest:
    """One picture to finish: its `source`; Central's `crop` (contracts/photo_crop.py; None only for
    a source with exactly the Frame's aspect); the Frame profile's `aspect`; the Frame's `placement`
    on its screen (the picture is placement.width x placement.height as it hangs); the screen's
    `turn`; the Output's picture `operations`; the Frame's `lights` (player/geometry.py
    `picture_lights`); the paper profile's `lut` body (None: no LUT step)."""
    source: FitSource
    crop: PhotoCrop | None
    aspect: float
    placement: Placement
    turn: DisplayTurn
    operations: PictureOperations
    lights: FrameLights
    lut: bytes | None


class DrawTarget(Protocol):
    """Where a draw lands: a complete GL framebuffer of `size` (width, height) as laid out
    (player/gpu.py `Target` in production; a plain texture's framebuffer in the harness)."""

    @property
    def framebuffer(self) -> int: ...

    @property
    def size(self) -> tuple[int, int]: ...


def target_size(placement: Placement, turn: DisplayTurn) -> tuple[int, int]:
    """The target's (width, height) for a picture at `placement` on a screen turned `turn` (module
    rule). ValueError("display_turn") for any other turn. Pure."""
    raise NotImplementedError("render-planes S1 bead B4")


class FitPass:
    """The fit pass's programs on the current GL context (module rules)."""

    def __init__(self, gl: Any) -> None:
        """Compile and link the programs (one per source kind and step set, as player/native.py's
        frame pass switches its steps) with `gl`, PyOpenGL's GLES 3 module or one like it.
        FitFailed("gpu_error") when a program does not link."""
        raise NotImplementedError("render-planes S1 bead B4")

    def draw(self, request: FitRequest, target: DrawTarget) -> None:
        """Finish `request`'s picture into `target`, whose size must be `target_size(request.
        placement, request.turn)` (ValueError("fit_target_size") otherwise). Submits the draw;
        the caller flushes before the target goes to the screen. FitFailed("gpu_error") when GL
        reports an error."""
        raise NotImplementedError("render-planes S1 bead B4")

    def black(self, target: DrawTarget) -> None:
        """Fill `target` with opaque black: a black layer's picture."""
        raise NotImplementedError("render-planes S1 bead B4")

    def close(self) -> None:
        """Delete the programs and the LUT texture."""
        raise NotImplementedError("render-planes S1 bead B4")


__all__ = ["DrawTarget", "FitFailed", "FitPass", "FitRequest", "FitSource", "SourceEncoding", "target_size"]
