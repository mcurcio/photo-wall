"""The Player's GPU device without a window system (render redesign, run ledger
.claude/runs/render-planes/s1-page.md): EGL on GBM over the V3D render node, one GLES 3 context
current on the owning thread, render targets the display controller can scan out, and imports of
the hardware video decoder's frames. Importing this module needs no native library: libgbm and
EGL are loaded through ctypes and PyOpenGL in `Gpu.__init__`.

Rules this module owns (one home each):
- **The targets** (`Gpu.target`): LINEAR XR24 for an opaque picture, LINEAR AR24 (pre-multiplied
  alpha) for one that carries transparency (a PNG); never a tiled layout (bench T5: LINEAR costs the
  same to render and is the layout the controller scans out unconverted) and never blend mode None.
  A target's memory is ordinary GBM memory, not CMA (bench T5).
- **The decoder frame import** (`Gpu.import_frame`): NV12 SAND128 dmabufs as external textures,
  the column height in the modifier's parameter (`sand_modifier`; vc4_plane.c), BT.709 limited
  range; one EGLImage per decoder buffer, reused while that buffer is (bench T4).
- **Ordering**: a draw is flushed (`flush`) before its target goes to the screen; the dmabuf's
  implicit fences order the controller's read after the GPU's write.

Rules it does NOT own: what is drawn (player/fit_pass.py); decoding (player/decode.py); the screen
(player/plane_screen.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

RENDER_NODE: Final = "/dev/dri/renderD128"       # V3D's render node on the Pi 5
XR24: Final = 0x34325258                         # DRM_FORMAT_XRGB8888
AR24: Final = 0x34325241                         # DRM_FORMAT_ARGB8888 (pre-multiplied by the fit pass)
NV12: Final = 0x3231564E                         # DRM_FORMAT_NV12
LINEAR: Final = 0                                # DRM_FORMAT_MOD_LINEAR


class GpuUnavailable(RuntimeError):
    """The device could not be opened: args[0] is "render_node", "gbm", "egl" or "context"."""


@dataclass(frozen=True)
class Target:
    """One picture's render target: `size` (width, height) as the buffer is laid out (turned for the
    screen, player/fit_pass.py `target_size`), the GL framebuffer and colour texture the fit pass
    draws into, and the dmabuf the screen imports (`fd`, owned by the Gpu until `free`, `stride` in
    bytes, `fourcc` XR24 or AR24, `modifier` LINEAR)."""
    size: tuple[int, int]
    framebuffer: int
    texture: int
    fd: int
    stride: int
    fourcc: int
    modifier: int


@dataclass(frozen=True)
class DmabufFrame:
    """One decoded video frame as the decoder hands it out (bench T4): its dmabuf `fd` (owned by the
    decode, valid until player/decode.py `VideoFrames.done`), `size` in pixels, `fourcc` NV12,
    the decoder's `modifier` (BROADCOM SAND128), the two planes' `offsets` and `strides` in bytes,
    the decoder's `serial` (rises with every frame) and its media `position` in seconds."""
    fd: int
    size: tuple[int, int]
    fourcc: int
    modifier: int
    offsets: tuple[int, int]
    strides: tuple[int, int]
    serial: int
    position: float


class Targets(Protocol):
    """Where the Player's pictures are drawn: the Gpu in production; plain GL textures in the fit
    pass harness (tests/native_fit_pass_harness.py), which has no render node."""

    def target(self, size: tuple[int, int], *, opaque: bool) -> Target: ...

    def free(self, target: Target) -> None: ...


def sand_modifier(modifier: int, luma_stride: int) -> int:
    """The modifier EGL is given for a SAND128 decoder frame: `modifier` with its parameter set to
    the column height, `luma_stride` (bench T4, test4/src/hevcbench.c); any other modifier as
    given. ValueError("sand_modifier") unless both are ints >= 0. Pure."""
    raise NotImplementedError("render-planes S1 bead B5")


class Gpu:
    """The GPU device (module rules). Every method on the thread that made it."""

    def __init__(self, render_node: str = RENDER_NODE) -> None:
        """Open `render_node`, make a GBM device, an EGL display on it (EGL_PLATFORM_GBM_KHR) and a
        surfaceless GLES 3 context, current on this thread. GpuUnavailable on any failure."""
        raise NotImplementedError("render-planes S1 bead B5")

    @property
    def gl(self):
        """The GLES 3 entry points (PyOpenGL's `OpenGL.GLES3`) for the current context."""
        raise NotImplementedError("render-planes S1 bead B5")

    def target(self, size: tuple[int, int], *, opaque: bool) -> Target:
        """A new LINEAR target of `size` (XR24 when `opaque`, else AR24): a GBM buffer, its
        EGLImage as a GL texture behind a complete framebuffer, and its dmabuf. RuntimeError
        ("gpu_target") when any step fails; nothing is kept then."""
        raise NotImplementedError("render-planes S1 bead B5")

    def free(self, target: Target) -> None:
        """Delete `target`'s framebuffer, texture, EGLImage and buffer and close its fd. A target
        already freed is ignored."""
        raise NotImplementedError("render-planes S1 bead B5")

    def upload(self, size: tuple[int, int], stride: int, pixels: bytes) -> int:
        """A new GL_TEXTURE_2D RGBA8 texture of a decoded still (player/decode.py `StillPicture`),
        `stride` bytes a row. RuntimeError("gpu_upload") on failure."""
        raise NotImplementedError("render-planes S1 bead B5")

    def delete_texture(self, texture: int) -> None:
        """Delete a texture `upload` made."""
        raise NotImplementedError("render-planes S1 bead B5")

    def import_frame(self, frame: DmabufFrame) -> int:
        """A GL_TEXTURE_EXTERNAL_OES texture showing `frame` (module rule: one EGLImage per decoder
        buffer, found again by the buffer's identity). RuntimeError("gpu_import") when EGL refuses
        it."""
        raise NotImplementedError("render-planes S1 bead B5")

    def forget_frames(self, fds: tuple[int, ...]) -> None:
        """Drop the EGLImages and textures of decoder buffers no longer in use (a closed decode)."""
        raise NotImplementedError("render-planes S1 bead B5")

    def flush(self) -> None:
        """glFlush: the draws so far are submitted, so a target can go to the screen (module rule)."""
        raise NotImplementedError("render-planes S1 bead B5")

    def close(self) -> None:
        """Free every target and import, then the context, display, GBM device and node."""
        raise NotImplementedError("render-planes S1 bead B5")


__all__ = ["AR24", "LINEAR", "NV12", "RENDER_NODE", "XR24", "DmabufFrame", "Gpu", "GpuUnavailable", "Target",
           "Targets", "sand_modifier"]
