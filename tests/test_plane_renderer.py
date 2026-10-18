"""The Player's renderer through fakes of its six ports (player/plane_renderer.py): each Frame is
placed by its drawn Position (its rectangle: the binding's, or a Position trial's, whose commits
carry the trial overlay); each picture is finished once for its Frame as it is now and finished
again only when something its pixels depend on changes; a fade is a plane strength, never a draw; a
picture is committed only once the GPU has finished it, a still is finished in steps, at most one
a refresh, and not during a fade it is not needed in; every commit shows the screen's ground, or a
pattern ground with no planes; the commit is made as soon as the last is answered, refused over the
controller's budget and tagged as the screen's grant asks; `capacity` judges a composition at its
busiest moment."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_executor import LIGHTING, binding, layer, media

from contracts.calibration_target import TargetCode
from contracts.frame_fit import Placement, place
from contracts.frame_lighting import AT_FULL, FrameLighting, Paper, WashLight
from contracts.models import Calibration, RectangleFit
from contracts.node_frame import neutral_operations
from contracts.node_picture import DisplayLight
from contracts.room_light import FixedColour, FixedEntry, RoomLightsDocument
from contracts.tuning import TestPattern
from player import plane_renderer as plane_renderer_module
from player.calibration_target import target_modules
from player.calibration_trial import AppliedTrial
from player.decode import StillPicture
from player.geometry import BoardDraw, encode_srgb
from player.gpu import LINEAR, NV12, XR24, DmabufFrame, Target
from player.pacing import DEFAULT_REFRESH, Word
from player.plane_renderer import BAND_BYTES, FENCE_POLL, VIDEO_TARGETS, PlaneRenderer
from player.plane_screen import FrameGrant, ScreenPicture
from player.planes import Composition, Screen, plane_strength
from player.rendering import LocalLayer, OutputComposition

OUTPUT = "hdmi1"
GRANT = uuid4()
FRAME = 1 / 60
SIZE = (3840, 2160)


def framed(fit: RectangleFit | None = None, **fields):
    """test_executor's binding, its Position a rectangle (full width, centred, unless `fit`)."""
    return binding(calibration=Calibration(fit=fit or RectangleFit()), **fields)


def trial(fit: RectangleFit, lighting: FrameLighting = LIGHTING, sequence: int = 1) -> AppliedTrial:
    """What player/plane_screen.py `PlaneScreens.trial` answers while a Position trial is drawn."""
    candidate = SimpleNamespace(trial_id=uuid4(), generation=1, sequence=sequence, candidate_sha256="a" * 64,
                                expires_boottime_ms=10**15)
    return AppliedTrial(candidate, Calibration(fit=fit), lighting)


@pytest.fixture(autouse=True)
def corners(monkeypatch):
    """overlay_corners as the renderer calls it: the placement and screen it was given (B3's rule
    is tested with B3; this module owns only which placement it is given)."""
    monkeypatch.setattr(plane_renderer_module, "overlay_corners", lambda placement, screen: (placement, screen))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, frames: int = 1) -> None:
        self.now += frames * FRAME


@dataclass
class FakeScreens:
    """PlaneScreens as the renderer uses it: each commit is presented at once unless `answer` is
    off; `busy` names the pictures the compositor still holds."""
    clock: Clock
    screen_now: Screen | None = Screen(OUTPUT, (3840, 2160), 0)
    admitted: bool | None = True
    operations: object = None
    trial_now: AppliedTrial | None = None
    answer: bool = True
    latency: float = 0.0            # seconds from a commit to its presentation
    busy_handles: set[int] = field(default_factory=set)
    commits: list[tuple[Composition, dict, str, int | None]] = field(default_factory=list)
    grounds: list = field(default_factory=list)          # each commit's ground
    overlays: list = field(default_factory=list)         # each commit's overlay
    trials: list = field(default_factory=list)           # each trial asked: its grant's admission
    forgotten: list[int] = field(default_factory=list)
    imported: int = 0
    log: list[str] = field(default_factory=list)         # the rig's calls, in order (shared)
    _sequence: int = 0
    _committed_at: float = 0.0

    def screen(self, output_id):
        return self.screen_now

    def grant(self, composition):
        if self.admitted is None:
            return None
        b = composition.binding
        return FrameGrant(GRANT, b.generation, b.configuration_revision, b.frame_id, self.admitted)

    def picture(self, output_id):
        return self.operations

    def trial(self, composition, grant):
        self.trials.append(grant.admitted)
        return self.trial_now

    def import_picture(self, target):
        self.imported += 1
        return ScreenPicture(1000 + target.framebuffer, target.size)

    def busy(self, picture):
        return picture.handle in self.busy_handles

    def forget(self, picture):
        self.forgotten.append(picture.handle)

    def present(self, composition, pictures, grant, tag, picture_change, ground=None, overlay=None):
        self.log.append("commit")
        self._sequence += 1
        self._committed_at = self.clock()
        self.commits.append((composition, dict(pictures), tag, picture_change))
        self.grounds.append(ground)
        self.overlays.append(overlay)
        return self._sequence

    def presentation(self, output_id, now):
        if not self._sequence or not self.answer or now < self._committed_at + self.latency:
            return None
        return Word(self._sequence, self._committed_at + self.latency, DEFAULT_REFRESH)


@dataclass
class FakeDevice:
    """The Gpu as the renderer uses it: each fence signals at once unless `hold` (then it waits for
    `signal`)."""
    made: int = 0
    freed: list[int] = field(default_factory=list)
    textures: int = 0
    uploads: list[tuple[int, int]] = field(default_factory=list)    # each upload's rows
    imports: list[int] = field(default_factory=list)
    hold: bool = False
    fences: int = 0
    pending: set[int] = field(default_factory=set)
    forgotten: set[int] = field(default_factory=set)
    log: list[str] = field(default_factory=list)

    def target(self, size, *, opaque):
        self.log.append("target")
        self.made += 1
        return Target(size, self.made, self.made, 100 + self.made, size[0] * 4, XR24, LINEAR)

    def free(self, target):
        self.freed.append(target.framebuffer)

    def texture(self, size):
        self.log.append("texture")
        self.textures += 1
        return 500 + self.textures

    def upload(self, texture, size, stride, pixels, rows):
        self.log.append("upload")
        self.uploads.append(rows)

    def delete_texture(self, texture):
        pass

    def import_frame(self, frame):
        self.log.append("import")
        self.imports.append(frame.serial)
        return 900 + frame.serial

    def forget_frames(self, fds):
        pass

    def fence(self):
        self.log.append("fence")
        self.fences += 1
        if self.hold:
            self.pending.add(self.fences)
        return self.fences

    def signalled(self, fence):
        return fence not in self.pending

    def forget_fence(self, fence):
        self.forgotten.add(fence)

    def signal(self):
        self.pending.clear()


@dataclass
class FakeFitter:
    draws: list = field(default_factory=list)          # (request, target framebuffer)
    blacks: list = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def draw(self, request, target):
        self.log.append("draw")
        self.draws.append((request, target.framebuffer))

    def black(self, target):
        self.log.append("black")
        self.blacks.append(target.framebuffer)


@dataclass
class FakePatterns:
    """The pattern pass as the renderer uses it (B2's PatternPass): each draw's request and target."""
    draws: list = field(default_factory=list)          # (request, target framebuffer)
    log: list[str] = field(default_factory=list)

    def draw(self, request, target):
        self.log.append("pattern")
        self.draws.append((request, target.framebuffer))


@dataclass
class FakeStill:
    size: tuple[int, int] = (1920, 1080)

    def result(self):
        return StillPicture(self.size, self.size[0] * 4, b"", False)

    def close(self):
        pass


@dataclass
class FakeVideo:
    """A video decode: `push` decodes a frame; a seek flushes the frames not taken, as GStreamer's
    flushing seek does."""
    on_frame: object
    frames: list[DmabufFrame] = field(default_factory=list)
    returned: list[int] = field(default_factory=list)
    seeks: list[float] = field(default_factory=list)

    def push(self, serial: int) -> None:
        self.frames.append(DmabufFrame(serial, (1920, 1080), NV12, 0, (0, 0), (1920, 1920), serial, serial / 30))

    def seek(self, position):
        self.seeks.append(position)
        self.frames.clear()
        return True

    def latest(self):
        newest = self.frames[-1] if self.frames else None
        self.frames.clear()
        return newest

    def done(self, frame):
        self.returned.append(frame.serial)

    def buffers(self):
        return ()

    def close(self):
        pass


class Rig:
    def __init__(self, **screens) -> None:
        self.clock = Clock()
        self.screens = FakeScreens(self.clock, **screens)
        self.log: list[str] = []
        self.screens.log = self.log
        self.device, self.fitter, self.patterns = FakeDevice(log=self.log), FakeFitter(log=self.log), FakePatterns(
            log=self.log)
        self.videos: list[FakeVideo] = []
        self.stills: list[Path] = []
        self.renderer = PlaneRenderer(self.screens, self.device, self.fitter, self.patterns, NoLuts(),
                                      stills=self._still, videos=self._video, clock=self.clock)

    def _still(self, path):
        self.stills.append(path)
        return FakeStill()

    def _video(self, path, on_frame):
        video = FakeVideo(on_frame)
        self.videos.append(video)
        return video

    def present(self, *layers: LocalLayer, frame=None, timebase: float | None = 0.0, fallback: bool = False):
        return self.renderer.present(OutputComposition(frame or framed(), layers, fallback, timebase))

    def ready(self, *layers: LocalLayer, frame=None) -> int:
        """Prepare `layers` a refresh at a time, finishing after each, until each is prepared: the
        refreshes it took."""
        frame = frame or framed()
        for refreshes in range(1, 200):
            if all(self.renderer.prepare(local, frame).status == "prepared" for local in layers):
                return refreshes
            self.renderer.finish()
            self.clock.tick()
        raise AssertionError("never prepared")

    def settle(self, *layers: LocalLayer, frame=None, timebase: float | None = 0.0, fallback: bool = False):
        """Present `layers` a refresh at a time, finishing after each, until a present answers
        "presented" over the screen's finished ground."""
        for _ in range(200):
            result = self.present(*layers, frame=frame, timebase=timebase, fallback=fallback)
            self.renderer.finish()
            if result.status == "presented" and self.screens.grounds[-1] is not None:
                return result
            self.clock.tick()
        raise AssertionError("never presented")

    def strengths(self, commit: int = -1) -> list[tuple[str, float]]:
        """Each committed plane's assignment and strength, to 9 places (full strength over black is
        encode_srgb(1), 1 within a float's last bit)."""
        return [(plane.layer.layer.assignment_id, round(plane.strength, 9))
                for plane in self.screens.commits[commit][0].planes]


class NoLuts:
    def lut(self, ref):
        return None

    def generation(self):
        return 0


def photo(key: str, **fields) -> LocalLayer:
    """A JPEG (opaque at full alpha) on hdmi1, its alpha read from the plan at the frame time."""
    jpeg = media(key.encode()).model_copy(update={"media_type": "image/jpeg"})
    return LocalLayer(layer(key, variant=jpeg, **fields), Path(f"/media/{key}.jpg"), 0.0, 1.0)


def video(key: str, **fields) -> LocalLayer:
    return LocalLayer(layer(key, video=True, **fields), Path(f"/media/{key}.mp4"), 0.0, 1.0)


def prepared_video(rig: Rig, clip: LocalLayer) -> FakeVideo:
    """`clip` prepared on the rig's Frame: its decode started, its first frame finished."""
    frame = framed()
    assert rig.renderer.prepare(clip, frame).status == "pending"
    decode = rig.videos[-1]
    decode.push(1)
    rig.ready(clip)
    return decode


def test_a_dissolve_draws_each_still_once_and_commits_the_plane_strengths():
    """60 frame times through a one-second dissolve: two draws, 60 commits whose incoming strength
    is its alpha (over a picture) and whose outgoing photo stays at full."""
    rig = Rig()
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=1.0)
    rig.ready(outgoing, incoming)
    start, times = rig.clock.now, []
    for _ in range(60):
        times.append(rig.clock.now)
        result = rig.present(outgoing, incoming, timebase=100.0 - start)
        assert result.status == "presented"
        rig.clock.tick()
    assert len(rig.fitter.draws) == 2
    assert len(rig.screens.commits) == 60
    for index, (composition, _pictures, _tag, _change) in enumerate(rig.screens.commits):
        alpha = incoming.layer.alpha(100.0 + times[index] - start)
        wanted = [("out", 1.0)] + ([("in", round(plane_strength(alpha, True), 9))] if alpha else [])
        assert rig.strengths(index) == wanted


def test_a_dip_to_black_commits_the_srgb_strength():
    rig = Rig()
    leaving = photo("leaving", start=0, end=101, fade_out=1.0)
    rig.ready(leaving)
    rig.present(leaving, timebase=100.5 - rig.clock.now)
    assert rig.strengths() == [("leaving", pytest.approx(encode_srgb(leaving.layer.alpha(100.5))))]
    assert rig.strengths()[0][1] != pytest.approx(0.5)


def test_a_black_layer_is_one_black_picture_per_output_and_size():
    rig = Rig()
    first = LocalLayer(layer("black1", presentation="black", variant=None, start=0), None, 0.0, 1.0)
    second = LocalLayer(layer("black2", presentation="black", variant=None, start=1), None, 0.0, 1.0)
    rig.ready(first, second)
    assert len(rig.fitter.blacks) == 1 and rig.fitter.draws == []


def test_what_a_still_depends_on_finishes_it_again_and_nothing_else_does():
    lit = framed(lighting=FrameLighting(mat_depth_mm=0, paper=Paper(whiteness=.9, ink_density=2),
                                         lights=(WashLight(light="room", lux=200, reference=AT_FULL),)))
    rig = Rig()
    still = photo("still", start=0)
    frame = lit

    def draws_after(change) -> int:
        before = len(rig.fitter.draws)
        change()
        rig.clock.tick()
        rig.settle(still, frame=frame)
        rig.clock.tick()
        rig.present(still, frame=frame)                 # nothing changed: no draw
        return len(rig.fitter.draws) - before

    assert draws_after(lambda: None) == 1               # the first finish
    assert draws_after(lambda: None) == 0
    assert draws_after(lambda: rig.renderer.set_room_lights(RoomLightsDocument(change=1, lights={
        "room": FixedEntry(colour=FixedColour(kelvin=6500))}))) == 1
    assert draws_after(lambda: rig.renderer.set_room_lights(RoomLightsDocument(change=2, lights={
        "room": FixedEntry(colour=FixedColour(kelvin=3000))}))) == 1
    operations = neutral_operations(OUTPUT, 7, DisplayLight(200, .01))
    assert draws_after(lambda: setattr(rig.screens, "operations", operations)) == 1
    assert rig.screens.commits[-1][3] == 7              # the tag names the change drawn
    frame = lit.model_copy(update={"calibration": Calibration(fit=RectangleFit(width=0.5))})
    assert draws_after(lambda: None) == 1               # a new rectangle size
    assert rig.fitter.draws[-1][0].placement.width == 1920
    assert draws_after(lambda: setattr(rig.screens, "screen_now", Screen(OUTPUT, (2160, 3840), 90))) == 1
    request = rig.fitter.draws[-1][0]
    assert request.turn == 90 and rig.screens.commits[-1][0].screen.turn == 90
    assert rig.screens.commits[0][3] is None            # the stand-in's draw names no change


def test_a_covered_layer_keeps_its_picture():
    rig = Rig()
    under, over = photo("under", start=0), photo("over", start=1)
    frame = framed()
    rig.ready(under, over)
    rig.present(under, over)
    assert rig.strengths() == [("over", 1.0)]
    assert rig.screens.forgotten == [] and rig.device.freed == []
    assert rig.renderer.prepare(under, frame).status == "prepared"
    assert len(rig.fitter.draws) == 2


def test_a_released_layer_is_freed_once_no_commit_shows_it():
    rig = Rig()
    gone, stays = photo("gone", start=0), photo("stays", start=1)
    rig.ready(gone, stays)
    rig.settle(gone)
    shown = rig.screens.commits[-1][1]["gone"]
    rig.renderer.release("gone")
    assert rig.screens.forgotten == []                  # its screen's last commit shows it
    rig.clock.tick()
    rig.screens.busy_handles.add(shown.handle)
    rig.present(stays)
    assert rig.screens.forgotten == []                  # the compositor still holds it
    rig.screens.busy_handles.clear()
    rig.clock.tick()
    rig.present(stays)
    assert rig.screens.forgotten == [shown.handle] and rig.device.freed == [shown.handle - 1000]
    assert rig.screens.grounds[-1] is not None and rig.screens.grounds[-1].handle not in rig.screens.forgotten


def test_a_video_frame_is_drawn_into_a_released_target():
    rig = Rig()
    clip = video("clip")
    prepared_video(rig, clip)
    assert rig.present(clip).status == "presented"
    first = rig.screens.commits[-1][1]["clip"]
    rig.videos[0].push(2)
    rig.videos[0].on_frame()
    assert rig.device.imports == [1]                    # the callback only records it
    rig.renderer.finish()
    assert rig.fitter.draws[-1][1] != first.handle - 1000      # not the shown one
    assert rig.fitter.draws[-1][0].source.external and rig.device.imports == [1, 2]
    rig.clock.tick()
    rig.present(clip)
    assert rig.screens.commits[-1][1]["clip"] != first
    assert rig.videos[0].returned == [1]                # the older frame went back once replaced


def test_a_busy_target_is_never_drawn_into():
    rig = Rig()
    clip = video("clip")
    prepared_video(rig, clip)
    rig.settle(clip)                                    # target 1 shown (target 2: the ground)
    for serial in (2, 3, 4):
        if serial == 3:
            rig.screens.busy_handles.add(1003)
        if serial == 4:
            rig.screens.busy_handles.add(1004)
        rig.videos[0].push(serial)
        rig.videos[0].on_frame()
        rig.renderer.finish()                           # 2 into 3; then 1 shown, 3 finished: 4;
    assert [target for _, target in rig.fitter.draws] == [1, 3, 4]     # then 1 shown, 3 busy, 4 finished
    assert len(rig.fitter.draws) == VIDEO_TARGETS
    assert len(rig.fitter.draws) == VIDEO_TARGETS


def half(key: str, start: float, **fields) -> LocalLayer:
    """A full-width photo at half strength from plan `start` (it covers nothing beneath it)."""
    return photo(key, start=start, media_origin=start, opacity=0.5, **fields)


def test_a_composition_is_judged_at_its_busiest_moment():
    """A dissolve starting in 2 s counts both its planes: three full-width 4K pictures then fit the
    Pi 5's line (the screen's ground and three pictures, 15,360 pixels), four do not, though each
    shows one picture fewer now."""
    rig = Rig()
    incoming = half("incoming", 102, fade_in=1.0)

    def available(*below: LocalLayer) -> bool:
        composition = OutputComposition(framed(), (*below, incoming), timebase=100.0)
        return rig.renderer.capacity((composition,)).available

    assert available(half("a", 90), half("b", 90))
    assert not available(half("a", 90), half("b", 90), half("c", 90))
    assert rig.renderer.capacity((OutputComposition(framed(), (half("a", 90), half("b", 90), half("c", 90)),
                                                    timebase=100.0),)).available      # no fade ahead
    rig.screens.screen_now = None                       # an Output not there counts nothing
    assert available(half("a", 90), half("b", 90), half("c", 90))


def test_a_present_over_the_budget_fails_capacity_and_commits_nothing():
    """Who gives up a layer is the executor's (player/visibility.py `shed`): the renderer refuses."""
    rig = Rig()
    layers = [half(f"p{index}", index) for index in range(4)]
    rig.ready(*layers)
    result = rig.present(*layers)
    assert (result.status, result.code) == ("failed", "capacity")
    assert rig.screens.commits == []
    assert rig.renderer.present(OutputComposition(framed(), tuple(layers[1:]), timebase=0.0)).status == "presented"


def test_a_rectangle_position_places_its_plane_and_is_reported_without_a_trial():
    rig = Rig()
    still = photo("still", start=0)
    bound = framed(RectangleFit(centre_x=0.5, centre_y=0.5, width=0.5))
    result = rig.settle(still, frame=bound)
    assert rig.screens.commits[-1][0].planes[0].at == Placement(960, 540, 1920, 1080)
    assert result.applied_calibration.calibration == bound.calibration
    assert result.applied_calibration.trial_id is None
    assert rig.screens.overlays[-1] is None


def test_a_trial_moves_the_plane_carries_its_overlay_and_is_reported():
    rig = Rig()
    still = photo("still", start=0)
    rig.settle(still)
    assert rig.screens.commits[-1][0].planes[0].at == Placement(0, 0, *SIZE)
    tried = trial(RectangleFit(centre_x=0.25, centre_y=0.5, width=0.5))
    rig.screens.trial_now = tried
    rig.clock.tick()
    result = rig.settle(still)
    moved = place(tried.calibration.fit.rectangle(), SIZE, 1920 / 1080)
    assert moved == Placement(0, 540, 1920, 1080)
    assert rig.screens.commits[-1][0].planes[0].at == moved
    assert rig.screens.overlays[-1] == (moved, SIZE)
    applied = result.applied_calibration
    assert applied.calibration == tried.calibration and applied.trial_id == str(tried.candidate.trial_id)
    assert (applied.trial_generation, applied.trial_sequence, applied.candidate_sha256) == (1, 1, "a" * 64)
    assert result.composition.binding == framed()       # what plays is unchanged
    rig.screens.trial_now = None                        # the trial ends: the binding's again
    rig.clock.tick()
    result = rig.settle(still)
    assert rig.screens.commits[-1][0].planes[0].at == Placement(0, 0, *SIZE)
    assert rig.screens.overlays[-1] is None and result.applied_calibration.trial_id is None


def test_a_trials_lighting_finishes_the_still_again():
    rig = Rig()
    still = photo("still", start=0)
    rig.settle(still)
    lit = FrameLighting(mat_depth_mm=0, paper=Paper(whiteness=.8, ink_density=2),
                        lights=(WashLight(light="room", lux=300, reference=AT_FULL),))
    rig.screens.trial_now = trial(RectangleFit(), lighting=lit)
    rig.clock.tick()
    rig.settle(still)
    assert len(rig.fitter.draws) == 2
    assert rig.fitter.draws[1][0].lights != rig.fitter.draws[0][0].lights
    assert rig.fitter.draws[1][0].lights.paper == lit.paper


def test_a_test_pattern_is_the_screens_ground_drawn_once_per_request():
    rig = Rig()
    still = photo("still", start=0)
    white = replace(neutral_operations(OUTPUT, 1, DisplayLight(200, .01)), test_pattern=TestPattern.WHITE)
    rig.screens.operations = white
    result = rig.settle(still)
    composition, pictures, _tag, change = rig.screens.commits[-1]
    assert composition.planes == () and pictures == {} and change == 1
    assert len(rig.patterns.draws) == 1 and rig.fitter.draws == []
    request, target = rig.patterns.draws[0]
    assert (request.operations, request.size, request.placement, request.board) == (
        white, SIZE, Placement(0, 0, *SIZE), None)
    assert rig.screens.grounds[-1].handle == 1000 + target           # the pattern's picture, not the black
    assert [local.layer.assignment_id for local in result.composition.layers] == ["still"]
    rig.clock.tick()
    rig.present(still)
    assert len(rig.patterns.draws) == 1 and len(rig.screens.commits) == 1      # nothing changed
    rig.screens.operations = replace(white, picture_change=2)
    rig.clock.tick()
    rig.settle(still)
    assert len(rig.patterns.draws) == 2 and rig.screens.commits[-1][3] == 2
    rig.screens.operations = None                       # the pattern ends: the pictures again
    rig.clock.tick()
    rig.settle(still)
    assert [key for key, _ in rig.strengths()] == ["still"]


def test_a_pattern_is_pending_until_the_gpu_finished_it():
    rig = Rig()
    still = photo("still", start=0)
    rig.settle(still)
    rig.device.hold = True
    rig.screens.operations = replace(neutral_operations(OUTPUT, 1, DisplayLight(200, .01)),
                                     test_pattern=TestPattern.GREY_RAMP)
    rig.clock.tick()
    assert rig.present(still).status == "pending" and rig.renderer.finishing      # owed to finish
    rig.renderer.finish()
    rig.clock.tick()
    assert rig.present(still).status == "pending" and rig.renderer.finishing      # drawn, fence pending
    commits = len(rig.screens.commits)
    rig.device.signal()
    rig.clock.tick()
    assert rig.present(still).status == "presented" and rig.screens.commits[-1][0].planes == ()
    assert len(rig.screens.commits) == commits + 1


def test_a_trial_over_a_fallback_with_nothing_playing_draws_the_commissioning_grid():
    rig = Rig()
    rig.screens.trial_now = trial(RectangleFit(width=0.5))
    result = rig.settle(fallback=True, timebase=None)
    assert result.status == "presented" and rig.screens.commits[-1][0].planes == ()
    request = rig.patterns.draws[-1][0]
    assert request.operations.test_pattern is None and request.placement == Placement(960, 540, 1920, 1080)
    assert rig.screens.overlays[-1] == (request.placement, SIZE)
    rig.screens.trial_now = None                        # no trial: the fallback's black ground
    rig.clock.tick()
    rig.present(fallback=True, timebase=None)
    assert len(rig.patterns.draws) == 1 and rig.screens.grounds[-1].handle != rig.screens.grounds[-2].handle


def test_a_calibration_targets_qr_names_the_player_once_it_has_an_id(monkeypatch):
    boards = []

    def board(profile, modules):
        boards.append((profile, modules))
        return BoardDraw(16 / 9, False, modules)

    monkeypatch.setattr(plane_renderer_module, "picture_board", board)
    rig = Rig()
    rig.screens.operations = replace(neutral_operations(OUTPUT, 3, DisplayLight(200, .01)),
                                     test_pattern=TestPattern.TARGET_POST_SOFTWARE)
    rig.settle()
    assert rig.patterns.draws[-1][0].board.modules is None
    rig.renderer.set_player("p")
    rig.clock.tick()
    rig.settle()
    assert len(rig.patterns.draws) == 2
    assert rig.patterns.draws[-1][0].board.modules == target_modules(TargetCode("p", OUTPUT, 3))
    assert boards[-1][0] == framed().profile
    rig.renderer.set_player("p")
    rig.clock.tick()
    rig.present()
    assert len(rig.patterns.draws) == 2


def test_capacity_counts_no_planes_for_a_pattern():
    rig = Rig()
    rig.screens.operations = replace(neutral_operations(OUTPUT, 1, DisplayLight(200, .01)),
                                     test_pattern=TestPattern.WHITE)
    layers = tuple(half(f"p{index}", 90) for index in range(5))
    assert rig.renderer.capacity((OutputComposition(framed(), layers, timebase=100.0),)).available


def test_no_grant_is_pending_and_commits_nothing():
    rig = Rig(admitted=None)
    assert rig.present(photo("p", start=0)).status == "pending"
    assert rig.screens.commits == [] and rig.fitter.draws == []


def test_a_candidate_grant_commits_the_screens_black_again_on_each_present_the_pacer_allows():
    """The shell completes a handoff only on a presented commit after its release, which the app
    cannot see: the candidate's black is committed again whenever the last one was answered, and
    never while it is in flight."""
    rig = Rig(admitted=False, answer=False)
    assert rig.present(photo("p", start=0)).status == "pending"
    rig.clock.tick()
    assert rig.present(photo("p", start=0)).status == "pending"
    assert len(rig.screens.commits) == 1                # in flight: nothing more
    rig.screens.answer = True
    for _ in range(2):
        rig.clock.tick()
        assert rig.present(photo("p", start=0)).status == "pending"
    assert [(commit[0].planes, commit[2], commit[3]) for commit in rig.screens.commits] == [
        ((), f"candidate-{GRANT}", None)] * 3
    assert rig.fitter.draws == []


def test_a_held_still_whose_play_position_moves_commits_once():
    """Each planning tick hands the layer at its new play position (now - media_origin): the
    picture, its placement and strength are the same, so nothing is committed again."""
    rig = Rig()
    still = photo("still", start=0)
    rig.ready(still)
    for tick in range(30):
        rig.present(replace(still, position=tick * 0.043))
        rig.clock.tick()
    assert len(rig.screens.commits) == 1 and len(rig.fitter.draws) == 1


def test_a_picture_finished_again_into_the_same_buffer_commits_again():
    """A picture's finish counts, not only its buffer: a still finished again whose new target
    the compositor gives the old handle (a freed buffer's id reused) is a new commit."""
    rig = Rig()
    still = photo("still", start=0)
    rig.settle(still)
    rig.screens.import_picture = lambda target: ScreenPicture(4242, target.size)
    rig.clock.tick()
    rig.present(still)                                  # same key: no draw, no commit
    rig.screens.operations = neutral_operations(OUTPUT, 3, DisplayLight(200, .01))
    rig.clock.tick()
    rig.settle(still)
    rig.screens.operations = neutral_operations(OUTPUT, 3, DisplayLight(300, .01))
    rig.clock.tick()
    rig.settle(still)
    assert [commit[1]["still"].handle for commit in rig.screens.commits][1:] == [4242, 4242]
    assert len(rig.screens.commits) == 3


def test_a_prepared_video_finishes_its_first_frame_once_and_starts_when_first_presented():
    """During its lead a video is ready to start: its first frame finished once, its later
    frames left in the decoder (no draw on their callbacks or on a prepare). Its first present
    plays it from the layer's position; from then on each frame is finished while it is seen."""
    rig = Rig()
    clip = video("clip", start=100, media_origin=100)
    decode = prepared_video(rig, clip)
    assert decode.seeks == [0.0] and len(rig.fitter.draws) == 1
    for serial in range(2, 30):                         # the lead: frames arrive, nothing is finished
        decode.push(serial)
        decode.on_frame()
        assert rig.renderer.prepare(clip, framed()).status == "prepared"
        rig.renderer.finish()
    assert len(rig.fitter.draws) == 1 and rig.device.imports == [1]
    started = replace(clip, position=0.02)
    assert rig.present(started).status == "presented"
    assert decode.seeks == [0.0, 0.02]                  # started from where the layer is
    assert rig.screens.commits[-1][1]["clip"].handle == 1001       # the prepared frame meanwhile
    decode.push(30)
    decode.on_frame()
    rig.renderer.finish()
    assert rig.device.imports == [1, 30]


def test_a_video_no_commit_shows_finishes_no_frame():
    rig = Rig()
    clip = video("clip", start=0)
    decode = prepared_video(rig, clip)
    rig.present(clip)
    cover = photo("cover", start=1)
    rig.ready(cover)
    rig.clock.tick()
    rig.present(clip, cover)                            # covered: not in the commit
    assert [key for key, _ in rig.strengths()] == ["cover"]
    decode.push(2)
    decode.on_frame()
    rig.renderer.finish()
    assert rig.device.imports == [1]


def test_while_a_commit_is_in_flight_a_present_commits_nothing():
    rig = Rig(answer=False)
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=1.0)
    rig.ready(outgoing, incoming)
    first = rig.present(outgoing, incoming, timebase=100.2)
    rig.clock.tick()
    second = rig.present(outgoing, incoming, timebase=100.2)
    assert len(rig.screens.commits) == 1 and second == first
    rig.screens.answer = True
    rig.clock.tick()
    assert rig.present(outgoing, incoming, timebase=100.2).presented_at == rig.clock.now
    assert len(rig.screens.commits) == 2


def test_an_output_gone_or_a_refused_rectangle_fails_the_present():
    rig = Rig(screen_now=None)
    assert (rig.present(photo("p", start=0)).status, rig.present(photo("p", start=0)).code) == ("failed", "decode")
    rig = Rig()
    off = framed(RectangleFit(centre_x=0.9))
    assert rig.present(photo("p", start=0), frame=off).code == "decode"


def test_presented_carries_the_drawn_composition_and_the_witness_tag():
    rig = Rig()
    still = photo("still", start=0)
    rig.ready(still)
    result = rig.present(still)
    assert result.presented_at == rig.clock.now and result.composition.layers[0].layer == still.layer
    assert rig.screens.commits[-1][2] != f"candidate-{GRANT}"
    assert result.composition.layers[0] == replace(still, alpha=1.0)


def test_every_commit_shows_the_screens_ground_laid_out_for_the_whole_screen():
    """Beneath the planes the screen shows the Output's black picture of the whole screen as it is
    laid out (turned with the screen), so the display controller can scan out what the planes leave
    uncovered: the candidate's commit and each admitted one alike, one picture shared with a black
    layer of the screen's size."""
    rig = Rig(admitted=False)
    still = photo("still", start=0)
    rig.ready(still)
    rig.present(still)                                  # the ground owed: the compositor's black
    assert rig.screens.grounds[-1] is None
    rig.renderer.finish()
    rig.clock.tick()
    rig.present(still)
    candidate = rig.screens.grounds[-1]
    assert candidate is not None and candidate.size == (3840, 2160)
    rig.screens.admitted = True
    rig.clock.tick()
    rig.present(still)
    assert rig.screens.grounds[-1] == candidate
    assert len(rig.fitter.blacks) == 1
    black = LocalLayer(layer("black", presentation="black", variant=None, start=0), None, 0.0, 1.0)
    assert rig.renderer.prepare(black, framed()).status == "prepared"
    assert len(rig.fitter.blacks) == 1                  # the ground's own picture
    rig.screens.screen_now = Screen(OUTPUT, (2160, 3840), 90)
    rig.clock.tick()
    rig.settle(still)
    assert rig.screens.grounds[-1] == candidate         # laid out turned: the mode's own size


def test_a_commit_is_made_as_soon_as_the_last_is_answered_even_after_a_late_one():
    """A commit draws nothing, so a late answer (here 1.2 refreshes) does not push the next commit
    to a later refresh: the next present after the answer commits."""
    rig = Rig(latency=1.2 * FRAME)
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=1.0)
    rig.ready(outgoing, incoming)
    timebase = 100.2 - rig.clock.now
    rig.present(outgoing, incoming, timebase=timebase)
    rig.clock.now += 1.25 * FRAME                       # just after the answer
    rig.present(outgoing, incoming, timebase=timebase)
    assert len(rig.screens.commits) == 2


def test_a_picture_is_committed_only_once_the_gpu_has_finished_it():
    """A still's draw is fenced: until its fence signals the still is not prepared and nothing
    shows it; a video's newer frame meanwhile leaves its finished one in the commits, so a fade
    goes on."""
    rig = Rig()
    rig.device.hold = True
    still = photo("still", start=0)
    frame = framed()
    for _ in range(20):
        assert rig.renderer.prepare(still, frame).status == "pending"
        rig.renderer.finish()
        rig.clock.tick()
    assert len(rig.fitter.draws) == 1 and rig.renderer.finishing
    rig.device.signal()
    assert rig.renderer.prepare(still, frame).status == "prepared"
    rig.device.hold = False
    clip = video("clip", start=0, end=200)
    over = photo("over", start=0, end=200, fade_in=10.0)
    decode = prepared_video(rig, clip)
    rig.ready(over)
    rig.settle(clip)
    first = rig.screens.commits[-1][1]["clip"]
    rig.device.hold = True
    decode.push(2)
    decode.on_frame()
    rig.renderer.finish()                               # drawn, not finished
    for _ in range(3):
        rig.clock.tick()
        assert rig.present(clip, over, timebase=1.0).status == "presented"
        assert rig.screens.commits[-1][1]["clip"] == first
    commits = len(rig.screens.commits)
    rig.device.signal()
    rig.clock.tick()
    rig.present(clip, over, timebase=1.0)
    assert len(rig.screens.commits) == commits + 1 and rig.screens.commits[-1][1]["clip"] != first


def test_a_still_is_finished_in_steps_at_most_one_a_refresh():
    """A 4K still: its decode taken, its rows uploaded BAND_BYTES at a time, its draw: one step a
    refresh over every layer, however often prepare is asked."""
    rig = Rig()
    rig.renderer = PlaneRenderer(rig.screens, rig.device, rig.fitter, rig.patterns, NoLuts(),
                                 stills=lambda path: FakeStill((3840, 2160)), videos=rig._video, clock=rig.clock)
    first, second = photo("first", start=0), photo("second", start=1)
    frame = framed()
    bands = -(-3840 * 4 * 2160 // BAND_BYTES)
    steps = []
    for _ in range(2 * (bands + 2) + 2):
        for _ in range(3):                              # asked and finished three times a refresh
            rig.renderer.prepare(first, frame)
            rig.renderer.prepare(second, frame)
            rig.renderer.finish()
        steps.append(len(rig.device.uploads) + len(rig.fitter.draws))
        rig.clock.tick()
    assert all(after - before <= 1 for before, after in zip([0, *steps], steps, strict=False))
    assert len(rig.device.uploads) == 2 * bands
    assert rig.device.uploads[:bands] == [(row, min(BAND_BYTES // (3840 * 4), 2160 - row))
                                          for row in range(0, 2160, BAND_BYTES // (3840 * 4))]
    assert rig.renderer.prepare(first, frame).status == "prepared"
    assert rig.renderer.prepare(second, frame).status == "prepared"


def test_a_still_not_needed_before_a_fade_under_way_ends_is_not_prepared_during_it():
    """While a dissolve runs (plan 100 to 103), a still starting after it is neither decoded nor
    finished; one starting within it is; after the fade the later one is prepared."""
    rig = Rig()
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=3.0)
    rig.ready(outgoing, incoming)
    timebase = 100.1 - rig.clock.now
    later, within_fade = photo("later", start=104), photo("within", start=102)
    frame = framed()
    decoded = len(rig.stills)
    for _ in range(30):                                 # half a second into the fade
        rig.present(outgoing, incoming, timebase=timebase)
        assert rig.renderer.prepare(later, frame).status == "pending"
        rig.renderer.prepare(within_fade, frame)
        rig.renderer.finish()
        rig.clock.tick()
    assert rig.stills[decoded:] == [Path("/media/within.jpg")]
    assert rig.renderer.prepare(within_fade, frame).status == "prepared"
    rig.clock.now += 3.0                                # the fade is over
    rig.present(outgoing, incoming, timebase=timebase)
    rig.ready(later)


GPU_CALLS = {"target", "texture", "upload", "import", "fence", "draw", "black", "pattern"}


def test_a_committing_present_does_no_gpu_work_and_the_next_finish_draws_the_waiting_frame_after_it():
    """Finishing is off the present path (s3-page decision 4, tracer 3): a video's new frame is only
    recorded by its decoder's callback; the present commits what is finished, and the frame is
    drawn by the finish after that commit."""
    rig = Rig()
    clip = video("clip", start=0, end=200, fade_in=10.0)
    decode = prepared_video(rig, clip)
    rig.settle(clip, timebase=1.0)
    rig.renderer.finish()
    decode.push(2)
    decode.on_frame()
    rig.log.clear()
    rig.clock.tick()
    assert rig.present(clip, timebase=1.0).status == "presented"      # a new strength: a commit
    assert rig.log == ["commit"]
    rig.renderer.finish()
    assert rig.log[0] == "commit" and rig.log[1:] == ["target", "import", "draw", "fence"]
    assert rig.device.imports[-1] == 2 and rig.fitter.draws[-1][0].source.external


def test_prepare_takes_no_step_and_finish_at_most_one_a_refresh():
    rig = Rig()
    first, second = photo("first", start=0), photo("second", start=1)
    frame = framed()
    for _ in range(10):
        assert rig.renderer.prepare(first, frame).status == "pending"
        assert rig.renderer.prepare(second, frame).status == "pending"
        rig.clock.tick()
    assert [call for call in rig.log if call in GPU_CALLS] == []
    steps = []
    for _ in range(20):
        for _ in range(3):                              # finished three times a refresh
            rig.renderer.finish()
        steps.append(len(rig.device.uploads) + len(rig.fitter.draws))
        rig.clock.tick()
    assert all(after - before <= 1 for before, after in zip([0, *steps], steps, strict=False))
    assert rig.renderer.prepare(first, frame).status == "prepared"
    assert rig.renderer.prepare(second, frame).status == "prepared"


def test_a_dissolve_held_by_one_present_is_committed_by_frame_at_every_frame_time():
    rig = Rig()
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=1.0)
    rig.ready(outgoing, incoming)
    start = rig.clock.now + 1.0                         # plan 100: the dissolve starts a second ahead
    rig.settle(outgoing, incoming, timebase=100.0 - start)      # held; its ground finished
    commits, times = len(rig.screens.commits), []
    for index in range(1, 61):
        rig.clock.now = start + (index - 0.5) * FRAME   # inside the dissolve: 60 strengths
        times.append(rig.clock.now)
        rig.renderer.frame()
        rig.renderer.finish()
    assert len(rig.screens.commits) == commits + 60 and len(rig.fitter.draws) == 2
    for index, at in enumerate(times):
        alpha = incoming.layer.alpha(100.0 + at - start)
        wanted = [("out", 1.0)] + ([("in", round(plane_strength(alpha, True), 9))] if alpha else [])
        assert rig.strengths(commits + index) == wanted


def test_due_asks_a_pending_fence_again_soon_never_at_once():
    """A turn never spins at FRAME priority while the GPU works: a pending fence is asked again
    FENCE_POLL later, so the planning tick and the decoders' callbacks run in between."""
    rig = Rig()
    rig.device.hold = True
    still = photo("still", start=0)
    for _ in range(10):
        rig.renderer.prepare(still, framed())
        rig.renderer.finish()
        rig.clock.tick()
    assert len(rig.fitter.draws) == 1
    assert rig.renderer.due() == pytest.approx(rig.clock.now + FENCE_POLL)
    rig.device.signal()
    rig.renderer.finish()                               # its draw taken up with nothing asking again
    assert rig.renderer.due() is None


def test_a_video_that_fails_while_its_draw_is_fenced_leaves_nothing_owed():
    """A decode error while a frame's draw is pending: the draw is given up, its frame goes back to
    the decoder, and the frame loop is not kept turning (render planes end review, finding 1)."""
    rig = Rig()
    clip = video("clip", start=0)
    decode = prepared_video(rig, clip)
    rig.settle(clip)
    rig.device.hold = True
    decode.push(2)
    decode.on_frame()
    rig.renderer.frame()
    rig.renderer.finish()                               # frame 2 drawn, its fence pending

    def refused():
        raise RuntimeError("decode")
    decode.latest = refused
    rig.renderer.finish()                               # the decode fails with the draw pending
    rig.renderer.finish()
    assert 2 in decode.returned
    assert rig.renderer.due() is None


def test_a_black_picture_nobody_asks_for_again_is_asked_by_finish_and_retired_by_a_present():
    """A trial's edit changes the black layer's size: the old black's fence is asked by `finish`
    (nothing else asks for that size again) and the old black is retired, so neither the frame loop
    nor the black targets grow with the edits."""
    rig = Rig()
    dark = LocalLayer(layer("dark", presentation="black", variant=None, start=0), None, 0.0, 1.0)
    rig.settle(dark)
    made = rig.device.made
    rig.device.hold = True
    for width in (0.9, 0.8, 0.7, 0.6):
        frame = framed(RectangleFit(width=width))
        rig.clock.tick()
        rig.present(dark, frame=frame)
        rig.renderer.finish()                           # the new size's black drawn, fence pending
    rig.device.signal()
    rig.renderer.finish()
    assert rig.renderer.due() is None
    assert rig.device.made - made == 4
    rig.clock.tick()
    assert rig.present(dark, frame=frame).status == "presented"
    assert len(rig.renderer._blacks) == 2               # the ground's and the newest size's
    assert len(rig.device.freed) == 3                   # the three sizes the edits left behind


def test_due_is_now_while_a_video_frame_waits():
    rig = Rig()
    clip = video("clip", start=0)
    decode = prepared_video(rig, clip)
    rig.settle(clip)
    rig.renderer.frame()
    rig.renderer.finish()
    assert rig.renderer.due() is None
    decode.push(2)
    decode.on_frame()
    assert rig.renderer.due() == rig.clock.now


def test_due_is_a_held_compositions_next_fade_and_none_for_a_still():
    rig = Rig()
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=102, fade_in=1.0)
    rig.ready(outgoing, incoming)
    start = rig.clock.now
    rig.settle(outgoing, incoming, timebase=100.0 - start)
    rig.renderer.frame()
    assert rig.renderer.due() == pytest.approx(start + 2.0)
    rig.settle(outgoing, timebase=None)
    rig.renderer.frame()
    assert rig.renderer.due() is None


def test_a_composition_whose_layers_are_all_released_is_no_longer_held():
    rig = Rig()
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=1.0)
    rig.ready(outgoing, incoming)
    rig.settle(outgoing, incoming, timebase=100.2 - rig.clock.now)
    rig.renderer.release("out")
    rig.clock.tick()
    rig.renderer.frame()                                # still held: the incoming layer fades on
    assert [key for key, _ in rig.strengths()] == ["in"]
    rig.renderer.release("in")
    commits = len(rig.screens.commits)
    rig.clock.tick()
    rig.renderer.frame()
    assert len(rig.screens.commits) == commits and rig.renderer.due() is None
