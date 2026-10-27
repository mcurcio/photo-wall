"""The Player's renderer (player/rendering.py `Renderer`): each layer's picture finished once at its
finish size (player/planes.py `finish_size`), shown as a plane the display controller places and fades (render redesign, run ledgers
.claude/runs/render-planes/s1-page.md and s3-page.md), the Player's only renderer.

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
  calibration; its box is placed by `place_box` (a box always lies on its screen, so a placement
  never fails). The drawn lighting is the trial's while one is
  drawn, else the binding's. Every presented answer names the drawn Position (`applied_calibration`,
  with the trial's identity while one is drawn), and a commit under a trial carries the trial
  overlay's corners of the drawn placement and the trial's tag (player/plane_screen.py
  `commit_tag`), with photos or with nothing to play (the commissioning grid).
- **When a pattern shows** (s3-page decision 2): while the Output's picture operations carry a test
  pattern, or a trial is drawn over a fallback with no layers (Position's commissioning grid), the
  commit has no planes and its ground is the pattern's: one target per Output, drawn again exactly
  when its request changes (the LUTs' generation counted, as a still's key), fenced, and "pending"
  (the screen keeps its last commit) until finished. A calibration target's QR names the Player
  once `set_player` gave an id. The composition reported and tagged stays the looked one: a
  pattern is a tuning overlay, not a change of what plays.
- **When a still is decoded**: in `finish`, never in `prepare` or `present`, and at most one still
  decode in flight over every Output (the next starts once it answers, its layer is released, its
  start is refused, or its layer no longer needs it; one answering while its layer waits for a
  fade is taken then: `_settle_decoding`). A still's decode hands
  the GPU only its crop window at its finish size (player/planes.py `still_region`), so its
  texture is at most the finish size, never the whole copy. A new crop window or a new finish
  size (a new crop, a Position whose box changes shape, or size below the source) decodes it
  again and deletes its old texture; the turn, the picture operations, the lights and the LUT
  only draw it again from the texture it has.
- **When a picture is finished again**: a still when anything its pixels depend on changes (its
  decode, its finish size (player/planes.py `finish_size`: the smaller of its cropped source and
  the placement, which the display controller enlarges to the placement), the turn, the crop,
  the placement's aspect, the Output's picture operations, the Frame's lights, its LUT's
  generation); a playing video on each new decoded frame while it is seen (in its screen's last
  commit), into the first of its VIDEO_TARGETS targets the screen has released. Never a fade: a
  fade is a strength.
- **A video's targets are made together**: all VIDEO_TARGETS, at its finish size, by the `finish`
  that draws its first frame (during its prepare lead), never one by one on its first frames
  during the fade that reveals it (Pi diagnosis, milestone 3: ~10 ms of CPU and an import each).
- **Finishing is off the present path** (s3-page decision 4): `present`, `frame` and `prepare` never
  draw, upload, import or fence; they commit and report only pictures the GPU has finished (a fence
  signalled, player/gpu.py: asked, never waited for), so no GPU work sits between a due commit and
  the screen, and a video's newer frame meanwhile leaves its last finished one on screen. A video's
  decoder callback only records its newest frame. `finish`, run after the turn's commits, does the
  work owed, in this order: each seen started video's newest waiting frame (or a prepared video's
  first) drawn into a released target and fenced, one draw per video per call; every pattern ground
  and black picture owed, drawn and fenced; at most one still step over every Output (its decode
  started, its decode taken, a band of BAND_BYTES of its rows uploaded, or its draw) once STEP_GAP
  has passed since the last, seen layers first, then by start. A still not needed before a fade under way on any Output
  is over (it starts after that fade ends) takes no step during it.
- **Every fence is asked**: `finish` ends by asking every outstanding fence (each black picture's,
  each pattern ground's, each layer's draw), so none waits for a present to ask for its picture
  again; a failed layer's draw is given up at once (a still's retired, a video's target and frame
  given back). While a fence is pending and nothing else is owed, `due` is FENCE_POLL away, never
  now: a turn never spins at FRAME priority over the planning tick. A present whose screen's last
  commit is unanswered answers at once, before any sweep, grant or setting work (the last answer
  if it was made for this request, else "pending": Outcomes),
  and `due` reads each such screen's word before judging it overdue, so a turn polling a fence
  costs a read, not a present (Pi diagnosis, milestone 3: the main thread at 87 % while a video's
  fence was pending).
- **The held composition**: each Output's newest composition given to `present` is held, and `frame`
  presents it again at each frame time (a fade's strengths, a newly finished picture, a trial's edit
  reach the screen with no planning tick) until all its layers are released or the renderer closes.
- **The screen's ground**: every commit shows, beneath its planes, the Output's black picture laid
  out for the whole screen (the one black picture of that size, shared with black layers; a Frame's
  dark plane, player/planes.py, shows the black picture of its placement's size), so
  the display controller can always scan out what the planes leave uncovered and the compositor
  never needs its renderer for the screen's black. An Output keeps black pictures of two sizes at
  most, its present's ground and its Frame's placement: a present retires the others (freed once
  no commit shows them), so a trial's edits or a new mode never pile up black targets.
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
  a trial refused or a picture refused: "failed"
  ("decode"); over the controller budget with every other Output's last commit: "failed"
  ("capacity"), nothing committed; a seen layer's picture (or the pattern) not finished yet:
  "pending", the screen keeps its last commit; else "presented" (that commit), its composition the
  looked one. A "presented" answers only the request asked (player/rendering.py
  `OutputComposition.request`; the frame loop and the planning tick share the one commit in flight):
  while the screen's last commit is unanswered, the last answer if it was made for this request,
  else "pending"; a present that shows what the last commit shows answers with this request's look.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from itertools import product
from pathlib import Path
from typing import Final, Literal, Protocol

from contracts.calibration_target import QrModules, TargetCode
from contracts.frame_fit import Placement, place_box
from contracts.frame_lighting import FrameLighting
from contracts.models import (
    AppliedCalibration,
    Calibration,
    Layer,
    OutputBinding,
    PhotoCrop,
    frame_size_mm,
)
from contracts.node_frame import PictureOperations, neutral_operations
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT
from contracts.photo_crop import CENTRED
from contracts.room_light import RoomLightsDocument
from contracts.tuning import TARGET_PATTERNS
from player.calibration_target import target_modules
from player.calibration_trial import AppliedTrial
from player.controller_budget import PI5, ControllerBudget, fits
from player.decode import StillPicture, StillRegion
from player.fit_pass import DrawTarget, FitRequest, FitSource, target_size
from player.geometry import FrameLights, picture_board, picture_lights
from player.gpu import DmabufFrame, Target
from player.look import fade_end, look, moments, next_change
from player.luts import LutSource
from player.pacing import DEFAULT_REFRESH, TOLERANCE, FramePacer, Word
from player.pattern_pass import PatternRequest
from player.plane_screen import (
    FrameGrant,
    OverlayCorners,
    PresentRefused,
    ScreenPicture,
    commit_tag,
    overlay_corners,
)
from player.planes import (
    Composition,
    FrameLayers,
    Plane,
    Screen,
    compose,
    finish_size,
    still_region,
)
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
FENCE_POLL: Final = 0.001
"""Seconds between two asks of a pending fence while nothing else is owed (module rule, Every fence
is asked): the frame loop sleeps between them, so the planning tick and the decoders' callbacks run."""
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


StillFactory = Callable[[Path, StillRegion], Still]
VideoFactory = Callable[[Path, Callable[[], None]], Video]


_PENDING: Final = PresentationResult("pending")
_FAILED: Final = PresentationResult("failed", "decode")


def _answers(result: PresentationResult, composition: OutputComposition) -> bool:
    """Whether `result` is an answer about `composition`: one made for its request
    (player/rendering.py `OutputComposition.request`; module rule, Outcomes)."""
    return result.composition is not None and result.composition.request == composition.request


def _expiry(trial: AppliedTrial, now: float) -> float:
    """When a drawn Position trial runs out, on the renderer's clock (`now` read from it): its expiry
    is on CLOCK_BOOTTIME (player/plane_screen.py), read against it here at once."""
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    clock_now = time.monotonic() if boottime is None else time.clock_gettime(boottime)
    return now + trial.candidate.expires_boottime_ms / 1000 - clock_now


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
    on it (whose `aspect` is the Frame's shape), the picture operations drawn and the change a tag names (None:
    the stand-in, never tagged), the Frame's lights, its LUT and the LUTs' generation."""
    drawn: _Drawn
    screen: Screen
    placement: Placement
    operations: PictureOperations
    tagged: int | None
    lights: FrameLights
    lut: bytes | None
    generation: int

    @property
    def size(self) -> tuple[int, int]:
        """A black layer's picture as it is laid out on the screen: the whole placement's target
        (player/fit_pass.py)."""
        return target_size((self.placement.width, self.placement.height), self.screen.turn)

    def finish(self, layer: Layer) -> tuple[int, int]:
        """`layer`'s finish size as it hangs (player/planes.py `finish_size` at this placement)."""
        return finish_size(layer, self.placement)

    def target(self, layer: Layer) -> tuple[int, int]:
        """`layer`'s picture as it is laid out on the screen: its finish size's target."""
        return target_size(self.finish(layer), self.screen.turn)

    @property
    def ground(self) -> tuple[int, int]:
        """The screen's ground as it is laid out: the whole screen's target."""
        return target_size(self.screen.size, self.screen.turn)


@dataclass
class _Layer:
    """One media layer's decode and its finished pictures. A still: its decode of `region` (none
    until `finish` starts it), its source taken from the decode, uploaded into `texture` a band at
    a time (`uploaded` rows so far), then one picture, finished again when its key changes. A video: up to VIDEO_TARGETS pictures, the newest decoded
    frame drawn into the first the screen has released. `picture` is the newest finished picture
    and `key` what it was finished for; `drawing` a picture drawn whose fence has not signalled,
    for `drawing_key` (a video's, of the frame `drawing_frame`); `held` is a video's frame in
    `picture` (kept, so a new key draws it again), `waiting` a newer one not drawn yet, `started`
    whether a present has played it. `serial` counts the pictures finished (module rule, Nothing
    changed); `want` is the setting the last prepare or present asked it for, which `finish`
    finishes it for."""
    local: LocalLayer
    binding: OutputBinding
    still: Still | None = None
    region: StillRegion | None = None
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
    want: _Setting | None = None


@dataclass
class _Output:
    """One screen's commits: its pacer, the last answer, the pictures and the layers its last
    commit shows, and what that commit showed (module rule, Nothing changed); when its last commit
    was made, and when the trial drawn by the last present runs out (on the renderer's clock)."""
    pacer: FramePacer = field(default_factory=FramePacer)
    last: PresentationResult = _PENDING
    shown: frozenset[int] = frozenset()
    seen: frozenset[str] = frozenset()
    committed: tuple | None = None
    submitted: float = -float("inf")
    expires: float | None = None


class PlaneRenderer:
    """The new path's renderer (module rules). Every method on one owning thread (the Player's GLib
    main loop), the thread the Gpu's context is current on. The VideoFactory hands each video's
    `on_frame` to that thread (player/decode.py calls it on a streaming thread). The frame loop
    (player/mainloop.py `FrameLoop`) calls `frame`, then `finish`, on each turn, and turns again at
    `due`: a picture becomes showable when the GPU finishes it, which no event announces."""

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
        self._owed_grounds: dict[str, tuple[tuple, PatternRequest, tuple[int, int]]] = {}  # for `finish`
        self._owed_blacks: set[tuple[str, tuple[int, int]]] = set()                     # for `finish`
        self._refused_blacks: set[tuple[str, tuple[int, int]]] = set()  # answered "failed" once, then owed
        self._retired: list[_Picture] = []          # freed once no commit shows them
        self._held: dict[str, OutputComposition] = {}   # Output -> its held composition (module rule)
        self._outputs: dict[str, _Output] = {}
        self._newest: dict[str, Composition] = {}   # every Output's newest composition, for the budget
        self._fades: dict[str, tuple[float, float]] = {}    # Output -> its fade's end (plan, monotonic)
        self._stepped = -float("inf")               # when the last finishing step was taken
        self._decoding: _Layer | None = None        # the one still decode in flight (module rule)

    @property
    def finishing(self) -> bool:
        """Whether a picture is drawn and not finished yet (its fence pending, or not yet taken up),
        a still is part way through its steps, or a ground is owed (module rule, Finishing is off
        the present path)."""
        return (self._drawn_pending()
                or any(entry.source is not None and entry.picture is None for entry in self._layers.values()))

    def _drawn_pending(self) -> bool:
        """A ground owed to `finish`, or a fence still unsignalled (a layer's draw, a black
        picture's, a pattern ground's): `finish` asks every one (module rule, Every fence is asked)."""
        return self._owed() or self._fenced()

    def _owed(self) -> bool:
        """A pattern ground or a black picture owed to `finish`."""
        return bool(self._owed_blacks or self._owed_grounds)

    def _fenced(self) -> bool:
        """A fence `finish` has not seen signal yet: a layer's draw (taken up by the next
        `finish` once signalled), a black picture's or a pattern ground's."""
        return (any(entry.drawing is not None for entry in self._layers.values())
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
            screen, placement = placed
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
        never draws. Its answers are the renderer's own: the executor hears at its next present."""
        for composition in list(self._held.values()):
            self._present(composition)

    def finish(self) -> None:
        """The GPU work owed, after this turn's commits (module rule, Finishing is off the present
        path): each seen started video's newest waiting frame drawn into a released target and
        fenced; every pattern ground and black picture owed, drawn and fenced; at most one still step
        over every Output (STEP_GAP). Never commits and never waits for a fence."""
        for assignment_id, entry in list(self._layers.items()):
            if entry.failed or entry.want is None:
                continue
            if entry.video is not None:
                self._draw_video(assignment_id, entry)
            elif entry.drawing is not None:
                self._still_ready(entry, entry.want)    # a finished draw taken up: nothing more owed
        for output_id, (key, request, size) in list(self._owed_grounds.items()):
            try:
                picture = self._draw(lambda target, request=request: self._patterns.draw(request, target), size,
                                     opaque=True)
            except (RuntimeError, ValueError):      # FitFailed, a Gpu or compositor refusal
                picture = None
            self._grounds[output_id] = (key, picture)
        self._owed_grounds.clear()
        for owed in sorted(self._owed_blacks):
            try:
                self._blacks[owed] = self._draw(self._fitter.black, owed[1], opaque=True)
            except (RuntimeError, ValueError):
                self._refused_blacks.add(owed)
        self._owed_blacks.clear()
        self._settle_decoding()
        if self._may_step():
            for entry in self._owed_stills():
                if self._still_step(entry):
                    break
        self._ask_fences()

    def _ask_fences(self) -> None:
        """Ask every outstanding fence (module rule, Every fence is asked); a failed layer's draw is
        given up."""
        for picture in self._blacks.values():
            self._finished(picture)
        for _, picture in self._grounds.values():
            if picture is not None:
                self._finished(picture)
        for entry in self._layers.values():
            if entry.drawing is None:
                continue
            if entry.failed:
                self._give_up_drawing(entry)
            else:
                self._finished(entry.drawing)

    def _give_up_drawing(self, entry: _Layer) -> None:
        """A failed layer's draw, never taken up: a still's picture retired, a video's target kept
        among its targets with its fence forgotten and its frame given back to the decoder."""
        drawing, entry.drawing = entry.drawing, None
        if entry.video is None:
            self._retired.append(drawing)
            return
        if drawing.fence:
            self._device.forget_fence(drawing.fence)
            drawing.fence = 0
        frame, entry.drawing_frame = entry.drawing_frame, None
        if frame is not None and frame is not entry.held and frame is not entry.waiting:
            entry.video.done(frame)

    def due(self) -> float | None:
        """When `frame` and `finish` are next owed with no event to announce it (monotonic seconds;
        render redesign S3, bead B5): now while a ground is owed or a video frame waits to be drawn
        into a free target; FENCE_POLL from now while a fence is pending (module rule, Every fence
        is asked); the time the
        next still step may be taken while a still is owed one (after the fade it waits for, module
        rule); the soonest player/look.py `next_change` of a held composition (a trial's expiry
        included), though a change owed while that screen's last commit is unanswered is announced
        by the answer (the compositor speaks: the frame loop's read), so it is due only once that
        commit is overdue (its word read first: an answer read since the turn frees it now); None
        when nothing changes until an event."""
        now = self._clock()
        if self._owed() or any(self._video_owed(assignment_id, entry)
                               for assignment_id, entry in self._layers.items()):
            return now
        times = [max(now, self._stepped + STEP_GAP * (1 - TOLERANCE), self._deferred_until(entry.local))
                 for entry in self._owed_stills()]
        if self._fenced():
            times.append(now + FENCE_POLL)
        for output_id, composition in self._held.items():
            state = self._outputs.get(output_id)
            expires = None if state is None else state.expires
            change = next_change(composition, now, expires)
            if change is None:
                continue
            if state is not None and state.pacer.in_flight:
                word = self._screens.presentation(output_id, now)      # an answer read since the turn
                if word is not None:
                    state.pacer.answered(word)
            if state is not None and state.pacer.in_flight:
                change = max(change, state.submitted + state.pacer.overdue)
            times.append(max(now, change))
        return min(times, default=None)

    def prepare(self, local: LocalLayer, binding: OutputBinding) -> PrepareResult:
        """Decode `local` and ask `finish` for its picture for `binding`'s Frame as it is drawn now
        (module rules): "prepared" once finished, "pending" while decoding or finishing (or before the
        Output is there, while its trial is refused, or while a fade it is not needed before is
        under way), "failed" ("decode") when its media cannot be decoded or drawn. A black layer is prepared once its black picture
        is finished."""
        setting = self._binding_setting(binding)
        if setting == "gone":
            return PrepareResult("pending")
        if self._deferred(local):
            return PrepareResult("pending")
        picture = self._ready(local, binding, setting)
        if picture == "failed":
            return PrepareResult("failed", "decode")
        return PrepareResult("pending" if picture == "pending" else "prepared")

    def present(self, composition: OutputComposition) -> PresentationResult:
        """Show `composition` (its layers looked at this frame time, player/look.py) on its Output as
        one commit over the screen's ground (or its pattern ground), by its drawn Position, within
        the controller budget with every other Output's last commit (module rules, Outcomes). While
        the screen's last commit has no word from the compositor (player/pacing.py) nothing is
        committed; a present that shows what the last commit shows commits nothing either (the
        answer in each case: module rule, Outcomes). `composition` is held (module rule) for
        `frame`."""
        self._held[composition.binding.output_id] = composition
        return self._present(composition)

    def _present(self, composition: OutputComposition) -> PresentationResult:
        """`present` of `composition`, not held again."""
        output_id = composition.binding.output_id
        now = self._clock()
        state = self._outputs.setdefault(output_id, _Output())
        word = self._screens.presentation(output_id, now)
        if word is not None:
            state.pacer.answered(word)
        if not state.pacer.free(now):               # nothing can be committed: no other work (module rule)
            return state.last if _answers(state.last, composition) else _PENDING
        self._sweep()
        grant = self._screens.grant(composition)
        if grant is None:
            return _PENDING
        try:
            setting = self._setting(composition.binding, self._drawn(composition, grant))
        except ValueError:                          # a trial refused
            return _FAILED
        if setting == "gone":
            return _FAILED
        state.expires = None if setting.drawn.trial is None else _expiry(setting.drawn.trial, now)
        self._keep_blacks(output_id, {setting.ground, setting.size})
        ground = self._black(output_id, setting.ground)
        if ground == "failed":
            return _FAILED
        ground = None if ground == "pending" else ground        # the compositor's black meanwhile
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
            picture = (self._black(output_id, setting.size) if plane.layer is None
                       else self._ready(plane.layer, composition.binding, setting, live=True))
            if picture == "failed":
                return _FAILED
            if picture == "pending":
                return _PENDING                     # the screen keeps its last commit
            pictures[plane.key] = picture
        drawn = setting.drawn
        tag = commit_tag(looked, visible_now(looked.layers), drawn.trial)
        overlay = None if drawn.trial is None else overlay_corners(setting.placement, setting.screen.size)
        shows = (composed.screen, tuple(self._shown(plane, pictures) for plane in composed.planes), tag,
                 setting.tagged, grant.grant_id, None if ground is None else ground.handle, overlay, drawn.applied)
        if state.committed == shows and state.last.status == "presented":
            state.last = replace(state.last, composition=looked)
            return state.last                       # what the screen shows is this request's look
        sequence = self._screens.present(composed, pictures, grant, tag, setting.tagged, ground, overlay)
        if not isinstance(sequence, int):
            return _FAILED
        state.pacer.submitted(sequence, now)
        state.submitted = now
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
        """Drop the layer's decode, targets and compositor buffers once no commit shows them, and
        the layer from the held compositions (one with no layer left is no longer held)."""
        for output_id, held in list(self._held.items()):
            kept = tuple(local for local in held.layers if local.layer.assignment_id != assignment_id)
            if len(kept) == len(held.layers):
                continue
            if kept:
                self._held[output_id] = replace(held, layers=kept)
            else:
                del self._held[output_id]
        entry = self._layers.pop(assignment_id, None)
        if entry is None:
            return
        if self._decoding is entry:
            self._decoding = None
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
        self._owed_blacks.clear()
        self._refused_blacks.clear()
        self._outputs.clear()
        self._newest.clear()
        self._held.clear()

    def _drawn(self, composition: OutputComposition, grant: FrameGrant | None) -> _Drawn:
        """The Position `composition`'s Frame is drawn with now (module rule): the trial
        `Screens.trial` holds under `grant` (none without a grant), else the binding's. ValueError
        when the trial refuses (player/calibration_trial.py `TrialState.apply`)."""
        trial = None if grant is None else self._screens.trial(composition, grant)
        if trial is None:
            return _Drawn(composition.binding.calibration, composition.binding.lighting, None)
        return _Drawn(trial.calibration, trial.lighting, trial)

    def _place(self, binding: OutputBinding, drawn: _Drawn) -> tuple[Screen, Placement] | Literal["gone"]:
        """The Frame's screen and the drawn Position's box on it (`place_box`; its `aspect` is the
        Frame's shape); "gone" without the screen. Never "failed": a box always places."""
        screen = self._screens.screen(binding.output_id)
        if screen is None:
            return "gone"
        return screen, place_box(drawn.calibration.fit.box(), screen.size)

    def _setting(self, binding: OutputBinding, drawn: _Drawn) -> _Setting | Literal["gone"]:
        """What the Frame's pictures are finished for now, drawn by `drawn` (`_place`'s answers
        otherwise)."""
        placed = self._place(binding, drawn)
        if isinstance(placed, str):
            return placed
        screen, placement = placed
        operations = self._screens.picture(binding.output_id)
        shown = operations or neutral_operations(binding.output_id, 1, UPGRADE_DISPLAY_LIGHT)
        size_mm = frame_size_mm(drawn.calibration.fit, binding.display_mm, screen.turn)
        lights = picture_lights(drawn.lighting, self._room_lights, size_mm, shown.light)
        ref = drawn.lighting.paper.profile
        return _Setting(drawn, screen, placement, shown,
                        None if operations is None else operations.picture_change, lights,
                        None if ref is None else self._luts.lut(ref), self._luts.generation())

    def _binding_setting(self, binding: OutputBinding) -> _Setting | Literal["gone"]:
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
            board = picture_board(setting.placement, self._modules(setting.operations))
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
        """The Output's pattern ground for `request` (module rule): owed to `finish` when the request
        (or the LUTs' generation) changed since the last, "pending" until the GPU finished it;
        "failed" while the pass, the Gpu or the compositor refuses this request."""
        key = (request, setting.generation)
        held = self._grounds.get(output_id)
        if held is None or held[0] != key:
            self._forget_pattern(output_id)
            self._owed_grounds[output_id] = (key, request, setting.ground)
            return "pending"
        picture = held[1]
        if picture is None:
            return "failed"
        return picture.shown if self._finished(picture) else "pending"

    def _forget_pattern(self, output_id: str) -> None:
        """Retire the Output's pattern ground (freed once no commit shows it); none is owed."""
        self._owed_grounds.pop(output_id, None)
        held = self._grounds.pop(output_id, None)
        if held is not None and held[1] is not None:
            self._retired.append(held[1])

    def _deferred(self, local: LocalLayer) -> bool:
        """Whether `local` is a still not needed before a fade under way on some Output is over: it
        starts after that fade ends (module rule, Finishing is off the present path)."""
        return self._deferred_until(local) > self._clock()

    def _deferred_until(self, local: LocalLayer) -> float:
        """When the last fade `local` waits for (`_deferred`) is over; -inf when it waits for none."""
        variant = local.layer.variant
        if local.layer.presentation != "media" or variant is None or variant.duration is not None:
            return -float("inf")
        now = self._clock()
        return max((until for ends, until in self._fades.values() if until > now and local.layer.start > ends),
                   default=-float("inf"))

    def _shown(self, plane: Plane, pictures: Mapping[str, ScreenPicture]) -> tuple:
        """What `plane` shows (module rule, Nothing changed): its Frame and layer, its picture and
        that picture's finish, its placement and strength; never the layer's play position."""
        entry = None if plane.layer is None else self._layers.get(plane.key)
        return (plane.frame_id, plane.key, pictures[plane.key].handle, 0 if entry is None else entry.serial,
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
        state.submitted = now
        state.shown = frozenset(() if ground is None else (ground.handle,))
        state.seen, state.committed = frozenset(), None
        self._sweep()
        state.last = _PENDING
        return _PENDING

    def _ready(self, local: LocalLayer, binding: OutputBinding, setting: _Setting, *,
               live: bool = False) -> ScreenPicture | Literal["pending", "failed"]:
        """`local`'s finished picture for `setting` (module rule), else "pending" with the picture
        owed to `finish`; a video's decode started when it has none (a still's is started by
        `finish`, module rule). `live`: for a present (a video starts, module rule), not a
        prepare. No GPU work."""
        if local.layer.presentation == "black":
            return self._black(binding.output_id, setting.size)
        entry = self._layers.get(local.layer.assignment_id)
        if entry is None:
            entry = self._layers[local.layer.assignment_id] = self._decode(local, binding, live)
        entry.binding = binding
        if entry.failed:
            return "failed"
        entry.want = setting
        if entry.video is None:
            return self._still_ready(entry, setting)
        if live and not entry.started:
            entry.started = True                    # it plays from where the layer is now
            try:
                entry.failed = not entry.video.seek(local.position)
            except (RuntimeError, ValueError):      # VideoRefused
                entry.failed = True
            if entry.failed:
                return "failed"
        return self._video_ready(entry, setting)

    def _decode(self, local: LocalLayer, binding: OutputBinding, live: bool) -> _Layer:
        """`local`'s entry: a video's decode started; a still's waits for `finish` (module rule,
        When a still is decoded). Failed for a layer with no file or no variant."""
        entry = _Layer(local, binding, started=live)
        variant = local.layer.variant
        try:
            if local.path is None or variant is None:
                raise ValueError("decode_path")
            if variant.duration is not None:
                assignment_id = local.layer.assignment_id
                entry.video = self._videos(local.path, lambda: self._video_frame(assignment_id))
                entry.failed = not entry.video.seek(local.position)
        except (RuntimeError, ValueError):
            entry.failed = True
        return entry

    @staticmethod
    def _key(entry: _Layer, setting: _Setting) -> tuple:
        """Everything a layer's pixels depend on besides its decode (module rule)."""
        return (setting.target(entry.local.layer), setting.screen.turn, entry.local.layer.crop, setting.placement.aspect,
                setting.operations, setting.lights, setting.generation)

    @staticmethod
    def _request(entry: _Layer, source: FitSource, setting: _Setting, crop: PhotoCrop | None) -> FitRequest:
        """The fit pass's request for `entry` from `source`: a still's source is its crop window
        already (`crop` CENTRED), a video's frame is cut by the layer's crop."""
        return FitRequest(source, crop, setting.placement.aspect, setting.finish(entry.local.layer),
                          setting.screen.turn, setting.operations, setting.lights, setting.lut)

    def _may_step(self) -> bool:
        """Whether a still's finishing step may be taken now: none was within STEP_GAP (module
        rule, Finishing is off the present path)."""
        return self._clock() - self._stepped >= STEP_GAP * (1 - TOLERANCE)

    def _still_ready(self, entry: _Layer, setting: _Setting) -> ScreenPicture | Literal["pending"]:
        """The still's finished picture for `setting`, its finished draw taken up (one drawn for
        what is no longer so retired); else "pending"."""
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
        return "pending"

    def _owed_stills(self) -> list[_Layer]:
        """The stills owed a step for what they were last asked (module rule): seen layers first,
        then by start; none drawing (its fence answers first) and none waiting for a fade."""
        owed = [entry for entry in self._layers.values() if self._still_owed(entry) and not self._deferred(entry.local)]
        return sorted(owed, key=lambda entry: (not self._seen(entry.local.layer.assignment_id),
                                               entry.local.layer.start))

    def _still_owed(self, entry: _Layer) -> bool:
        """Whether `entry` is a still owed a step for what it was last asked, a fade aside: not
        failed, asked, not drawing (its fence answers first), its picture not finished for it."""
        return (entry.video is None and not entry.failed and entry.want is not None and entry.drawing is None
                and (entry.picture is None or entry.key != self._key(entry, entry.want)))

    def _settle_decoding(self) -> None:
        """The still decode in flight never holds up the others (module rule, When a still is
        decoded): one whose layer now waits for a fade has its result taken once it is done (no
        GPU work, so no step), and one whose layer no longer needs it (its picture is finished for
        what it is asked again, or it failed) is given up, its region forgotten so it decodes again
        when asked."""
        entry = self._decoding
        if entry is None or entry.drawing is not None:
            return
        if self._still_owed(entry):
            if self._deferred(entry.local) and entry.still is not None and entry.source is None:
                result = entry.still.result()
                if result != "pending":
                    self._decoding = None
                    entry.failed = result == "failed"
                    entry.source = None if result == "failed" else result
            return
        self._decoding = None
        if entry.still is not None:
            entry.still.close()
        entry.still = entry.region = None

    def _still_step(self, entry: _Layer) -> bool:
        """One step towards the still's picture for what it was last asked (module rule): its decode
        started (for a new crop window or finish size, once no other still decode is in flight), its
        decode taken, a band of its rows uploaded, or its draw; whether a step was spent (a decode
        not done yet, or waiting for another, spends none)."""
        if self._still_ready(entry, entry.want) != "pending" or entry.drawing is not None:
            return False
        wanted = still_region(entry.local.layer, entry.want.placement)
        if entry.region != wanted:
            if self._decoding is not None and self._decoding is not entry:
                return False                        # one still decode in flight
            if entry.still is not None:
                entry.still.close()
            if entry.texture:
                self._device.delete_texture(entry.texture)
            entry.uploaded, entry.source, entry.texture, entry.region = 0, None, 0, wanted
            self._decoding = entry
            self._stepped = self._clock()
            try:
                entry.still = self._stills(entry.local.path, wanted)
            except (RuntimeError, ValueError):
                entry.still, entry.failed, self._decoding = None, True, None
            return True
        if entry.source is None:
            result = entry.still.result()
            if result == "pending":
                return False                        # nothing taken: no step spent
            self._decoding = None
            self._stepped = self._clock()
            if result == "failed":
                entry.failed = True
            else:
                entry.source = result
            return True
        self._stepped = self._clock()
        source = entry.source
        height = source.size[1]
        try:
            if entry.uploaded < height:
                if not entry.texture:
                    entry.texture = self._device.texture(source.size)
                rows = min(height - entry.uploaded, max(1, BAND_BYTES // source.stride))
                self._device.upload(entry.texture, source.size, source.stride, source.pixels,
                                    (entry.uploaded, rows))
                entry.uploaded += rows
                if entry.uploaded == height:
                    entry.source = replace(source, pixels=b"")     # its pixels are the texture's now
                return True
            setting = entry.want
            request = self._request(entry, FitSource(entry.texture, False, source.size, "srgb", source.alpha),
                                    setting, CENTRED)
            entry.drawing = self._draw(lambda target: self._fitter.draw(request, target),
                                       setting.target(entry.local.layer), opaque=not source.alpha)
            entry.drawing_key = self._key(entry, setting)
        except (RuntimeError, ValueError):          # FitFailed, a Gpu or compositor refusal
            entry.failed = True
        return True

    def _video_ready(self, entry: _Layer, setting: _Setting) -> ScreenPicture | Literal["pending"]:
        """The video's newest finished frame for `setting`, its finished draw taken up (targets laid
        out for another size or turn retired); else "pending"."""
        if any(picture.target.size != setting.target(entry.local.layer) for picture in entry.targets):
            self._retired += entry.targets          # laid out for another size or turn
            entry.targets, entry.picture = [], None
            if entry.drawing is not None:
                self._requeue(entry, entry.drawing_frame)
                entry.drawing = entry.drawing_frame = None
        if entry.drawing is not None and self._finished(entry.drawing):
            if entry.held is not None and entry.held is not entry.drawing_frame:
                entry.video.done(entry.held)
            entry.held, entry.picture, entry.key = entry.drawing_frame, entry.drawing, entry.drawing_key
            entry.drawing = entry.drawing_frame = None
            entry.serial += 1
        if entry.picture is None or entry.key != self._key(entry, setting):
            return "pending"
        return entry.picture.shown

    def _seen(self, assignment_id: str) -> bool:
        """Whether a screen's last commit shows the layer."""
        return any(assignment_id in state.seen for state in self._outputs.values())

    def _may_take(self, assignment_id: str, entry: _Layer) -> bool:
        """Whether the video takes its decoder's newest frame (module rule): its first, or any once
        it is started and seen; later frames otherwise wait in the decoder."""
        taken = any(frame is not None for frame in (entry.held, entry.waiting, entry.drawing_frame))
        return not taken or (entry.started and self._seen(assignment_id))

    def _video_source(self, assignment_id: str, entry: _Layer) -> DmabufFrame | None:
        """The frame `finish` draws for the video: its waiting frame (a started one's only while it
        is seen), else its frame in `picture` again for a new setting; None when none is owed."""
        if entry.waiting is not None and (not entry.started or self._seen(assignment_id)):
            return entry.waiting
        if entry.held is not None and entry.key != self._key(entry, entry.want):
            return entry.held
        return None

    def _video_owed(self, assignment_id: str, entry: _Layer) -> bool:
        """Whether `finish` would draw a frame of the video now: one owed and a target free."""
        if entry.video is None or entry.failed or entry.want is None or entry.drawing is not None:
            return False
        if self._video_source(assignment_id, entry) is None:
            return False
        return len(entry.targets) < VIDEO_TARGETS or any(
            picture is not entry.picture and not self._showing(picture) for picture in entry.targets)

    def _draw_video(self, assignment_id: str, entry: _Layer) -> None:
        """`finish`'s draw of the video: its newest frame taken (`_may_take`) and the frame owed
        drawn into a released target and fenced, one draw per call (module rule)."""
        setting = entry.want
        try:
            self._video_ready(entry, setting)
            if self._may_take(assignment_id, entry):
                self._requeue(entry, entry.video.latest())
            if entry.drawing is not None:
                return
            source = self._video_source(assignment_id, entry)
            target = None if source is None else self._released(entry, setting.target(entry.local.layer))
            if target is None:
                return
            texture = self._device.import_frame(source)
            request = self._request(entry, FitSource(texture, True, source.size, "bt709", False), setting,
                                    entry.local.layer.crop)
            self._fitter.draw(request, target.target)
            target.fence = self._device.fence()
            entry.drawing, entry.drawing_key, entry.drawing_frame = target, self._key(entry, setting), source
            if source is entry.waiting:
                entry.waiting = None
        except (RuntimeError, ValueError):          # FitFailed, a Gpu refusal
            entry.failed = True

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
        finished picture nor a draw, its VIDEO_TARGETS targets of `size` made together first when it
        has fewer (module rule, A video's targets are made together); None when every one is in
        use."""
        while len(entry.targets) < VIDEO_TARGETS:
            target = self._device.target(size, opaque=True)
            shown = self._screens.import_picture(target)
            if shown is None:
                self._device.free(target)
                raise RuntimeError("picture_refused")
            entry.targets.append(_Picture(target, shown))
        for picture in entry.targets:
            if picture is not entry.picture and picture is not entry.drawing and not self._showing(picture):
                return picture
        return None

    def _black(self, output_id: str, size: tuple[int, int]) -> ScreenPicture | Literal["pending", "failed"]:
        """The Output's one black picture of `size`, shared by its screen's ground and every black
        layer on it; owed to `finish` when there is none, "pending" until the GPU has finished it,
        "failed" once when `finish` could not draw it (then owed again)."""
        picture = self._blacks.get((output_id, size))
        if picture is None:
            if (output_id, size) in self._refused_blacks:
                self._refused_blacks.discard((output_id, size))
                return "failed"
            self._owed_blacks.add((output_id, size))
            return "pending"
        return picture.shown if self._finished(picture) else "pending"

    def _keep_blacks(self, output_id: str, sizes: set[tuple[int, int]]) -> None:
        """Retire the Output's black pictures of any size but `sizes` (module rule, The screen's
        ground), freed once no commit shows them; none of another size stays owed."""
        for key in [key for key in self._blacks if key[0] == output_id and key[1] not in sizes]:
            self._retired.append(self._blacks.pop(key))
        for kept in (self._owed_blacks, self._refused_blacks):
            kept.difference_update({key for key in kept if key[0] == output_id and key[1] not in sizes})

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
        """A video's new frame (on the owning thread): recorded as its waiting frame when it takes
        it (`_may_take`); `finish` draws it, never this callback (module rule)."""
        entry = self._layers.get(assignment_id)
        if entry is None or entry.failed or entry.video is None:
            return
        if self._may_take(assignment_id, entry):
            self._requeue(entry, entry.video.latest())

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


__all__ = ["BAND_BYTES", "FENCE_POLL", "STEP_GAP", "VIDEO_TARGETS", "Device", "Fitter", "Patterns", "PlaneRenderer", "Screens",
           "Still", "StillFactory", "Video", "VideoFactory"]
