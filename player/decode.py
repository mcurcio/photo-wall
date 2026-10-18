"""Decoding a layer's media for the Player's new path (render redesign, run ledger
.claude/runs/render-planes/s1-page.md): a still to RGBA once, a video to the hardware decoder's
dmabuf frames as it plays. GStreamer runs on its own threads; every method here belongs to the
Player's owning thread except the `on_frame` callback. Importing this module needs no GStreamer:
it is loaded through gi in the constructors.

Rules this module owns (one home each):
- **The video pipeline** (`VideoFrames`, bench T4): filesrc ! qtdemux ! h265parse ! v4l2slh265dec !
  video/x-raw(memory:DMABuf),format=DMA_DRM ! appsink. The decoder hands out dmabufs only when
  the sink's allocation answer declares GstVideoMeta; PyGObject cannot write a query, so a ctypes
  pad probe on the appsink's sink pad adds it (the bench's C probe, test4/src/hevcbench.c). At
  most DECODE_QUEUE frames are held ahead; the video loops; HEVC only (owner, gate answer Q5,
  2026-10-18: HEVC now, one 1080p copy per video; software H.264 is deleted in S4).
- **The still pipeline** (`StillDecode`): filesrc ! decodebin ! videoconvert ! RGBA appsink,
  prerolled once; JPEG and PNG.

Rules it does NOT own: which position a video plays at (contracts/models.py `Layer.position`); GPU
imports (player/gpu.py); when a layer is decoded (player/plane_renderer.py).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from player.gpu import DmabufFrame

DECODE_QUEUE: Final = 4         # decoded frames held ahead of the screen (bench T4)
VIDEO_DECODER: Final = "v4l2slh265dec"                          # the Pi 5's stateless HEVC decoder
DMABUF_CAPS: Final = "video/x-raw(memory:DMABuf),format=DMA_DRM"  # its output as dmabufs (bench T4)
STILL_TIMEOUT: Final = 10.0     # seconds a still may take to decode before it fails


class VideoRefused(RuntimeError):
    """The video pipeline could not start or stopped with an error: args[0] is "decode"."""


@dataclass(frozen=True)
class StillPicture:
    """A decoded still: `size` (width, height), `stride` bytes a row, straight (not pre-multiplied)
    RGBA `pixels`, and whether it may carry transparency (`alpha`: a PNG)."""
    size: tuple[int, int]
    stride: int
    pixels: bytes
    alpha: bool


class StillDecode:
    """One still's decode (module rule), started at construction and polled."""

    def __init__(self, path: Path) -> None:
        """Start decoding `path` (absolute; never a URL). ValueError("decode_path") otherwise."""
        raise NotImplementedError("render-planes S1 bead B5")

    def result(self) -> StillPicture | Literal["pending", "failed"]:
        """The picture once decoded; "pending" while decoding; "failed" on an error or after
        STILL_TIMEOUT seconds."""
        raise NotImplementedError("render-planes S1 bead B5")

    def close(self) -> None:
        """Stop the pipeline and drop the picture."""
        raise NotImplementedError("render-planes S1 bead B5")


class VideoFrames:
    """One video's hardware decode (module rule), playing at GStreamer's clock from `seek`'s
    position."""

    def __init__(self, path: Path, on_frame: Callable[[], None]) -> None:
        """Build and start the pipeline for `path` (absolute). `on_frame` is called on a GStreamer
        streaming thread when a new frame is ready: it must only schedule work on the owning
        thread. VideoRefused("decode") when the pipeline cannot reach PAUSED."""
        raise NotImplementedError("render-planes S1 bead B5")

    def seek(self, position: float) -> bool:
        """Play from `position` seconds (the layer's, modulo the duration); False when refused."""
        raise NotImplementedError("render-planes S1 bead B5")

    def latest(self) -> DmabufFrame | None:
        """The newest decoded frame not yet taken (None: none since the last), held until `done`;
        older undelivered frames are dropped. VideoRefused("decode") after a pipeline error."""
        raise NotImplementedError("render-planes S1 bead B5")

    def done(self, frame: DmabufFrame) -> None:
        """Give `frame`'s buffer back to the decoder (its fit pass has been flushed)."""
        raise NotImplementedError("render-planes S1 bead B5")

    def buffers(self) -> tuple[int, ...]:
        """The dmabuf fds the decoder has handed out so far (player/gpu.py `forget_frames` on close)."""
        raise NotImplementedError("render-planes S1 bead B5")

    def close(self) -> None:
        """Stop the pipeline; every held frame is released."""
        raise NotImplementedError("render-planes S1 bead B5")


__all__ = ["DECODE_QUEUE", "DMABUF_CAPS", "VIDEO_DECODER", "STILL_TIMEOUT", "StillDecode", "StillPicture", "VideoFrames", "VideoRefused"]
