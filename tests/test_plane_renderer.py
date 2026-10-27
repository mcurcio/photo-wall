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
from test_executor import Rig as PlanRig

from contracts.calibration_target import TargetCode
from contracts.frame_fit import Placement, place_box
from contracts.frame_lighting import AT_FULL, FarLight, FrameLighting, Paper, WashLight
from contracts.models import Calibration, PositionBox
from contracts.node_frame import neutral_operations
from contracts.node_picture import DisplayLight
from contracts.photo_crop import CENTRED
from contracts.room_light import FixedEntry, KelvinColour, RoomLightsDocument
from contracts.tuning import TestPattern
from player import geometry as geometry_module
from player import plane_renderer as plane_renderer_module
from player.calibration_target import target_modules
from player.calibration_trial import AppliedTrial
from player.decode import StillPicture, StillRegion
from player.geometry import BoardDraw, encode_srgb
from player.gpu import LINEAR, NV12, XR24, DmabufFrame, Target
from player.pacing import DEFAULT_REFRESH, Word
from player.plane_renderer import BAND_BYTES, FENCE_POLL, VIDEO_TARGETS, PlaneRenderer
from player.plane_screen import FrameGrant, ScreenPicture
from player.planes import DARK_KEY, MIN_STRENGTH, Composition, Screen, plane_strength, still_region
from player.rendering import LocalLayer, OutputComposition

OUTPUT = "hdmi1"
GRANT = uuid4()
FRAME = 1 / 60
SIZE = (3840, 2160)


def centred(width: float, height: float | None = None) -> PositionBox:
    """A box `width` of the screen wide and `height` (default `width`) high, centred."""
    height = width if height is None else height
    return PositionBox(left=(1 - width) / 2, top=(1 - height) / 2, width=width, height=height)


def framed(fit: PositionBox | None = None, **fields):
    """test_executor's binding (no Display size), its Position a box (the whole screen unless `fit`)."""
    return binding(calibration=Calibration(fit=fit or PositionBox()), **fields)


def trial(fit: PositionBox, lighting: FrameLighting = LIGHTING, sequence: int = 1) -> AppliedTrial:
    """What player/plane_screen.py `PlaneScreens.trial` answers while a Position trial is drawn."""
    candidate = SimpleNamespace(trial_id=uuid4(), generation=1, sequence=sequence, candidate_sha256="a" * 64,
                                frame_tag="trial-" + "a" * 64, expires_boottime_ms=10**15)
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
    sizes: list[tuple[int, int]] = field(default_factory=list)      # each texture's size
    deleted: list[int] = field(default_factory=list)                # each texture deleted
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
        self.sizes.append(size)
        return 500 + self.textures

    def upload(self, texture, size, stride, pixels, rows):
        self.log.append("upload")
        self.uploads.append(rows)

    def delete_texture(self, texture):
        self.deleted.append(texture)

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
    """A still's decode of its region: done at once unless `held` (then "pending" until let go)."""
    size: tuple[int, int] = (1920, 1080)
    held: bool = False
    closed: bool = False

    def result(self):
        return "pending" if self.held else StillPicture(self.size, self.size[0] * 4, b"", False)

    def close(self):
        self.closed = True


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
    def __init__(self, clock=None, **screens) -> None:
        self.clock = clock or Clock()
        self.screens = FakeScreens(self.clock, **screens)
        self.log: list[str] = []
        self.screens.log = self.log
        self.device, self.fitter, self.patterns = FakeDevice(log=self.log), FakeFitter(log=self.log), FakePatterns(
            log=self.log)
        self.videos: list[FakeVideo] = []
        self.stills: list[Path] = []
        self.regions: list[StillRegion] = []
        self.decodes: list[FakeStill] = []
        self.holding: set[str] = set()          # the stills whose decode is held until let go
        self.refusing: set[str] = set()         # the stills whose decode cannot be started
        self.renderer = PlaneRenderer(self.screens, self.device, self.fitter, self.patterns, NoLuts(),
                                      stills=self._still, videos=self._video, clock=self.clock)

    def _still(self, path, region):
        self.stills.append(path)
        self.regions.append(region)
        if path.stem in self.refusing:
            raise RuntimeError("decode_refused")
        self.decodes.append(FakeStill(region.size, held=path.stem in self.holding))
        return self.decodes[-1]

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
        return [(plane.key, round(plane.strength, 9))
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


def dissolved(alpha: float) -> list[tuple[str, float]]:
    """The planes committed while "in" dissolves over "out" at `alpha` (player/planes.py, A shown
    picture keeps its plane): "out" alone before; then "in" at full beneath "out" at 1 - alpha
    (over a picture), "out" removed once under MIN_STRENGTH."""
    if not alpha:
        return [("out", 1.0)]
    rest = round(1 - plane_strength(alpha, True), 9)
    return [("in", 1.0)] + ([("out", rest)] if rest >= MIN_STRENGTH else [])


def test_a_dissolve_draws_each_still_once_and_commits_the_plane_strengths():
    """60 frame times through a one-second dissolve: two draws, 60 commits with the incoming photo
    at full beneath and the outgoing one, which keeps its overlay, above it at 1 - the incoming's
    alpha (over a picture)."""
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
        assert rig.strengths(index) == dissolved(alpha)


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
        "room": FixedEntry(colour=KelvinColour(kelvin=6500))}))) == 1
    assert draws_after(lambda: rig.renderer.set_room_lights(RoomLightsDocument(change=2, lights={
        "room": FixedEntry(colour=KelvinColour(kelvin=3000))}))) == 1
    operations = neutral_operations(OUTPUT, 7, DisplayLight(200, .01))
    assert draws_after(lambda: setattr(rig.screens, "operations", operations)) == 1
    assert rig.screens.commits[-1][3] == 7              # the tag names the change drawn
    frame = lit.model_copy(update={"calibration": Calibration(fit=centred(0.5))})
    assert draws_after(lambda: None) == 0               # a new box, the same finish size (1080p)
    frame = lit.model_copy(update={"calibration": Calibration(fit=centred(0.25))})
    assert draws_after(lambda: None) == 1               # a smaller placement than the source
    assert rig.fitter.draws[-1][0].size == (960, 540)
    assert draws_after(lambda: setattr(rig.screens, "screen_now", Screen(OUTPUT, (2160, 3840), 90))) == 1
    request = rig.fitter.draws[-1][0]
    assert request.turn == 90 and rig.screens.commits[-1][0].screen.turn == 90
    assert rig.screens.commits[0][3] is None            # the stand-in's draw names no change


def test_a_frames_lights_are_worked_out_only_when_what_they_read_changes(monkeypatch):
    """The Frame's lights and peak (player/geometry.py `picture_lights`, a 33 x 33 grid in Python)
    are worked out once for what they read: the Frame's lighting, its room lights' entries, its
    physical size and the Display's light; ticks and presents with all four unchanged reuse them,
    a change to any one works them out again and the next finish draws with it. Mutation probe:
    drop the memo on `picture_lights` -> every prepare and present counts one more."""
    worked = []
    brightest = geometry_module._brightest_white
    monkeypatch.setattr(geometry_module, "_brightest_white", lambda *a: worked.append(a) or brightest(*a))
    lighting = FrameLighting(mat_depth_mm=3, paper=Paper(whiteness=.91, ink_density=2.1), lights=(
        WashLight(light="memo-wash", lux=123.25, reference=AT_FULL),
        FarLight(light="memo-far", lux=321.5, reference=AT_FULL, from_degrees=30, steep_degrees=40,
                 softness_degrees=5)))
    rig = Rig()
    rig.renderer.set_room_lights(RoomLightsDocument(change=1, lights={
        "memo-wash": FixedEntry(colour=KelvinColour(kelvin=4000)),
        "memo-far": FixedEntry(colour=KelvinColour(kelvin=5000))}))
    still = photo("still", start=0)
    frame = framed(lighting=lighting, display_mm=(950, 540))

    def turns() -> list:
        """A few ticks: prepare, present and finish with nothing changed; the lights drawn."""
        for _ in range(5):
            rig.renderer.prepare(still, frame)
            rig.settle(still, frame=frame)
            rig.clock.tick()
        return rig.fitter.draws[-1][0].lights

    first = turns()
    assert len(worked) == 1
    assert turns() == first and len(worked) == 1

    rig.renderer.set_room_lights(RoomLightsDocument(change=2, lights={
        "memo-wash": FixedEntry(colour=KelvinColour(kelvin=4000)),
        "memo-far": FixedEntry(colour=KelvinColour(kelvin=3000))}))
    warmer = turns()
    assert len(worked) == 2 and warmer.lights != first.lights

    rig.screens.operations = neutral_operations(OUTPUT, 7, DisplayLight(150, .01))
    dimmer_display = turns()
    assert len(worked) == 3 and dimmer_display.peak != warmer.peak

    frame = framed(lighting=lighting.model_copy(update={"lights": lighting.lights[:1]}), display_mm=(950, 540))
    wash_only = turns()
    assert len(worked) == 4 and len(wash_only.lights) == 1

    frame = framed(lighting=lighting.model_copy(update={"lights": lighting.lights[:1]}), display_mm=(1200, 675))
    larger = turns()
    assert len(worked) == 5 and larger.size_mm != wash_only.size_mm


@pytest.mark.parametrize(("stated", "row"), [(1000.0, 2000), (40000.0, 10000), (3456.7, 3500)])
def test_the_renderer_draws_a_stated_kelvin_past_the_table_at_its_drawn_row(stated, row):
    """The document carries a light's colour as stated (real kelvin); the renderer finishes the
    Frame with the lights of the row the Pi bounds and rounds it to (player/geometry.py
    `drawn_kelvin`), never refusing it. Mutation probe: no bound in `drawn_kelvin` -> 1000 K and
    40000 K raise ValueError("kelvin")."""
    lighting = FrameLighting(mat_depth_mm=0, paper=Paper(whiteness=.9, ink_density=2),
                             lights=(WashLight(light="room", lux=200, reference=AT_FULL),))
    frame = framed(lighting=lighting, display_mm=(950, 540))
    still = photo("still", start=0)

    def finished(kelvin: float):
        rig = Rig()
        rig.renderer.set_room_lights(RoomLightsDocument(change=1, lights={
            "room": FixedEntry(colour=KelvinColour(kelvin=kelvin))}))
        rig.renderer.prepare(still, frame)
        rig.settle(still, frame=frame)
        rig.clock.tick()
        return rig.fitter.draws[-1][0].lights

    drawn = finished(stated)
    assert len(drawn.lights) == 1 and drawn == finished(float(row))


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
    rig.settle(clip)                                    # target 1 shown (targets 2, 3 made with it)
    for serial in (2, 3, 4):
        rig.screens.busy_handles = {1003} if serial == 3 else set()
        rig.videos[0].push(serial)
        rig.videos[0].on_frame()
        rig.renderer.finish()                           # 2 into 2; then 1 shown, 2 finished, 3 busy:
    assert [target for _, target in rig.fitter.draws] == [1, 2, 3]     # none; then 4 into 3
    assert rig.device.imports == [1, 2, 4]


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


def test_a_box_position_places_its_plane_and_is_reported_without_a_trial():
    rig = Rig()
    still = photo("still", start=0)
    bound = framed(centred(0.5))
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
    tried = trial(PositionBox(top=0.25, width=0.5, height=0.5))
    rig.screens.trial_now = tried
    rig.clock.tick()
    result = rig.settle(still)
    moved = place_box(tried.calibration.fit.box(), SIZE)
    assert moved == Placement(0, 540, 1920, 1080)
    assert rig.screens.commits[-1][0].planes[0].at == moved
    assert rig.screens.overlays[-1] == (moved, SIZE)
    assert rig.screens.commits[-1][2] == tried.candidate.frame_tag     # the shell draws the overlay under it
    applied = result.applied_calibration
    assert applied.calibration == tried.calibration and applied.trial_id == str(tried.candidate.trial_id)
    assert (applied.trial_generation, applied.trial_sequence, applied.candidate_sha256) == (1, 1, "a" * 64)
    assert result.composition.binding == framed()       # what plays is unchanged
    rig.screens.trial_now = None                        # the trial ends: the binding's again
    rig.clock.tick()
    result = rig.settle(still)
    assert rig.screens.commits[-1][0].planes[0].at == Placement(0, 0, *SIZE)
    assert rig.screens.overlays[-1] is None and result.applied_calibration.trial_id is None
    assert not rig.screens.commits[-1][2].startswith("trial-")


def test_a_trials_lighting_finishes_the_still_again():
    rig = Rig()
    still = photo("still", start=0)
    rig.settle(still)
    lit = FrameLighting(mat_depth_mm=0, paper=Paper(whiteness=.8, ink_density=2),
                        lights=(WashLight(light="room", lux=300, reference=AT_FULL),))
    rig.screens.trial_now = trial(PositionBox(), lighting=lit)
    rig.clock.tick()
    rig.settle(still)
    assert len(rig.fitter.draws) == 2
    assert rig.fitter.draws[1][0].lights != rig.fitter.draws[0][0].lights
    assert rig.fitter.draws[1][0].lights.paper == lit.paper


def test_the_lights_take_the_frames_size_from_its_displays_and_the_drawn_box():
    """Frame box run decision 12: the binding carries the Display's size and the Player works out
    the Frame's from the box it draws (the binding's, or a trial's) and the screen's turn; with no
    Display size the lights have none (washes only). Mutation probe: size the lights by the
    binding's box during a trial -> the trial's draw reads 475 mm."""
    rig = Rig()
    still = photo("still", start=0)
    sized = framed(centred(0.5), display_mm=(950, 540))
    rig.settle(still, frame=sized)
    assert rig.fitter.draws[-1][0].lights.size_mm == pytest.approx((475.0, 270.0))
    rig.screens.trial_now = trial(centred(0.25))
    rig.clock.tick()
    rig.settle(still, frame=sized)
    assert rig.fitter.draws[-1][0].lights.size_mm == pytest.approx((237.5, 135.0))
    rig.screens.trial_now = None
    rig.clock.tick()
    rig.settle(still, frame=framed(centred(0.5)))
    assert rig.fitter.draws[-1][0].lights.size_mm is None


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
    """A Frame with no photo (Central's plan has no layers) still shows its Position trial: the
    grid, the overlay, and the commit tagged as the trial, which is what the shell draws the
    overlay under and what Display Host reports to Central as the trial presented."""
    rig = Rig()
    rig.screens.trial_now = trial(centred(0.5))
    result = rig.settle(fallback=True, timebase=None)
    assert result.status == "presented" and rig.screens.commits[-1][0].planes == ()
    assert rig.screens.commits[-1][2] == rig.screens.trial_now.candidate.frame_tag
    assert result.applied_calibration.trial_id == str(rig.screens.trial_now.candidate.trial_id)
    request = rig.patterns.draws[-1][0]
    assert request.operations.test_pattern is None and request.placement == Placement(960, 540, 1920, 1080)
    assert rig.screens.overlays[-1] == (request.placement, SIZE)
    rig.screens.trial_now = None                        # no trial: the fallback's black ground
    rig.clock.tick()
    rig.present(fallback=True, timebase=None)
    assert len(rig.patterns.draws) == 1 and rig.screens.grounds[-1].handle != rig.screens.grounds[-2].handle


def test_a_frame_with_nothing_to_play_shows_a_black_plane_the_compositor_sees():
    """Issue #123: with nothing kept and nothing playing (an empty fallback) the Frame is dark on
    purpose, drawn as a black picture plane over its placement (Display Host's shell keeps the
    Output with a picture plane, never with the ground alone), so it is never read as "nothing to
    show". The plane is the Player's own drawing: the presented composition stays empty, so no
    assignment is invented or reported presented; the candidate's commit stays the bare ground."""
    rig = Rig(admitted=False)
    frame = framed(centred(0.5))
    rig.present(frame=frame, fallback=True, timebase=None)
    rig.renderer.finish()
    rig.clock.tick()
    rig.present(frame=frame, fallback=True, timebase=None)
    assert rig.screens.commits[-1][0].planes == ()      # a candidate is not judged: its ground only
    rig.screens.admitted = True
    result = rig.settle(frame=frame, fallback=True, timebase=None)
    composition, pictures, _tag, _change = rig.screens.commits[-1]
    (plane,) = composition.planes
    at = Placement(960, 540, 1920, 1080)
    assert plane.dark and plane.key == DARK_KEY + frame.frame_id and plane.at == at and plane.strength == 1.0
    assert pictures[plane.key].size == (1920, 1080) and pictures[plane.key] != rig.screens.grounds[-1]
    assert rig.fitter.draws == [] and len(rig.fitter.blacks) == 2      # the ground's black and the Frame's
    assert result.status == "presented" and result.composition.layers == () and result.composition.fallback
    commits = len(rig.screens.commits)
    rig.clock.tick()
    rig.present(frame=frame, fallback=True, timebase=None)
    assert len(rig.screens.commits) == commits          # nothing changed, nothing committed
    still = photo("kept", start=0)
    rig.ready(still, frame=frame)
    rig.clock.tick()
    rig.settle(still, frame=frame, fallback=True, timebase=None)
    assert rig.strengths() == [("kept", 1.0)]            # a kept photo is its own plane: no dark one


@pytest.mark.parametrize("pattern", list(TestPattern))
@pytest.mark.parametrize("tried", [False, True], ids=["no-trial", "trial"])
def test_a_screen_with_nothing_to_play_still_shows_its_pattern_and_its_trial(pattern, tried):
    """A granted screen whose Frame has nothing to play (a fallback with no layers: Central sends no
    layers while the Position is to check) still commits what the screen must show: every test
    pattern and calibration target as its ground, a trial's overlay and the trial's tag."""
    rig = Rig()
    rig.renderer.set_player("player-1")
    rig.screens.operations = replace(neutral_operations(OUTPUT, 4, DisplayLight(200, .01)), test_pattern=pattern)
    rig.screens.trial_now = trial(centred(0.5)) if tried else None
    result = rig.settle(fallback=True, timebase=None)
    composition, pictures, tag, change = rig.screens.commits[-1]
    assert result.status == "presented" and composition.planes == () and pictures == {} and change == 4
    assert rig.patterns.draws[-1][0].operations.test_pattern is pattern
    assert rig.screens.grounds[-1].handle == 1000 + rig.patterns.draws[-1][1]
    if tried:
        assert tag == rig.screens.trial_now.candidate.frame_tag
        assert rig.screens.overlays[-1] == (Placement(960, 540, 1920, 1080), SIZE)
    else:
        assert not tag.startswith("trial-") and rig.screens.overlays[-1] is None


def test_a_calibration_targets_qr_names_the_player_once_it_has_an_id(monkeypatch):
    boards = []

    def board(placement, modules):
        boards.append((placement, modules))
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
    assert boards[-1][0] == rig.patterns.draws[-1][0].placement      # the board's aspect: the box's pixels
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


def test_an_output_gone_fails_the_present():
    rig = Rig(screen_now=None)
    assert (rig.present(photo("p", start=0)).status, rig.present(photo("p", start=0)).code) == ("failed", "decode")


def test_a_box_of_its_own_shape_places_at_place_boxs_pixels_and_finishes_at_its_aspect():
    """A box a quarter wide and half high on a 1920 x 1080 screen, its profile 16:9: the plane is at
    exactly `place_box`'s pixels (480 x 540) and the picture is finished at 480:540, the box's
    shape, never the profile's (frame box run, fb-6). Mutation probe: take `_place`'s height from
    the width and the profile's aspect -> RED."""
    rig = Rig(screen_now=Screen(OUTPUT, (1920, 1080), 0))
    still = photo("still", start=0)
    box = PositionBox(left=0.5, top=0.25, width=0.25, height=0.5)
    assert rig.settle(still, frame=framed(box)).status == "presented"
    assert rig.screens.commits[-1][0].planes[0].at == place_box(box.box(), (1920, 1080)) == Placement(960, 270, 480, 540)
    request = rig.fitter.draws[-1][0]
    assert request.aspect == pytest.approx(480 / 540)
    assert request.size[0] / request.size[1] == pytest.approx(480 / 540, rel=0.01)


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
    """A 4K still: its decode started, then taken, its rows uploaded BAND_BYTES at a time, its draw:
    one step a refresh over every layer, however often prepare is asked."""
    rig = Rig()
    first, second = sized("first", 3840, 2160, start=0), sized("second", 3840, 2160, start=1)
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
    assert rig.log[0] == "commit" and rig.log[1:] == ["import", "draw", "fence"]     # its targets made already
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
        assert rig.strengths(commits + index) == dissolved(alpha)


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
        frame = framed(centred(width))
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


def sized(key: str, width: int, height: int, **fields) -> LocalLayer:
    """A photo whose prepared copy is `width` x `height`."""
    variant = media(key.encode()).model_copy(update={"media_type": "image/jpeg", "width": width, "height": height})
    return LocalLayer(layer(key, variant=variant, **fields), Path(f"/media/{key}.jpg"), 0.0, 1.0)


HALF = framed(centred(0.5))          # a 1920 x 1080 placement on the 4K screen


def test_a_1080p_video_in_a_4k_placement_is_finished_at_1080p_and_shown_enlarged():
    """The owner, 2026-10-18: the look applied "at the minimum of the asset size and the frame
    size"; the display controller enlarges the finished picture to the Frame's placement."""
    rig = Rig()
    clip = video("clip")
    prepared_video(rig, clip)
    rig.settle(clip)
    request, target = rig.fitter.draws[-1]
    assert request.size == (1920, 1080)
    plane, = rig.screens.commits[-1][0].planes
    assert plane.at == Placement(0, 0, 3840, 2160) and plane.size == (1920, 1080) and plane.enlarged
    assert rig.screens.commits[-1][1]["clip"].size == (1920, 1080)


@pytest.mark.parametrize(("source", "finished", "enlarged"), [
    ((6000, 4000), (1920, 1080), False),        # a large photo: finished at its placement, shown 1:1
    ((1280, 720), (1280, 720), True),           # a small photo: finished at its own size, enlarged
])
def test_a_photo_is_finished_at_the_smaller_of_its_source_and_its_placement(source, finished, enlarged):
    rig = Rig()
    still = sized("still", *source, start=0)
    rig.ready(still, frame=HALF)
    rig.settle(still, frame=HALF)
    assert [request.size for request, _ in rig.fitter.draws] == [finished]
    plane, = rig.screens.commits[-1][0].planes
    assert plane.at == Placement(960, 540, 1920, 1080) and plane.size == finished and plane.enlarged is enlarged
    assert rig.screens.commits[-1][1]["still"].size == finished


# The fireplace Frame as read from production on 2026-10-26: a 3,004 x 2,140 box on a 3,840 x 2,160
# screen, showing a 2:3 photo whose copy is 3,004 x 4,506 (its window holds the Frame exactly).
FIREPLACE = framed(PositionBox(left=418 / 3840, top=10 / 2160, width=3004 / 3840, height=2140 / 2160))
TALL = (3004, 4506)


def test_a_still_hands_the_gpu_only_its_window_at_its_finish_size():
    """V3D's largest texture is 4,096 on a side: the copy's 4,506 rows never reach the GPU, only
    its window at the finish size (player/planes.py `still_region`), drawn uncut. Mutation probe
    AC4 (decode the whole copy): this fails."""
    rig = Rig()
    portrait = sized("portrait", *TALL, start=0)
    rig.ready(portrait, frame=FIREPLACE)
    plane_at = rig.renderer.present(OutputComposition(FIREPLACE, (portrait,), False, 0.0))
    assert plane_at.status == "presented"
    placement = rig.screens.commits[-1][0].planes[0].at
    assert (placement.width, placement.height) == (3004, 2140)
    assert rig.stills == [Path("/media/portrait.jpg")]
    assert rig.regions == [still_region(portrait.layer, placement)]
    assert rig.regions[0].size == (3004, 2140)
    assert rig.device.sizes == [(3004, 2140)]
    request, _ = rig.fitter.draws[-1]
    assert request.source.size == request.size == (3004, 2140)
    assert request.crop == CENTRED


def test_a_new_window_decodes_the_still_again_and_nothing_else_does():
    """A Position whose box changes shape is a new window: the still is decoded again and its old
    texture deleted. The picture operations, the lights and the turn only draw it again."""
    rig = Rig()
    still = sized("still", 1280, 720, start=0)            # finished at its own size on every box below
    rig.settle(still)
    assert (len(rig.regions), rig.device.deleted) == (1, [])
    rig.screens.operations = neutral_operations(OUTPUT, 7, DisplayLight(200, .01))
    rig.settle(still)
    rig.renderer.set_room_lights(RoomLightsDocument(change=1, lights={"room": FixedEntry(colour=KelvinColour(kelvin=6500))}))
    rig.settle(still)
    rig.screens.screen_now = Screen(OUTPUT, (2160, 3840), 90)
    rig.settle(still, frame=framed(PositionBox(top=1313 / 3840, height=1215 / 3840)))
    assert len(rig.regions) == 1 and rig.device.deleted == []
    rig.screens.screen_now = Screen(OUTPUT, (3840, 2160), 0)
    rig.settle(still, frame=framed(PositionBox(width=0.5)))      # 1,920 x 2,160: another shape
    assert len(rig.regions) == 2 and rig.device.deleted == [501]
    assert rig.regions[-1].box[2] < rig.regions[0].box[2]


def test_one_still_decode_is_in_flight_at_a_time():
    """The second still's decode is not started until the first's answers. Mutation probe AC7
    (no gate): this fails."""
    rig = Rig()
    first, second = photo("first", start=0), photo("second", start=1)
    frame = framed()
    holding = []

    def held(path, region):
        holding.append(FakeStill(region.size, held=True))
        return holding[-1]

    rig.renderer = PlaneRenderer(rig.screens, rig.device, rig.fitter, rig.patterns, NoLuts(),
                                 stills=held, videos=rig._video, clock=rig.clock)
    for _ in range(10):
        rig.renderer.prepare(first, frame)
        rig.renderer.prepare(second, frame)
        rig.renderer.finish()
        rig.clock.tick()
    assert len(holding) == 1
    holding[0].held = False
    for _ in range(10):
        rig.renderer.prepare(first, frame)
        rig.renderer.prepare(second, frame)
        rig.renderer.finish()
        rig.clock.tick()
    assert len(holding) == 2


def _decodes_started(rig: Rig, *layers: LocalLayer, frame=None, refreshes: int = 10) -> list[str]:
    """Prepare and finish `layers` for `refreshes` refreshes: the stills whose decode started."""
    for _ in range(refreshes):
        for local in layers:
            rig.renderer.prepare(local, frame or framed())
        rig.renderer.finish()
        rig.clock.tick()
    return [path.stem for path in rig.stills]


def test_a_still_released_while_it_decodes_never_holds_up_another():
    """The first still's layer goes while its decode is held: the second still decodes. Mutation
    probe (release keeps the decode in flight): the second never starts."""
    rig = Rig()
    rig.holding = {"first"}
    first, second = photo("first", start=0), photo("second", start=1)
    assert _decodes_started(rig, first) == ["first"]
    rig.renderer.release("first")
    assert _decodes_started(rig, second) == ["first", "second"]
    assert rig.renderer.prepare(second, framed()).status == "prepared"


def test_a_decode_its_layer_no_longer_needs_never_holds_up_another():
    """A still's box changes shape, so its window decodes again; it changes back before that decode
    answers, so the picture it has is right again and nothing is owed: that decode is given up and
    another still decodes (and the still decodes again if its box changes once more). Mutation
    probe (no give-up in `_settle_decoding`): the other still never starts."""
    rig = Rig()
    still, other = sized("still", 1280, 720, start=0), photo("other", start=1)
    rig.settle(still)
    rig.holding = {"still"}
    half = framed(PositionBox(width=0.5))
    assert _decodes_started(rig, still, frame=half, refreshes=3) == ["still", "still"]
    assert rig.renderer.prepare(still, framed()).status == "prepared"
    assert _decodes_started(rig, other) == ["still", "still", "other"]
    assert rig.decodes[1].closed
    assert _decodes_started(rig, still, frame=half, refreshes=3)[-1] == "still"


def test_a_decode_answering_while_its_layer_waits_for_a_fade_frees_the_next():
    """A still started decoding before a fade it is not needed during; its decode answers during the
    fade: a still needed within the fade decodes then, and the first is prepared after it. Mutation
    probe (a deferred layer's answer not taken in `_settle_decoding`): the one within the fade
    never decodes during it."""
    rig = Rig()
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=3.0)
    rig.ready(outgoing, incoming)
    later, within_fade = photo("later", start=104), photo("within", start=102)
    rig.holding = {"later"}
    assert _decodes_started(rig, later, refreshes=3)[-1] == "later"
    timebase = 100.1 - rig.clock.now
    rig.present(outgoing, incoming, timebase=timebase)
    rig.decodes[-1].held = False                        # it answers during the fade
    for _ in range(30):
        rig.present(outgoing, incoming, timebase=timebase)
        rig.renderer.prepare(within_fade, framed())
        rig.renderer.finish()
        rig.clock.tick()
    assert rig.renderer.prepare(within_fade, framed()).status == "prepared"
    rig.clock.now += 3.0                                # the fade is over
    rig.present(outgoing, incoming, timebase=timebase)
    rig.ready(later)
    assert [path.stem for path in rig.stills].count("later") == 1


def test_a_still_whose_decode_cannot_start_fails_and_frees_the_next():
    """The decode refused at its start fails the layer, and the next still decodes (the refusal
    frees the decode, and `_settle_decoding` gives up a failed layer's: either alone suffices)."""
    rig = Rig()
    rig.refusing = {"first"}
    first, second = photo("first", start=0), photo("second", start=1)
    assert _decodes_started(rig, first, second) == ["first", "second"]
    assert rig.renderer.prepare(first, framed()).status == "failed"
    assert rig.renderer.prepare(second, framed()).status == "prepared"


def test_a_turned_screen_finishes_the_picture_at_its_size_laid_out_turned():
    rig = Rig(screen_now=Screen(OUTPUT, (2160, 3840), 90))
    still = sized("still", 1280, 720, start=0)
    wide = framed(PositionBox(top=1313 / 3840, height=1215 / 3840))     # 2160 x 1215 across the turned screen
    rig.ready(still, frame=wide)
    rig.settle(still, frame=wide)
    assert rig.fitter.draws[-1][0].size == (1280, 720)
    assert rig.screens.commits[-1][1]["still"].size == (720, 1280)


def test_capacity_counts_the_enlarged_planes():
    """Five translucent pictures over one Frame: their pixels fit the line, but the controller
    enlarges at most four at once (bench T2); shown 1:1 the same five fit."""
    rig = Rig()

    def layers(width: int, height: int, count: int) -> tuple[LocalLayer, ...]:
        return tuple(LocalLayer(layer(f"p{index}", start=0, opacity=0.5, variant=media(f"p{index}".encode()).model_copy(
            update={"width": width, "height": height})), Path(f"/media/p{index}.png"), 0.0, 0.5) for index in range(count))

    def fits(*local: LocalLayer) -> bool:
        return rig.renderer.capacity((OutputComposition(HALF, local, False, 0.0),)).available

    assert fits(*layers(1280, 720, 4)) is True
    assert fits(*layers(1280, 720, 5)) is False
    assert fits(*layers(1920, 1080, 5)) is True


def test_a_present_while_its_last_commit_is_unanswered_does_no_other_work():
    """Pi diagnosis (milestone 3, fix A): while a video's fence is pending the frame loop turns
    every millisecond; a present that cannot commit returns its last answer before asking for the
    grant or sweeping."""
    rig = Rig(answer=False)
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=1.0)
    rig.ready(outgoing, incoming)
    first = rig.present(outgoing, incoming, timebase=100.2)
    asked = []
    grant = rig.screens.grant
    rig.screens.grant = lambda composition: asked.append(composition) or grant(composition)
    for _ in range(5):
        rig.clock.tick()
        assert rig.renderer.frame() is None and rig.present(outgoing, incoming, timebase=100.2) == first
    assert asked == [] and len(rig.screens.commits) == 1


def test_due_reads_the_screens_word_before_judging_a_commit_overdue():
    """Pi diagnosis (milestone 3, fix B): an answer that arrived since the turn frees the screen, so
    a fade's next frame is due now, not when the commit would be overdue."""
    rig = Rig(latency=FRAME / 2)
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=10.0)
    rig.ready(outgoing, incoming)
    timebase = 100.2 - rig.clock.now
    rig.settle(outgoing, incoming, timebase=timebase)
    rig.clock.tick()
    assert rig.present(outgoing, incoming, timebase=timebase).status == "presented"
    rig.renderer.finish()
    assert rig.renderer._outputs[OUTPUT].pacer.in_flight
    rig.clock.now += FRAME
    assert rig.renderer.due() == pytest.approx(rig.clock.now, abs=FRAME)
    assert not rig.renderer._outputs[OUTPUT].pacer.in_flight


def test_a_prepared_video_has_all_its_targets_before_its_first_fade_frame():
    """Pi diagnosis (milestone 3, fix C): a video's targets are made and imported together while it
    is prepared, at its finish size, so the fade that reveals it makes none."""
    rig = Rig()
    clip = video("clip", start=0, fade_in=1.0)
    decode = prepared_video(rig, clip)
    entry = rig.renderer._layers["clip"]
    assert [picture.target.size for picture in entry.targets] == [(1920, 1080)] * VIDEO_TARGETS
    rig.settle(clip, timebase=0.1)
    rig.log.clear()
    for serial in range(2, 8):
        rig.clock.tick()
        rig.present(clip, timebase=0.1)
        decode.push(serial)
        decode.on_frame()
        rig.renderer.finish()
    assert "target" not in rig.log and rig.log.count("import") >= 3


class MonotonicClock:
    """The executor rig's ManualClock's monotonic seconds, as the renderer and its fakes read them."""

    def __init__(self, manual) -> None:
        self.manual = manual

    def __call__(self) -> float:
        return self.manual.monotonic()


class Wall:
    """The real Executor driving the real PlaneRenderer over this module's fakes, on one clock (plan
    time = monotonic + 100): the frame loop (player/mainloop.py `FrameLoop`: `frame`, then `finish`)
    every refresh and the planning tick (player/service.py: `prepare_imminent`, then `tick`) every
    TICK refreshes. Each video's decoder hands over a new frame every refresh."""

    TICK = 2

    def __init__(self, directory: Path) -> None:
        self.plan = PlanRig(directory, bindings=(framed(),))
        self.planes = Rig(MonotonicClock(self.plan.clock))
        self.screens, self.renderer = self.planes.screens, self.planes.renderer
        self.plan.renderer = self.plan.executor.renderer = self.renderer
        self.observations: list = []
        self.refreshes = 0

    @property
    def now(self) -> float:
        """Plan time."""
        return self.plan.clock.utc()

    def play(self, *layers) -> None:
        """Offer `layers` (each one's media its key's bytes), secure each, prepare them through the
        frame loop and commit them all."""
        self.plan.offer(*layers)
        for item in layers:
            self.plan.secure(item.assignment_id, item.assignment_id.encode())
        keys = tuple(item.assignment_id for item in layers)
        for _ in range(200):
            self.plan.executor.prepare_imminent()
            if set(self.plan.executor.readiness().prepared) == set(keys):
                break
            self.refresh(tick=False)
        else:
            raise AssertionError("never prepared")
        self.plan.commit(*keys)

    def refresh(self, *, tick: bool = True) -> None:
        """One refresh: decoders hand over a frame, the frame loop's turn, the planning tick on its
        cadence."""
        self.plan.advance(FRAME)
        self.refreshes += 1
        for decode in self.planes.videos:
            decode.push(self.refreshes + 1)
            decode.on_frame()
        self.renderer.frame()
        self.renderer.finish()
        if tick and self.refreshes % self.TICK == 0:
            self.tick()

    def tick(self) -> None:
        self.plan.executor.prepare_imminent()
        self.observations += self.plan.executor.tick()

    def run(self, until: float) -> None:
        """Refreshes until plan time `until`."""
        while self.now + FRAME <= until:
            self.refresh()

    def cut(self, at: float, *, silent: bool = False) -> int:
        """The layer set changes at plan time `at` while the frame loop's last commit before it is
        in flight: the frame loop's next commit before `at` has its answer withheld; the planning
        tick runs just after `at`; then the answer comes (never, if `silent`: the display says
        nothing from then on) and the wall runs a second and a half on. The index of the first
        commit after the one in flight."""
        self.run(at - 4 * FRAME)
        made = len(self.screens.commits)
        while len(self.screens.commits) == made:
            assert self.now + FRAME < at, "the frame loop commits before the cut"
            self.refresh(tick=False)
        self.screens.answer = False
        mark = len(self.screens.commits)
        self.plan.advance(at - self.now + 0.005)
        self.tick()
        assert len(self.screens.commits) == mark, "nothing is committed while the last commit is in flight"
        self.screens.answer = not silent
        self.run(at + 1.5)
        return mark

    def shown(self, since: int) -> list[list[str]]:
        """Each commit's planes from commit `since` on, by assignment."""
        return [[plane.layer.layer.assignment_id for plane in composition.planes]
                for composition, *_ in self.screens.commits[since:]]

    def failures(self) -> list[tuple[str, str, str]]:
        return [(o.assignment_id, o.status, o.detail) for o in self.observations
                if o.status in ("failed", "fallback")]


def still(key: str, **fields):
    """A JPEG layer on the wall's Frame, its media its key's bytes."""
    return layer(key, data=key.encode(), variant=media(key.encode()).model_copy(update={"media_type": "image/jpeg"}),
                 **fields)


def test_a_cross_fades_end_never_shows_the_old_photo_again(tmp_path):
    """Issue #130: Central plans the old photo at full strength until the new one has faded in, so
    the old one leaves the plan at the fade's end, just after the frame loop's last fade commit,
    while that commit is unanswered. Every commit after it shows the new photo alone (never the
    old one, kept by its Scene, nor black), and the new photo never fails."""
    wall = Wall(tmp_path)
    old = still("old", start=100, end=106, after_end="keep_this_photo")
    new = still("new", start=103, end=130, media_origin=103, fade_in=3, after_end="keep_this_photo")
    wall.play(old, new)
    mark = wall.cut(106)
    assert wall.shown(mark) and all(planes == ["new"] for planes in wall.shown(mark))
    assert wall.failures() == []


def test_a_dips_cut_never_shows_the_old_photo_again(tmp_path):
    """The dip (fade out, then in): the old photo fades out to its end, where the new one starts
    fading in. No commit after the cut shows the old photo, and the new one never fails."""
    wall = Wall(tmp_path)
    old = still("old", start=100, end=106, fade_out=1, after_end="keep_this_photo")
    new = still("new", start=106, end=130, media_origin=106, fade_in=1, after_end="keep_this_photo")
    wall.play(old, new)
    mark = wall.cut(106)
    assert all("old" not in planes for planes in wall.shown(mark))
    assert wall.shown(mark)[-1] == ["new"]
    assert wall.failures() == []


def test_a_photo_cut_in_over_a_playing_video_is_never_dropped(tmp_path):
    """A video commits every frame, so its screen's last commit is in flight at almost every
    planning tick: a photo that starts over it is shown from its first commit on and never fails."""
    wall = Wall(tmp_path)
    clip = layer("clip", data=b"clip", video=True, start=100, end=130)
    cover = still("cover", start=103, end=130, media_origin=103)
    wall.play(clip, cover)
    mark = wall.cut(103)
    shown = wall.shown(mark)
    first = next(index for index, planes in enumerate(shown) if planes == ["cover"])
    assert all(planes == ["cover"] for planes in shown[first:])
    assert wall.failures() == []


def test_a_cross_fades_end_on_a_silent_display_never_fails_the_new_photo(tmp_path):
    """A display that never answers (off, or a slow compositor at its worst) holds each commit for
    the pacer's OVERDUE: the planning tick's new request waits "pending" that long and is committed
    by the first present to find the slot free, within the executor's draw grace. The old photo is
    never shown after the cut and the new one never fails."""
    wall = Wall(tmp_path)
    old = still("old", start=100, end=106, after_end="keep_this_photo")
    new = still("new", start=103, end=130, media_origin=103, fade_in=3, after_end="keep_this_photo")
    wall.play(old, new)
    mark = wall.cut(106, silent=True)
    assert wall.shown(mark) and all(planes == ["new"] for planes in wall.shown(mark))
    assert wall.failures() == []


def test_the_draw_grace_outlasts_a_commit_in_flight_and_a_planning_tick():
    """The executor gives a "pending" request up after DRAW_GRACE; the plane renderer holds a
    commit in flight for at most OVERDUE, and the held request is committed by the next present,
    at worst the next planning tick. A shorter grace fails a silent Output's new photo ("decode")
    and shows the old one again (issue #130)."""
    from player import executor, pacing, service
    assert pacing.OVERDUE + service.PLANNING_TICK_MS / 1000 < executor.DRAW_GRACE
