"""The Player's renderer (player/rendering.py `Renderer`): each layer's picture finished once at its
Frame's size, shown as a plane the display controller places and fades (render redesign, run ledgers
.claude/runs/render-planes/s1-page.md and s3-page.md). It stands beside player/native.py until S3's
last bead deletes the old one.

The composition root of the new path: it composes and calls, it never computes a rule. Each rule
has its home: what can be seen (player/visibility.py), a layer's alpha at a frame time, when a
fade under way ends and the moments a composition's seen layers change (player/look.py), the planes
and their strengths (player/planes.py), the controller's limits (player/controller_budget.py),
pixels (contracts/frame_fit.py `place`), the picture (player/fit_pass.py), a pattern
(player/pattern_pass.py), the screen, a trial and its overlay's corners (player/plane_screen.py),
a calibration target's QR (player/calibration_target.py `target_modules`) and board
(player/geometry.py `picture_board`), one commit in flight (player/pacing.py `FramePacer.free`).

Rules this module owns (one home each):
- **The drawn Position**: the Output's Position trial (`Screens.trial` under the composition's
  grant; a prepare asks under the grant of its binding alone) while one is drawn, else the binding's
  calibration; its fit must be a RectangleFit (an older form fails, "decode", until S5 converts
  it), placed by `place` at the profile's aspect. The drawn lighting is the trial's while one is
  drawn, else the binding's. Every presented answer names the drawn Position (`applied_calibration`,
  with the trial's identity while one is drawn), and a commit under a trial carries the trial
  overlay's corners of the drawn placement.
- **When a pattern shows** (s3-page decision 2): while the Output's picture operations carry a test
  pattern, or a trial is drawn over a fallback with no layers (Position's commissioning grid), the
  commit has no planes and its ground is the pattern's: one target per Output, drawn again exactly
  when its request changes (the LUTs' generation counted, as a still's key), fenced, and "pending"
  (the screen keeps its last commit) until finished. A calibration target's QR names the Player
  once `set_player` gave an id. The composition reported and tagged stays the looked one: a
  pattern is a tuning overlay, not a change of what plays.
- **When a picture is finished again**: a still when anything its pixels depend on changes (its
  decode, the placement's size, the turn, the crop, the profile's aspect, the Output's picture
  operations, the Frame's lights, its LUT's generation); a playing video on each new decoded
  frame while it is seen (in its screen's last commit), into the first of its VIDEO_TARGETS
  targets the screen has released. Never a fade: a fade is a strength.
- **Finishing never holds a due commit**: a picture goes to the screen only once the GPU has
  finished it (its fence signalled, player/gpu.py), never waited for; a video's newer frame
  meanwhile leaves its last finished one on screen. A still is finished in steps (its decode taken,
  a band of BAND_BYTES of its rows uploaded, its draw), at most one step every STEP_GAP over every
  Output, so no loop turn carries more than one; and a still not needed before a fade under way on
  any Output is over (it starts after that fade ends) is not prepared during it.
- **The screen's ground**: every commit shows, beneath its planes, the Output's black picture laid
  out for the whole screen (the one black picture of that size, shared with black layers), so
  the display controller can always scan out what the planes leave uncovered and the compositor
  never needs its renderer for the screen's black.
- **A video starts when it is first presented**: prepared, it is ready to start, its first frame
  finished once (later frames wait in the decoder); its first present plays it from the layer's
  position, and from then on each new frame is finished while it is seen.
- **Readiness**: a layer is prepared once its picture is finished for the Frame as it is now; a
  covered layer keeps its picture while it may be revealed within the executor's lead.
- **Nothing changed**: a present commits only when the screen would show something else: a
  plane's picture (a still finished again, a video's new frame), its placement or strength, the
  ground, the screen, the tag, the picture change, the grant, the trial's overlay or the drawn
  Position. A layer's play position alone
  changes nothing (a still's picture does not move with it; a video's frame is its decoder's).
- **Commits draw nothing**: every picture is finished before its commit, so a commit is made as
  soon as the last is answered (the pacer's one-in-flight rule, never its cadence slot).
- **The budget at a composition's busiest** (`capacity`, s3-page decision 3): each composition is
  composed at every player/look.py `moments` from now and judged with the other Outputs' by
  `fits`; who gives up a layer is the executor's (player/visibility.py `shed`), never this module's.
- **Outcomes**: no grant: "pending", nothing committed; a candidate grant: the screen's ground
  under the candidate tag, made again on every present the pacer allows (the shell completes a
  handoff only on a presented commit after its release, which the app cannot see); an Output gone,
  a Position that is not a rectangle on the screen, a trial refused or a picture refused: "failed"
  ("decode"); over the controller budget with every other Output's last commit: "failed"
  ("capacity"), nothing committed; a seen layer's picture (or the pattern) not finished yet:
  "pending", the screen keeps its last commit; else "presented" (that commit), its composition the
  looked one.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from itertools import product
from pathlib import Path
from typing import Final, Literal, Protocol

from contracts.calibration_target import QrModules, TargetCode
from contracts.frame_fit import Placement, place
from contracts.frame_lighting import FrameLighting
from contracts.models import AppliedCalibration, Calibration, OutputBinding, RectangleFit
from contracts.node_frame import PictureOperations, neutral_operations
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT
from contracts.room_light import RoomLightsDocument
from contracts.tuning import TARGET_PATTERNS
from player.calibration_target import target_modules
from player.calibration_trial import AppliedTrial
from player.controller_budget import PI5, ControllerBudget, fits
from player.decode import StillPicture
from player.fit_pass import DrawTarget, FitRequest, FitSource, target_size
from player.geometry import FrameLights, picture_board, picture_lights
from player.gpu import DmabufFrame, Target
from player.look import fade_end, look, moments
from player.luts import LutSource
from player.pacing import DEFAULT_REFRESH, TOLERANCE, FramePacer, Word
from player.pattern_pass import PatternRequest
from player.plane_screen import (
    FrameGrant,
    OverlayCorners,
    PresentRefused,
    ScreenPicture,
    composition_tag,
    overlay_corners,
)
from player.planes import Composition, FrameLayers, Plane, Screen, compose
from player.rendering import (
    CapacityResult,
    LocalLayer,
    OutputComposition,
    PrepareResult,
    PresentationResult,
)
from player.visibility import visible_now

VIDEO_TARGETS: Final = 3        # a playing video's targets: one shown, one in flight, one drawn
STEP_GAP: Final = DEFAULT_REFRESH
"""Seconds between two finishing steps of stills, over every Output (module rule): one a refresh."""
BAND_BYTES: Final = 4 << 20
"""The bytes of a still's rows one upload step copies (a 4K picture's 33 MB in eight steps)."""


class Screens(Protocol):
    """player/plane_screen.py `PlaneScreens`, as this module uses it."""

    def screen(self, output_id: str) -> Screen | None: ...

    def grant(self, composition: OutputComposition) -> FrameGrant | None: ...

    def picture(self, output_id: str) -> PictureOperations | None: ...

    def trial(self, composition: OutputComposition, grant: FrameGrant) -> AppliedTrial | None: ...

    def import_picture(self, target: Target) -> ScreenPicture | None: ...

    def busy(self, picture: ScreenPicture) -> bool: ...

    def forget(self, picture: ScreenPicture) -> None: ...

    def present(self, composition: Composition, pictures: Mapping[str, ScreenPicture], grant: FrameGrant,
                tag: str, picture_change: int | None, ground: ScreenPicture | None = None,
                overlay: OverlayCorners | None = None) -> int | PresentRefused: ...

    def presentation(self, output_id: str, now: float) -> Word | None: ...


class Device(Protocol):
    """player/gpu.py `Gpu`, as this module uses it."""

    def target(self, size: tuple[int, int], *, opaque: bool) -> Target: ...

    def free(self, target: Target) -> None: ...

    def texture(self, size: tuple[int, int]) -> int: ...

    def upload(self, texture: int, size: tuple[int, int], stride: int, pixels: bytes,
               rows: tuple[int, int]) -> None: ...

    def delete_texture(self, texture: int) -> None: ...

    def import_frame(self, frame: DmabufFrame) -> int: ...

    def forget_frames(self, fds: tuple[int, ...]) -> None: ...

    def fence(self) -> int: ...

    def signalled(self, fence: int) -> bool: ...

    def forget_fence(self, fence: int) -> None: ...


class Fitter(Protocol):
    """player/fit_pass.py `FitPass`, as this module uses it."""

    def draw(self, request: FitRequest, target: DrawTarget) -> None: ...

    def black(self, target: DrawTarget) -> None: ...


class Patterns(Protocol):
    """player/pattern_pass.py `PatternPass`, as this module uses it."""

    def draw(self, request: PatternRequest, target: DrawTarget) -> None: ...


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
    """One target the fit pass drew into, the compositor's buffer of it, and the GPU's fence after
    the draw (0 once it has signalled: the picture is finished)."""
    target: Target
    shown: ScreenPicture
    fence: int = 0


@dataclass(frozen=True)
class _Drawn:
    """The Position a Frame is drawn with now (module rule): the trial's while one is drawn, else
    the binding's calibration and lighting."""
    calibration: Calibration
    lighting: FrameLighting
    trial: AppliedTrial | None

    @property
    def applied(self) -> AppliedCalibration:
        """What a presented answer reports: the drawn calibration, with the trial's identity."""
        if self.trial is None:
            return AppliedCalibration(calibration=self.calibration)
        candidate = self.trial.candidate
        return AppliedCalibration(calibration=self.calibration, trial_id=str(candidate.trial_id),
                                  trial_generation=candidate.generation, trial_sequence=candidate.sequence,
                                  candidate_sha256=candidate.candidate_sha256)


@dataclass(frozen=True)
class _Setting:
    """What a Frame's pictures are finished for now: the Position drawn, its screen, its placement
    on it and the profile's aspect, the picture operations drawn and the change a tag names (None:
    the stand-in, never tagged), the Frame's lights, its LUT and the LUTs' generation."""
    drawn: _Drawn
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

    @property
    def ground(self) -> tuple[int, int]:
        """The screen's ground as it is laid out: the whole screen's target."""
        return target_size(Placement(0, 0, *self.screen.size), self.screen.turn)


@dataclass
class _Layer:
    """One media layer's decode and its finished pictures. A still: its source taken from the
    decode, uploaded into `texture` a band at a time (`uploaded` rows so far), then one picture,
    finished again when its key changes. A video: up to VIDEO_TARGETS pictures, the newest decoded
    frame drawn into the first the screen has released. `picture` is the newest finished picture
    and `key` what it was finished for; `drawing` a picture drawn whose fence has not signalled,
    for `drawing_key` (a video's, of the frame `drawing_frame`); `held` is a video's frame in
    `picture` (kept, so a new key draws it again), `waiting` a newer one not drawn yet, `started`
    whether a present has played it. `serial` counts the pictures finished (module rule, Nothing
    changed)."""
    local: LocalLayer
    binding: OutputBinding
    still: Still | None = None
    video: Video | None = None
    source: StillPicture | None = None
    texture: int = 0
    uploaded: int = 0
    picture: _Picture | None = None
    key: tuple | None = None
    drawing: _Picture | None = None
    drawing_key: tuple | None = None
    drawing_frame: DmabufFrame | None = None
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
    `on_frame` to that thread (player/decode.py calls it on a streaming thread). While
    `finishing`, the caller presents (and prepares) again on the next refresh: a picture becomes
    showable when the GPU finishes it, which no event announces."""

    def __init__(self, screens: Screens, device: Device, fitter: Fitter, patterns: Patterns, luts: LutSource, *,
                 stills: StillFactory, videos: VideoFactory, budget: ControllerBudget = PI5,
                 clock: Callable[[], float] = time.monotonic, gl_renderer: str | None = None) -> None:
        """`gl_renderer`: the Gpu context's GL_RENDERER (player/rendering.py `Renderer`)."""
        self._screens, self._device, self._fitter, self._patterns, self._luts = (
            screens, device, fitter, patterns, luts)
        self._stills, self._videos, self._budget, self._clock = stills, videos, budget, clock
        self.gl_renderer = gl_renderer
        self._room_lights: RoomLightsDocument | None = None
        self._player_id: str | None = None
        self._qr: tuple[TargetCode, QrModules] | None = None       # the newest QR made, by its code
        # Output -> its pattern ground: the request it was drawn for, its picture (None: refused).
        self._grounds: dict[str, tuple[tuple, _Picture | None]] = {}
        self._layers: dict[str, _Layer] = {}
        self._blacks: dict[tuple[str, tuple[int, int]], _Picture] = {}
        self._retired: list[_Picture] = []          # freed once no commit shows them
        self._outputs: dict[str, _Output] = {}
        self._newest: dict[str, Composition] = {}   # every Output's newest composition, for the budget
        self._fades: dict[str, tuple[float, float]] = {}    # Output -> its fade's end (plan, monotonic)
        self._stepped = -float("inf")               # when the last finishing step was taken

    @property
    def finishing(self) -> bool:
        """Whether a picture is drawn and not finished yet (its fence pending) or a still is part
        way through its steps (module rule, Finishing never holds a due commit)."""
        return (any(entry.drawing is not None or (entry.source is not None and entry.picture is None)
                    for entry in self._layers.values())
                or any(picture.fence for picture in self._blacks.values())
                or any(picture is not None and picture.fence for _, picture in self._grounds.values()))

    def set_room_lights(self, document: RoomLightsDocument) -> None:
        """Every Frame's lights from now on; its pictures are finished again (module rule)."""
        self._room_lights = document

    def set_player(self, player_id: str | None) -> None:
        """The Player's registered id (None: none yet), which a calibration target's QR names
        (player/calibration_target.py `target_modules` of (player id, Output, picture change)); a
        change draws a target ground again (render redesign S3, bead B4): its request changes."""
        self._player_id = player_id

    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult:
        """Whether `compositions` (one per Output) fit the controller budget together at their busiest
        (render redesign S3, bead B4): each composed on its screen by its drawn Position at every
        player/look.py `moments` from now, the busiest per Output by player/controller_budget.py
        `load`, judged by `fits`. An Output whose screen is not there, or whose rectangle does not lie
        on it, counts nothing (its present answers that). `qualified` stays False (the budget's
        two-port numbers are estimates, plan Q6). Asks nothing of the GPU or the compositor but the
        screens' facts."""
        now = self._clock()
        busiest: list[tuple[Composition, ...]] = []
        for composition in compositions:
            try:
                drawn = self._drawn(composition, self._screens.grant(composition))
            except ValueError:
                continue                            # a trial refused: its present answers that
            placed = self._place(composition.binding, drawn)
            if isinstance(placed, str):
                continue
            screen, placement, _aspect = placed
            operations = self._screens.picture(composition.binding.output_id)
            if self._patterned(composition, operations, drawn):
                busiest.append((Composition(screen, ()),))     # a pattern is the ground: no planes
                continue
            frame_id = composition.binding.frame_id
            busiest.append(tuple({compose(screen, [FrameLayers(frame_id, placement, look(composition, at).layers)])
                                  for at in moments(composition, now)}))
        return CapacityResult(all(fits(self._budget, together) for together in product(*busiest)))

    def frame(self) -> None:
        """Present again every Output's held composition (the newest `present` was given) at this
        frame time, so a fade's strengths, a finished picture or a trial's edit reach the screen with
        no planning tick (render redesign S3, bead B5). Commits only what changed, as `present`;
        never draws."""
        raise NotImplementedError("render-planes S3 bead B5")

    def finish(self) -> None:
        """The GPU work owed, after this turn's commits (render redesign S3, bead B5: finishing is
        off the present path): each seen playing video's newest waiting frame drawn into a released
        target and fenced; at most one still step over every Output (STEP_GAP); a pattern or black
        ground drawn and fenced. Never commits and never waits for a fence."""
        raise NotImplementedError("render-planes S3 bead B5")

    def due(self) -> float | None:
        """When `frame` and `finish` are next owed with no event to announce it (monotonic seconds;
        render redesign S3, bead B5): now while `finishing` or a video frame waits to be drawn; else
        the soonest player/look.py `next_change` of a held composition (a trial's expiry included);
        None when nothing changes until an event."""
        raise NotImplementedError("render-planes S3 bead B5")

    def prepare(self, local: LocalLayer, binding: OutputBinding) -> PrepareResult:
        """Decode `local` and finish its picture for `binding`'s Frame as it is drawn now (module
        rules): "prepared" once finished, "pending" while decoding or finishing (or before the
        Output is there, while its trial is refused, or while a fade it is not needed before is
        under way), "failed" ("decode") when its media cannot be decoded or drawn, or the drawn
        Position is not a rectangle on the screen. A black layer is prepared once its black picture
        is finished."""
        setting = self._binding_setting(binding)
        if setting == "gone":
            return PrepareResult("pending")
        if setting == "failed":
            return PrepareResult("failed", "decode")
        if self._deferred(local):
            return PrepareResult("pending")
        picture = self._finish(local, binding, setting)
        if picture == "failed":
            return PrepareResult("failed", "decode")
        return PrepareResult("pending" if picture == "pending" else "prepared")

    def present(self, composition: OutputComposition) -> PresentationResult:
        """Show `composition` (its layers looked at this frame time, player/look.py) on its Output as
        one commit over the screen's ground (or its pattern ground), by its drawn Position, within
        the controller budget with every other Output's last commit (module rules, Outcomes). While
        the screen's last commit has no word from the compositor (player/pacing.py) nothing is
        committed and the answer is the last commit's; a present that shows what the last commit
        shows commits nothing either."""
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
        try:
            setting = self._setting(composition.binding, self._drawn(composition, grant))
        except ValueError:                          # a trial refused
            return _FAILED
        if setting == "gone" or setting == "failed":
            return _FAILED
        ground = self._black(output_id, setting.ground)
        if ground == "failed":
            return _FAILED
        ground = None if ground == "pending" else ground        # the compositor's black meanwhile
        if not state.pacer.free(now):
            return state.last
        if not grant.admitted or not grant.matches(composition):
            return self._candidate(state, setting.screen, grant, ground, now)
        looked = look(composition, now)
        ending = fade_end(composition, now)
        if ending is None:
            self._fades.pop(output_id, None)
        else:
            self._fades[output_id] = (ending, ending - composition.timebase)
        pattern = self._pattern_request(composition, setting)
        if pattern is None:
            self._forget_pattern(output_id)
            composed = compose(setting.screen, [FrameLayers(composition.binding.frame_id, setting.placement,
                                                            looked.layers)])
        else:
            composed = Composition(setting.screen, ())
        if not fits(self._budget, tuple({**self._newest, output_id: composed}.values())):
            return PresentationResult("failed", "capacity")
        pictures: dict[str, ScreenPicture] = {}
        if pattern is not None:
            ground = self._pattern(output_id, pattern, setting)
            if ground == "failed":
                return _FAILED
            if ground == "pending":
                return _PENDING                     # the screen keeps its last commit
        for plane in composed.planes:
            picture = self._finish(plane.layer, composition.binding, setting, live=True)
            if picture == "failed":
                return _FAILED
            if picture == "pending":
                return _PENDING                     # the screen keeps its last commit
            pictures[plane.layer.layer.assignment_id] = picture
        tag = composition_tag(looked, visible_now(looked.layers))
        drawn = setting.drawn
        overlay = None if drawn.trial is None else overlay_corners(setting.placement, setting.screen.size)
        shows = (composed.screen, tuple(self._shown(plane, pictures) for plane in composed.planes), tag,
                 setting.tagged, grant.grant_id, None if ground is None else ground.handle, overlay, drawn.applied)
        if state.committed == shows and state.last.status == "presented":
            return state.last
        sequence = self._screens.present(composed, pictures, grant, tag, setting.tagged, ground, overlay)
        if not isinstance(sequence, int):
            return _FAILED
        state.pacer.submitted(sequence, now)
        state.shown = frozenset([*(picture.handle for picture in pictures.values()),
                                 *(() if ground is None else (ground.handle,))])
        state.seen = frozenset(pictures)
        state.committed = shows
        state.last = PresentationResult("presented", composition=looked, presented_at=now,
                                        applied_calibration=drawn.applied)
        self._newest[output_id] = composed
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
            frames = {id(frame): frame for frame in (entry.held, entry.waiting, entry.drawing_frame)
                      if frame is not None}
            for frame in frames.values():
                entry.video.done(frame)
            self._device.forget_frames(entry.video.buffers())
            entry.video.close()
            self._retired += entry.targets
        else:
            self._retired += [picture for picture in (entry.picture, entry.drawing) if picture is not None]
        if entry.texture:
            self._device.delete_texture(entry.texture)
        self._sweep()

    def close(self) -> None:
        """Release every layer and stop every decode."""
        for assignment_id in list(self._layers):
            self.release(assignment_id)
        for output_id in list(self._grounds):
            self._forget_pattern(output_id)
        for picture in [*self._retired, *self._blacks.values()]:
            self._free(picture)
        self._retired.clear()
        self._blacks.clear()
        self._outputs.clear()
        self._newest.clear()

    def _drawn(self, composition: OutputComposition, grant: FrameGrant | None) -> _Drawn:
        """The Position `composition`'s Frame is drawn with now (module rule): the trial
        `Screens.trial` holds under `grant` (none without a grant), else the binding's. ValueError
        when the trial refuses (player/calibration_trial.py `TrialState.apply`)."""
        trial = None if grant is None else self._screens.trial(composition, grant)
        if trial is None:
            return _Drawn(composition.binding.calibration, composition.binding.lighting, None)
        return _Drawn(trial.calibration, trial.lighting, trial)

    def _place(self, binding: OutputBinding, drawn: _Drawn) -> tuple[Screen, Placement, float] | Literal[
            "gone", "failed"]:
        """The Frame's screen, its placement on it by the drawn Position and the profile's aspect;
        "failed" when the Position is not a RectangleFit (s3-page decision 10) or does not lie on the
        screen, "gone" without the screen."""
        fit = drawn.calibration.fit
        if not isinstance(fit, RectangleFit):
            return "failed"
        screen = self._screens.screen(binding.output_id)
        if screen is None:
            return "gone"
        aspect = binding.profile.width_px / binding.profile.height_px
        try:
            return screen, place(fit.rectangle(), screen.size, aspect), aspect
        except ValueError:
            return "failed"

    def _setting(self, binding: OutputBinding, drawn: _Drawn) -> _Setting | Literal["gone", "failed"]:
        """What the Frame's pictures are finished for now, drawn by `drawn` (`_place`'s answers
        otherwise)."""
        placed = self._place(binding, drawn)
        if isinstance(placed, str):
            return placed
        screen, placement, aspect = placed
        operations = self._screens.picture(binding.output_id)
        shown = operations or neutral_operations(binding.output_id, 1, UPGRADE_DISPLAY_LIGHT)
        lights = picture_lights(drawn.lighting, self._room_lights, binding.profile, shown.light)
        ref = drawn.lighting.paper.profile
        return _Setting(drawn, screen, placement, aspect, shown,
                        None if operations is None else operations.picture_change, lights,
                        None if ref is None else self._luts.lut(ref), self._luts.generation())

    def _binding_setting(self, binding: OutputBinding) -> _Setting | Literal["gone", "failed"]:
        """`_setting` of `binding`'s Frame with the trial read under its binding's grant (module
        rule); "gone" while that trial refuses (a prepare waits for the next tick)."""
        composition = OutputComposition(binding)
        try:
            return self._setting(binding, self._drawn(composition, self._screens.grant(composition)))
        except ValueError:
            return "gone"

    @staticmethod
    def _patterned(composition: OutputComposition, operations: PictureOperations | None, drawn: _Drawn) -> bool:
        """Whether the Output shows a pattern ground (module rule, When a pattern shows)."""
        return ((operations is not None and operations.test_pattern is not None)
                or (drawn.trial is not None and composition.fallback and not composition.layers))

    def _pattern_request(self, composition: OutputComposition, setting: _Setting) -> PatternRequest | None:
        """The pattern ground `composition`'s Output shows now; None when it shows its pictures."""
        if not self._patterned(composition, setting.operations, setting.drawn):
            return None
        board = None
        if setting.operations.test_pattern in TARGET_PATTERNS:
            board = picture_board(composition.binding.profile, self._modules(setting.operations))
        return PatternRequest(setting.screen.size, setting.placement, setting.screen.turn, setting.operations,
                              setting.lights, setting.lut, board)

    def _modules(self, operations: PictureOperations) -> QrModules | None:
        """The QR a calibration target shows for `operations` (module rule): the code naming this
        Player, the Output and the picture change, made once per code; None before `set_player`."""
        if self._player_id is None:
            return None
        code = TargetCode(self._player_id, operations.output_id, operations.picture_change)
        if self._qr is None or self._qr[0] != code:
            self._qr = code, target_modules(code)
        return self._qr[1]

    def _pattern(self, output_id: str, request: PatternRequest,
                 setting: _Setting) -> ScreenPicture | Literal["pending", "failed"]:
        """The Output's pattern ground for `request` (module rule): drawn when the request (or the
        LUTs' generation) changed since the last, "pending" until the GPU finished it; "failed"
        while the pass, the Gpu or the compositor refuses this request."""
        key = (request, setting.generation)
        held = self._grounds.get(output_id)
        if held is None or held[0] != key:
            self._forget_pattern(output_id)
            try:
                picture = self._draw(lambda target: self._patterns.draw(request, target), setting.ground,
                                     opaque=True)
            except (RuntimeError, ValueError):      # FitFailed, a Gpu or compositor refusal
                picture = None
            held = self._grounds[output_id] = (key, picture)
        picture = held[1]
        if picture is None:
            return "failed"
        return picture.shown if self._finished(picture) else "pending"

    def _forget_pattern(self, output_id: str) -> None:
        """Retire the Output's pattern ground (freed once no commit shows it)."""
        held = self._grounds.pop(output_id, None)
        if held is not None and held[1] is not None:
            self._retired.append(held[1])

    def _deferred(self, local: LocalLayer) -> bool:
        """Whether `local` is a still not needed before a fade under way on some Output is over: it
        starts after that fade ends (module rule, Finishing never holds a due commit)."""
        variant = local.layer.variant
        if local.layer.presentation != "media" or variant is None or variant.duration is not None:
            return False
        now = self._clock()
        return any(until > now and local.layer.start > ends for ends, until in self._fades.values())

    def _shown(self, plane: Plane, pictures: Mapping[str, ScreenPicture]) -> tuple:
        """What `plane` shows (module rule, Nothing changed): its Frame and layer, its picture and
        that picture's finish, its placement and strength; never the layer's play position."""
        assignment_id = plane.layer.layer.assignment_id
        entry = self._layers.get(assignment_id)
        return (plane.frame_id, assignment_id, pictures[assignment_id].handle, 0 if entry is None else entry.serial,
                plane.at, plane.strength)

    def _candidate(self, state: _Output, screen: Screen, grant: FrameGrant, ground: ScreenPicture | None,
                   now: float) -> PresentationResult:
        """The candidate's commit: no planes (the screen's ground), tagged for its grant, on every
        present the pacer allows (module rule, Outcomes)."""
        sequence = self._screens.present(Composition(screen, ()), {}, grant, f"candidate-{grant.grant_id}", None,
                                         ground)
        if not isinstance(sequence, int):
            return _FAILED
        state.pacer.submitted(sequence, now)
        state.shown = frozenset(() if ground is None else (ground.handle,))
        state.seen, state.committed = frozenset(), None
        self._sweep()
        state.last = _PENDING
        return _PENDING

    def _finish(self, local: LocalLayer, binding: OutputBinding, setting: _Setting, *,
                live: bool = False) -> ScreenPicture | Literal["pending", "failed"]:
        """`local`'s picture for `setting` (module rule), its decode started when it has none.
        `live`: for a present (a video starts, module rule), not a prepare."""
        if local.layer.presentation == "black":
            return self._black(binding.output_id, setting.size)
        entry = self._layers.get(local.layer.assignment_id)
        if entry is None:
            entry = self._layers[local.layer.assignment_id] = self._decode(local, binding, live)
        entry.binding = binding
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

    def _decode(self, local: LocalLayer, binding: OutputBinding, live: bool) -> _Layer:
        entry = _Layer(local, binding, started=live)
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

    def _may_step(self) -> bool:
        """Whether a still's finishing step may be taken now: none was within STEP_GAP (module
        rule, Finishing never holds a due commit)."""
        return self._clock() - self._stepped >= STEP_GAP * (1 - TOLERANCE)

    def _finish_still(self, entry: _Layer, setting: _Setting) -> ScreenPicture | Literal["pending", "failed"]:
        """The still's finished picture for `setting`, else one step towards it (module rule)."""
        key = self._key(entry, setting)
        if entry.drawing is not None:
            if entry.drawing_key != key:
                self._retired.append(entry.drawing)     # drawn for what is no longer so
                entry.drawing = None
            elif self._finished(entry.drawing):
                if entry.picture is not None:
                    self._retired.append(entry.picture)
                entry.picture, entry.key, entry.drawing = entry.drawing, key, None
                entry.serial += 1
        if entry.picture is not None and entry.key == key:
            return entry.picture.shown
        if entry.drawing is not None or not self._may_step():
            return "pending"
        if entry.source is None:
            result = entry.still.result()
            if result == "pending":
                return "pending"                    # nothing taken: no step spent
            self._stepped = self._clock()
            if result == "failed":
                entry.failed = True
                return "failed"
            entry.source = result
            return "pending"
        self._stepped = self._clock()
        source = entry.source
        height = source.size[1]
        if entry.uploaded < height:
            if not entry.texture:
                entry.texture = self._device.texture(source.size)
            rows = min(height - entry.uploaded, max(1, BAND_BYTES // source.stride))
            self._device.upload(entry.texture, source.size, source.stride, source.pixels, (entry.uploaded, rows))
            entry.uploaded += rows
            if entry.uploaded == height:
                entry.source = replace(source, pixels=b"")     # its pixels are the texture's now
            return "pending"
        request = self._request(entry, FitSource(entry.texture, False, source.size, "srgb", source.alpha), setting)
        entry.drawing = self._draw(lambda target: self._fitter.draw(request, target), setting.size,
                                   opaque=not source.alpha)
        entry.drawing_key = key
        return "pending"

    def _finish_video(self, entry: _Layer, setting: _Setting) -> ScreenPicture | Literal["pending", "failed"]:
        """The newest finished frame for `setting`; a newer decoded frame drawn when a target is
        free and no draw is outstanding. Before it started, only its first frame is taken (module
        rule); later ones wait in the decoder."""
        video = entry.video
        key = self._key(entry, setting)
        if any(picture.target.size != setting.size for picture in entry.targets):
            self._retired += entry.targets          # laid out for another size or turn
            entry.targets, entry.picture = [], None
            if entry.drawing is not None:
                self._requeue(entry, entry.drawing_frame)
                entry.drawing = entry.drawing_frame = None
        if entry.drawing is not None and self._finished(entry.drawing):
            if entry.held is not None and entry.held is not entry.drawing_frame:
                video.done(entry.held)
            entry.held, entry.picture, entry.key = entry.drawing_frame, entry.drawing, entry.drawing_key
            entry.drawing = entry.drawing_frame = None
            entry.serial += 1
        taken = any(frame is not None for frame in (entry.held, entry.waiting, entry.drawing_frame))
        frame = video.latest() if entry.started or not taken else None
        if frame is not None:
            self._requeue(entry, frame)
        if entry.drawing is None:
            source = entry.waiting or (entry.held if entry.key != key else None)
            target = None if source is None else self._released(entry, setting.size)
            if target is not None:
                texture = self._device.import_frame(source)
                request = self._request(entry, FitSource(texture, True, source.size, "bt709", False), setting)
                self._fitter.draw(request, target.target)
                target.fence = self._device.fence()
                entry.drawing, entry.drawing_key, entry.drawing_frame = target, key, source
                if source is entry.waiting:
                    entry.waiting = None
        if entry.picture is None or entry.key != key:
            return "pending"
        return entry.picture.shown

    @staticmethod
    def _requeue(entry: _Layer, frame: DmabufFrame | None) -> None:
        """`frame` waits to be drawn, unless a newer one already does (the older goes back)."""
        if frame is None or frame is entry.held or frame is entry.waiting:
            return
        if entry.waiting is not None:
            if entry.waiting.serial > frame.serial:
                entry.video.done(frame)
                return
            entry.video.done(entry.waiting)
        entry.waiting = frame

    def _released(self, entry: _Layer, size: tuple[int, int]) -> _Picture | None:
        """The first of the video's targets the screen has released and that holds neither its
        finished picture nor a draw (a new one while it has fewer than VIDEO_TARGETS); None when
        every one is in use."""
        for picture in entry.targets:
            if picture is not entry.picture and picture is not entry.drawing and not self._showing(picture):
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

    def _black(self, output_id: str, size: tuple[int, int]) -> ScreenPicture | Literal["pending", "failed"]:
        """The Output's one black picture of `size`, shared by its screen's ground and every black
        layer on it; "pending" until the GPU has finished it."""
        picture = self._blacks.get((output_id, size))
        if picture is None:
            try:
                target = self._device.target(size, opaque=True)
            except RuntimeError:
                return "failed"
            try:
                self._fitter.black(target)
                fence = self._device.fence()
            except RuntimeError:
                self._device.free(target)
                return "failed"
            shown = self._screens.import_picture(target)
            if shown is None:
                self._device.forget_fence(fence)
                self._device.free(target)
                return "failed"
            picture = self._blacks[output_id, size] = _Picture(target, shown, fence)
        return picture.shown if self._finished(picture) else "pending"

    def _draw(self, paint: Callable[[Target], None], size: tuple[int, int], *, opaque: bool) -> _Picture:
        """A new target of `size` painted (the fit pass's or the pattern pass's draw) and fenced,
        imported by the compositor. RuntimeError (or the pass's ValueError) when the Gpu, the pass
        or the compositor refuses."""
        target = self._device.target(size, opaque=opaque)
        try:
            paint(target)
            fence = self._device.fence()
        except (RuntimeError, ValueError):
            self._device.free(target)
            raise
        shown = self._screens.import_picture(target)
        if shown is None:
            self._device.forget_fence(fence)
            self._device.free(target)
            raise RuntimeError("picture_refused")
        return _Picture(target, shown, fence)

    def _finished(self, picture: _Picture) -> bool:
        """Whether the GPU has finished `picture` (its fence signalled; asked, never waited for)."""
        if picture.fence and self._device.signalled(picture.fence):
            self._device.forget_fence(picture.fence)
            picture.fence = 0
        return not picture.fence

    def _video_frame(self, assignment_id: str) -> None:
        """A video's new frame (on the owning thread): draw it while the video is seen, or its
        first (module rule). Its commit is the next present's (s3-page: the frame loop's, B5)."""
        entry = self._layers.get(assignment_id)
        if entry is None or entry.failed:
            return
        seen = entry.started and any(assignment_id in state.seen for state in self._outputs.values())
        if (entry.picture is not None or entry.drawing is not None) and not seen:
            return
        setting = self._binding_setting(entry.binding)
        if setting == "gone" or setting == "failed":
            return
        self._finish(entry.local, entry.binding, setting, live=entry.started)

    def _showing(self, picture: _Picture) -> bool:
        """Whether a commit shows `picture`: its screen's last commit, or one the compositor still
        holds it for."""
        return (any(picture.shown.handle in state.shown for state in self._outputs.values())
                or self._screens.busy(picture.shown))

    def _free(self, picture: _Picture) -> None:
        if picture.fence:
            self._device.forget_fence(picture.fence)
            picture.fence = 0
        self._screens.forget(picture.shown)
        self._device.free(picture.target)

    def _sweep(self) -> None:
        """Free every retired picture no commit shows any more."""
        kept = []
        for picture in self._retired:
            if self._showing(picture):
                kept.append(picture)
            else:
                self._free(picture)
        self._retired = kept


__all__ = ["BAND_BYTES", "STEP_GAP", "VIDEO_TARGETS", "Device", "Fitter", "Patterns", "PlaneRenderer", "Screens",
           "Still", "StillFactory", "Video", "VideoFactory"]
