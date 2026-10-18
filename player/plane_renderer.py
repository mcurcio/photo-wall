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
  frame while it is seen (in its screen's last commit), into the first of its VIDEO_TARGETS
  targets the screen has released. Never a fade: a fade is a strength.
- **A video starts when it is first presented**: prepared, it is ready to start, its first frame
  finished once (later frames wait in the decoder); its first present plays it from the layer's
  position, and from then on each new frame is finished while it is seen.
- **Readiness**: a layer is prepared once its picture is finished for the Frame as it is now; a
  covered layer keeps its picture while it may be revealed within the executor's lead.
- **Nothing changed**: a present commits only when the screen would show something else: a
  plane's picture (a still finished again, a video's new frame), its placement or strength, the
  screen, the tag, the picture change or the grant. A layer's play position alone changes nothing
  (a still's picture does not move with it; a video's frame is its decoder's).
- **Outcomes**: no grant: "pending", nothing committed; a candidate grant: the screen's black
  under the candidate tag, made again on every present the pacer allows (the shell completes a
  handoff only on a presented commit after its release, which the app cannot see); an Output gone
  or a picture refused: "failed" ("decode"); over the
  controller budget after its cut: "failed" ("capacity"); a seen layer's picture not finished yet:
  "pending", the screen keeps its last commit; else "presented" (that commit), its composition the
  layers drawn (a layer the budget cut is absent).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, Literal, Protocol

from contracts.frame_fit import FrameRectangle, Placement, place
from contracts.models import OutputBinding
from contracts.node_frame import PictureOperations, neutral_operations
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT
from contracts.room_light import RoomLightsDocument
from player.controller_budget import PI5, ControllerBudget, fits, within
from player.decode import StillPicture
from player.fit_pass import DrawTarget, FitRequest, FitSource, target_size
from player.geometry import FrameLights, picture_lights
from player.gpu import DmabufFrame, Target
from player.look import look
from player.luts import LutSource
from player.pacing import FramePacer, Word
from player.plane_screen import PresentRefused, ScreenPicture
from player.planes import Composition, FrameLayers, Plane, Screen, compose
from player.rendering import LocalLayer, OutputComposition, PrepareResult, PresentationResult
from player.visibility import visible_now
from player.wayland_frames import FrameGrant, composition_tag

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


_PENDING: Final = PresentationResult("pending")
_FAILED: Final = PresentationResult("failed", "decode")


@dataclass
class _Picture:
    """One target the fit pass drew into and the compositor's buffer of it."""
    target: Target
    shown: ScreenPicture


@dataclass(frozen=True)
class _Setting:
    """What a Frame's pictures are finished for now: its screen, its placement on it and the
    profile's aspect, the picture operations drawn and the change a tag names (None: the stand-in,
    never tagged), the Frame's lights, its LUT and the LUTs' generation."""
    screen: Screen
    placement: Placement
    aspect: float
    operations: PictureOperations
    tagged: int | None
    lights: FrameLights
    lut: bytes | None
    generation: int

    @property
    def size(self) -> tuple[int, int]:
        """A picture's target as it is laid out on the screen (player/fit_pass.py)."""
        return target_size(self.placement, self.screen.turn)


@dataclass
class _Layer:
    """One media layer's decode and its finished pictures. A still: its source uploaded once and
    one picture, finished again when its key changes. A video: up to VIDEO_TARGETS pictures, the
    newest decoded frame drawn into the first the screen has released; `held` is the frame drawn
    last (kept, so a new key draws it again), `waiting` a newer one not drawn yet, `started`
    whether a present has played it. `serial` counts the pictures finished (module rule, Nothing
    changed)."""
    local: LocalLayer
    frame: FrameOnScreen
    still: Still | None = None
    video: Video | None = None
    source: StillPicture | None = None
    texture: int = 0
    picture: _Picture | None = None
    key: tuple | None = None
    targets: list[_Picture] = field(default_factory=list)
    held: DmabufFrame | None = None
    waiting: DmabufFrame | None = None
    started: bool = False
    serial: int = 0
    failed: bool = False


@dataclass
class _Output:
    """One screen's commits: its pacer, the last answer, the pictures and the layers its last
    commit shows, and what that commit showed (module rule, Nothing changed)."""
    pacer: FramePacer = field(default_factory=FramePacer)
    last: PresentationResult = _PENDING
    shown: frozenset[int] = frozenset()
    seen: frozenset[str] = frozenset()
    committed: tuple | None = None


class PlaneRenderer:
    """The new path's renderer (module rules). Every method on one owning thread (the Player's GLib
    main loop), the thread the Gpu's context is current on. The VideoFactory hands each video's
    `on_frame` to that thread (player/decode.py calls it on a streaming thread)."""

    def __init__(self, screens: Screens, device: Device, fitter: Fitter, luts: LutSource, *,
                 stills: StillFactory, videos: VideoFactory, budget: ControllerBudget = PI5,
                 on_change: Callable[[str], None] = lambda output_id: None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """`on_change(output_id)` is called on the owning thread when an Output's pictures changed
        without a new composition (a video's new frame finished): the caller presents again."""
        self._screens, self._device, self._fitter, self._luts = screens, device, fitter, luts
        self._stills, self._videos, self._budget = stills, videos, budget
        self._on_change, self._clock = on_change, clock
        self._room_lights: RoomLightsDocument | None = None
        self._layers: dict[str, _Layer] = {}
        self._blacks: dict[tuple[str, tuple[int, int]], _Picture] = {}
        self._retired: list[_Picture] = []          # freed once no commit shows them
        self._outputs: dict[str, _Output] = {}
        self._newest: dict[str, Composition] = {}   # every Output's newest composition, for the budget

    def set_room_lights(self, document: RoomLightsDocument) -> None:
        """Every Frame's lights from now on; its pictures are finished again (module rule)."""
        self._room_lights = document

    def prepare(self, local: LocalLayer, frame: FrameOnScreen) -> PrepareResult:
        """Decode `local` and finish its picture for `frame` as its screen is now (module rule):
        "prepared" once finished, "pending" while decoding or drawing (or before the Output is
        there), "failed" ("decode") when its media cannot be decoded or drawn, or the rectangle
        does not lie on the screen. A black layer is prepared once its black picture exists."""
        setting = self._setting(frame)
        if setting == "gone":
            return PrepareResult("pending")
        if setting == "failed":
            return PrepareResult("failed", "decode")
        picture = self._finish(local, frame, setting)
        if picture == "failed":
            return PrepareResult("failed", "decode")
        return PrepareResult("pending" if picture == "pending" else "prepared")

    def present(self, composition: OutputComposition, frame: FrameOnScreen) -> PresentationResult:
        """Show `composition` (its layers looked at this frame time, player/look.py) on its Output as
        one commit, within the controller budget over every Output's newest composition (module
        rule, Outcomes). While the screen's last commit has no word from the compositor
        (player/pacing.py) nothing is committed and the answer is the last commit's; a present
        that shows what the last commit shows commits nothing either."""
        output_id = composition.binding.output_id
        now = self._clock()
        state = self._outputs.setdefault(output_id, _Output())
        word = self._screens.presentation(output_id, now)
        if word is not None:
            state.pacer.answered(word, None)
        self._sweep()
        grant = self._screens.grant(composition)
        if grant is None:
            return _PENDING
        setting = self._setting(frame)
        if setting == "gone" or setting == "failed":
            return _FAILED
        if not state.pacer.ready(now):
            return state.last
        if not grant.admitted or not grant.matches(composition):
            return self._candidate(state, setting.screen, grant, now)
        looked = look(composition, now)
        composed = compose(setting.screen, [FrameLayers(composition.binding.frame_id, setting.placement,
                                                        looked.layers)])
        self._newest[output_id] = composed
        kept, cut = within(self._budget, tuple(self._newest.values()))
        if not fits(self._budget, kept):
            return PresentationResult("failed", "capacity")
        planes = kept[list(self._newest).index(output_id)]
        pictures: dict[str, ScreenPicture] = {}
        for plane in planes.planes:
            picture = self._finish(plane.layer, frame, setting, live=True)
            if picture == "failed":
                return _FAILED
            if picture == "pending":
                return _PENDING                 # the screen keeps its last commit
            pictures[plane.layer.layer.assignment_id] = picture
        drawn = replace(looked, layers=tuple(local for local in looked.layers
                                             if local.layer.assignment_id not in cut))
        tag = composition_tag(drawn, visible_now(drawn.layers))
        shows = (planes.screen, tuple(self._shown(plane, pictures) for plane in planes.planes), tag, setting.tagged,
                 grant.grant_id)
        if state.committed == shows and state.last.status == "presented":
            return state.last
        sequence = self._screens.present(planes, pictures, grant, tag, setting.tagged)
        if not isinstance(sequence, int):
            return _FAILED
        state.pacer.submitted(sequence, now)
        state.shown = frozenset(picture.handle for picture in pictures.values())
        state.seen = frozenset(pictures)
        state.committed = shows
        state.last = PresentationResult("presented", composition=drawn, presented_at=now)
        self._sweep()
        return state.last

    def release(self, assignment_id: str) -> None:
        """Drop the layer's decode, targets and compositor buffers once no commit shows them."""
        entry = self._layers.pop(assignment_id, None)
        if entry is None:
            return
        if entry.still is not None:
            entry.still.close()
        if entry.video is not None:
            for frame in (entry.held, entry.waiting):
                if frame is not None:
                    entry.video.done(frame)
            self._device.forget_frames(entry.video.buffers())
            entry.video.close()
            self._retired += entry.targets
        elif entry.picture is not None:
            self._retired.append(entry.picture)
        if entry.texture:
            self._device.delete_texture(entry.texture)
        self._sweep()

    def close(self) -> None:
        """Release every layer and stop every decode."""
        for assignment_id in list(self._layers):
            self.release(assignment_id)
        for picture in [*self._retired, *self._blacks.values()]:
            self._screens.forget(picture.shown)
            self._device.free(picture.target)
        self._retired.clear()
        self._blacks.clear()
        self._outputs.clear()
        self._newest.clear()

    def _setting(self, frame: FrameOnScreen) -> _Setting | Literal["gone", "failed"]:
        """What `frame`'s pictures are finished for now; "gone" without its screen, "failed" when
        its rectangle does not lie on the screen."""
        binding = frame.binding
        screen = self._screens.screen(binding.output_id)
        if screen is None:
            return "gone"
        aspect = binding.profile.width_px / binding.profile.height_px
        try:
            placement = place(frame.rectangle, screen.size, aspect)
        except ValueError:
            return "failed"
        operations = self._screens.picture(binding.output_id)
        drawn = operations or neutral_operations(binding.output_id, 1, UPGRADE_DISPLAY_LIGHT)
        lights = picture_lights(binding.lighting, self._room_lights, binding.profile, drawn.light)
        ref = binding.lighting.paper.profile
        return _Setting(screen, placement, aspect, drawn,
                        None if operations is None else operations.picture_change, lights,
                        None if ref is None else self._luts.lut(ref), self._luts.generation())

    def _shown(self, plane: Plane, pictures: Mapping[str, ScreenPicture]) -> tuple:
        """What `plane` shows (module rule, Nothing changed): its Frame and layer, its picture and
        that picture's finish, its placement and strength; never the layer's play position."""
        assignment_id = plane.layer.layer.assignment_id
        entry = self._layers.get(assignment_id)
        return (plane.frame_id, assignment_id, pictures[assignment_id].handle, 0 if entry is None else entry.serial,
                plane.at, plane.strength)

    def _candidate(self, state: _Output, screen: Screen, grant: FrameGrant, now: float) -> PresentationResult:
        """The candidate's commit: no planes (the screen's black), tagged for its grant, on every
        present the pacer allows (module rule, Outcomes)."""
        sequence = self._screens.present(Composition(screen, ()), {}, grant, f"candidate-{grant.grant_id}", None)
        if not isinstance(sequence, int):
            return _FAILED
        state.pacer.submitted(sequence, now)
        state.shown, state.seen, state.committed = frozenset(), frozenset(), None
        self._sweep()
        state.last = _PENDING
        return _PENDING

    def _finish(self, local: LocalLayer, frame: FrameOnScreen, setting: _Setting, *,
                live: bool = False) -> ScreenPicture | Literal["pending", "failed"]:
        """`local`'s picture for `setting` (module rule), its decode started when it has none.
        `live`: for a present (a video starts, module rule), not a prepare."""
        if local.layer.presentation == "black":
            return self._black(frame.binding.output_id, setting.size)
        entry = self._layers.get(local.layer.assignment_id)
        if entry is None:
            entry = self._layers[local.layer.assignment_id] = self._decode(local, frame, live)
        entry.frame = frame
        if entry.failed:
            return "failed"
        try:
            if entry.video is not None:
                if live and not entry.started:
                    entry.started = True            # it plays from where the layer is now
                    entry.failed = not entry.video.seek(local.position)
                    if entry.failed:
                        return "failed"
                return self._finish_video(entry, setting)
            return self._finish_still(entry, setting)
        except (RuntimeError, ValueError):          # FitFailed, a Gpu refusal, VideoRefused
            entry.failed = True
            return "failed"

    def _decode(self, local: LocalLayer, frame: FrameOnScreen, live: bool) -> _Layer:
        entry = _Layer(local, frame, started=live)
        variant = local.layer.variant
        try:
            if local.path is None or variant is None:
                raise ValueError("decode_path")
            if variant.duration is None:
                entry.still = self._stills(local.path)
            else:
                assignment_id = local.layer.assignment_id
                entry.video = self._videos(local.path, lambda: self._video_frame(assignment_id))
                entry.failed = not entry.video.seek(local.position)
        except (RuntimeError, ValueError):
            entry.failed = True
        return entry

    @staticmethod
    def _key(entry: _Layer, setting: _Setting) -> tuple:
        """Everything a layer's pixels depend on besides its decode (module rule)."""
        return (setting.size, setting.screen.turn, entry.local.layer.crop, setting.aspect, setting.operations,
                setting.lights, setting.generation)

    @staticmethod
    def _request(entry: _Layer, source: FitSource, setting: _Setting) -> FitRequest:
        return FitRequest(source, entry.local.layer.crop, setting.aspect, setting.placement, setting.screen.turn,
                          setting.operations, setting.lights, setting.lut)

    def _finish_still(self, entry: _Layer, setting: _Setting) -> ScreenPicture | Literal["pending", "failed"]:
        if entry.source is None:
            result = entry.still.result()
            if result == "pending":
                return "pending"
            if result == "failed":
                entry.failed = True
                return "failed"
            entry.source = result
            entry.texture = self._device.upload(result.size, result.stride, result.pixels)
        key = self._key(entry, setting)
        if entry.picture is None or entry.key != key:
            source = entry.source
            request = self._request(entry, FitSource(entry.texture, False, source.size, "srgb", source.alpha),
                                    setting)
            picture = self._draw(request, setting.size, opaque=not source.alpha)
            if entry.picture is not None:
                self._retired.append(entry.picture)
            entry.picture, entry.key = picture, key
            entry.serial += 1
        return entry.picture.shown

    def _finish_video(self, entry: _Layer, setting: _Setting) -> ScreenPicture | Literal["pending", "failed"]:
        """Before it started, only its first frame is taken (module rule); later ones wait."""
        video = entry.video
        frame = video.latest() if entry.started or entry.picture is None else None
        if frame is not None:
            if entry.waiting is not None:
                video.done(entry.waiting)
            entry.waiting = frame
        key = self._key(entry, setting)
        if any(picture.target.size != setting.size for picture in entry.targets):
            self._retired += entry.targets       # laid out for another size or turn
            entry.targets, entry.picture = [], None
        source = entry.waiting or (entry.held if entry.key != key else None)
        if source is not None:
            target = self._released(entry, setting.size)
            if target is not None:
                texture = self._device.import_frame(source)
                request = self._request(entry, FitSource(texture, True, source.size, "bt709", False), setting)
                self._fitter.draw(request, target.target)
                self._device.flush()
                if entry.held is not None and entry.held is not source:
                    video.done(entry.held)
                entry.held, entry.waiting = source, None
                entry.picture, entry.key = target, key
                entry.serial += 1
        if entry.picture is None or entry.key != key:
            return "pending"
        return entry.picture.shown

    def _released(self, entry: _Layer, size: tuple[int, int]) -> _Picture | None:
        """The first of the video's targets the screen has released (a new one while it has fewer
        than VIDEO_TARGETS); None when every one is shown."""
        for picture in entry.targets:
            if not self._showing(picture):
                return picture
        if len(entry.targets) >= VIDEO_TARGETS:
            return None
        target = self._device.target(size, opaque=True)
        shown = self._screens.import_picture(target)
        if shown is None:
            self._device.free(target)
            raise RuntimeError("picture_refused")
        entry.targets.append(_Picture(target, shown))
        return entry.targets[-1]

    def _black(self, output_id: str, size: tuple[int, int]) -> ScreenPicture | Literal["failed"]:
        """The Output's one black picture of `size`, shared by every black layer on it."""
        picture = self._blacks.get((output_id, size))
        if picture is None:
            try:
                target = self._device.target(size, opaque=True)
            except RuntimeError:
                return "failed"
            try:
                self._fitter.black(target)
                self._device.flush()
            except RuntimeError:
                self._device.free(target)
                return "failed"
            shown = self._screens.import_picture(target)
            if shown is None:
                self._device.free(target)
                return "failed"
            picture = self._blacks[output_id, size] = _Picture(target, shown)
        return picture.shown

    def _draw(self, request: FitRequest, size: tuple[int, int], *, opaque: bool) -> _Picture:
        """A new target of `size` with `request` drawn into it, imported by the compositor.
        RuntimeError when the Gpu, the fit pass or the compositor refuses."""
        target = self._device.target(size, opaque=opaque)
        try:
            self._fitter.draw(request, target)
            self._device.flush()
        except RuntimeError:
            self._device.free(target)
            raise
        shown = self._screens.import_picture(target)
        if shown is None:
            self._device.free(target)
            raise RuntimeError("picture_refused")
        return _Picture(target, shown)

    def _video_frame(self, assignment_id: str) -> None:
        """A video's new frame (on the owning thread): finish it while the video is seen, or its
        first (module rule), and say so once it is drawn."""
        entry = self._layers.get(assignment_id)
        if entry is None or entry.failed:
            return
        seen = entry.started and any(assignment_id in state.seen for state in self._outputs.values())
        if entry.picture is not None and not seen:
            return
        setting = self._setting(entry.frame)
        if setting == "gone" or setting == "failed":
            return
        before = entry.held
        self._finish(entry.local, entry.frame, setting, live=entry.started)
        if entry.held is not before:
            self._on_change(entry.frame.binding.output_id)

    def _showing(self, picture: _Picture) -> bool:
        """Whether a commit shows `picture`: its screen's last commit, or one the compositor still
        holds it for."""
        return (any(picture.shown.handle in state.shown for state in self._outputs.values())
                or self._screens.busy(picture.shown))

    def _sweep(self) -> None:
        """Free every retired picture no commit shows any more."""
        kept = []
        for picture in self._retired:
            if self._showing(picture):
                kept.append(picture)
            else:
                self._screens.forget(picture.shown)
                self._device.free(picture.target)
        self._retired = kept


__all__ = ["VIDEO_TARGETS", "Device", "Fitter", "FrameOnScreen", "PlaneRenderer", "Screens", "Still",
           "StillFactory", "Video", "VideoFactory"]
