"""The Player's new path: each layer's picture finished once at its Frame's size, shown as a plane the
display controller places and fades (render redesign, run ledger .claude/runs/render-planes/
s1-page.md). It stands beside player/native.py until S3 makes it the Player's renderer and deletes
the old one; until S2 puts the rectangle in the Frame's Position, a Frame's rectangle is given
beside its binding (`FrameOnScreen`).

The composition root of the new path: it composes and calls, it never computes a rule. Each rule
has its home: what can be seen (player/visibility.py), a layer's alpha at a frame time
(player/look.py), the planes and their strengths (player/planes.py), the controller's limits
(player/controller_budget.py), pixels (contracts/frame_fit.py `place`), the picture
(player/fit_pass.py), the screen (player/plane_screen.py).

Rules this module owns (one home each):
- **When a picture is finished again**: a still when anything its pixels depend on changes (its
  decode, the placement's size, the turn, the crop, the profile's aspect, the Output's picture
  operations, the Frame's lights, its LUT's generation); a playing video on each new decoded
  frame, into the first of its VIDEO_TARGETS targets the screen has released. Never a fade: a
  fade is a strength.
- **Readiness**: a layer is prepared once its picture is finished for the Frame as it is now; a
  covered layer keeps its picture while it may be revealed within the executor's lead.
- **Outcomes**: no grant: "pending", nothing committed; a candidate grant: the screen's black
  under the candidate tag; an Output gone or a picture refused: "failed" ("decode"); over the
  controller budget after its cut: "failed" ("capacity"); a seen layer's picture not finished yet:
  "pending", the screen keeps its last commit; else "presented" (that commit), its composition the
  layers drawn (a layer the budget cut is absent).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from contracts.frame_fit import FrameRectangle
from contracts.models import OutputBinding
from contracts.node_frame import PictureOperations
from contracts.room_light import RoomLightsDocument
from player.controller_budget import PI5, ControllerBudget
from player.decode import StillPicture
from player.fit_pass import DrawTarget, FitRequest
from player.gpu import DmabufFrame, Target
from player.luts import LutSource
from player.pacing import Word
from player.plane_screen import PresentRefused, ScreenPicture
from player.planes import Composition, Screen
from player.rendering import LocalLayer, OutputComposition, PrepareResult, PresentationResult
from player.wayland_frames import FrameGrant

VIDEO_TARGETS: Final = 3        # a playing video's targets: one shown, one in flight, one drawn


@dataclass(frozen=True)
class FrameOnScreen:
    """A Frame's binding and its rectangle on its screen (until S2 carries the rectangle in the
    binding's Position)."""
    binding: OutputBinding
    rectangle: FrameRectangle


class Screens(Protocol):
    """player/plane_screen.py `PlaneScreens`, as this module uses it."""

    def screen(self, output_id: str) -> Screen | None: ...

    def grant(self, composition: OutputComposition) -> FrameGrant | None: ...

    def picture(self, output_id: str) -> PictureOperations | None: ...

    def import_picture(self, target: Target) -> ScreenPicture | None: ...

    def busy(self, picture: ScreenPicture) -> bool: ...

    def forget(self, picture: ScreenPicture) -> None: ...

    def present(self, composition: Composition, pictures: Mapping[str, ScreenPicture], grant: FrameGrant,
                tag: str, picture_change: int | None) -> int | PresentRefused: ...

    def presentation(self, output_id: str, now: float) -> Word | None: ...


class Device(Protocol):
    """player/gpu.py `Gpu`, as this module uses it."""

    def target(self, size: tuple[int, int], *, opaque: bool) -> Target: ...

    def free(self, target: Target) -> None: ...

    def upload(self, size: tuple[int, int], stride: int, pixels: bytes) -> int: ...

    def delete_texture(self, texture: int) -> None: ...

    def import_frame(self, frame: DmabufFrame) -> int: ...

    def forget_frames(self, fds: tuple[int, ...]) -> None: ...

    def flush(self) -> None: ...


class Fitter(Protocol):
    """player/fit_pass.py `FitPass`, as this module uses it."""

    def draw(self, request: FitRequest, target: DrawTarget) -> None: ...

    def black(self, target: DrawTarget) -> None: ...


class Still(Protocol):
    """player/decode.py `StillDecode`, as this module uses it."""

    def result(self) -> StillPicture | Literal["pending", "failed"]: ...

    def close(self) -> None: ...


class Video(Protocol):
    """player/decode.py `VideoFrames`, as this module uses it."""

    def seek(self, position: float) -> bool: ...

    def latest(self) -> DmabufFrame | None: ...

    def done(self, frame: DmabufFrame) -> None: ...

    def buffers(self) -> tuple[int, ...]: ...

    def close(self) -> None: ...


StillFactory = Callable[[Path], Still]
VideoFactory = Callable[[Path, Callable[[], None]], Video]


class PlaneRenderer:
    """The new path's renderer (module rules). Every method on one owning thread (the Player's GLib
    main loop), the thread the Gpu's context is current on."""

    def __init__(self, screens: Screens, device: Device, fitter: Fitter, luts: LutSource, *,
                 stills: StillFactory, videos: VideoFactory, budget: ControllerBudget = PI5,
                 on_change: Callable[[str], None] = lambda output_id: None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """`on_change(output_id)` is called on the owning thread when an Output's pictures changed
        without a new composition (a video's new frame finished): the caller presents again."""
        raise NotImplementedError("render-planes S1 bead B6")

    def set_room_lights(self, document: RoomLightsDocument) -> None:
        """Every Frame's lights from now on; its pictures are finished again (module rule)."""
        raise NotImplementedError("render-planes S1 bead B6")

    def prepare(self, local: LocalLayer, frame: FrameOnScreen) -> PrepareResult:
        """Decode `local` and finish its picture for `frame` as its screen is now (module rule):
        "prepared" once finished, "pending" while decoding or drawing, "failed" ("decode") when its
        media cannot be decoded or drawn. A black layer is prepared once its black picture exists."""
        raise NotImplementedError("render-planes S1 bead B6")

    def present(self, composition: OutputComposition, frame: FrameOnScreen) -> PresentationResult:
        """Show `composition` (its layers looked at this frame time, player/look.py) on its Output as
        one commit, within the controller budget over every Output's newest composition (module
        rule, Outcomes)."""
        raise NotImplementedError("render-planes S1 bead B6")

    def release(self, assignment_id: str) -> None:
        """Drop the layer's decode, targets and compositor buffers once no commit shows them."""
        raise NotImplementedError("render-planes S1 bead B6")

    def close(self) -> None:
        """Release every layer and stop every decode."""
        raise NotImplementedError("render-planes S1 bead B6")


__all__ = ["VIDEO_TARGETS", "Device", "Fitter", "FrameOnScreen", "PlaneRenderer", "Screens", "Still",
           "StillFactory", "Video", "VideoFactory"]
