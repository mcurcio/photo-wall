"""The Player's new path through fakes of its five ports (player/plane_renderer.py): each picture is
finished once for its Frame as it is now and finished again only when something its pixels depend
on changes; a fade is a plane strength, never a draw; a picture is committed only once the GPU has
finished it, a still is finished in steps, at most one a refresh, and not during a fade it is not
needed in; every commit shows the screen's ground; the commit is made as soon as the last is
answered, cut to the controller's budget and tagged as the screen's grant asks."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_executor import binding, layer, media

from contracts.frame_fit import FrameRectangle
from contracts.frame_lighting import AT_FULL, FrameLighting, Paper, WashLight
from contracts.node_frame import neutral_operations
from contracts.node_picture import DisplayLight
from contracts.room_light import FixedColour, FixedEntry, RoomLightsDocument
from player.decode import StillPicture
from player.geometry import encode_srgb
from player.gpu import LINEAR, NV12, XR24, DmabufFrame, Target
from player.pacing import DEFAULT_REFRESH, Word
from player.plane_renderer import BAND_BYTES, VIDEO_TARGETS, FrameOnScreen, PlaneRenderer
from player.plane_screen import FrameGrant, ScreenPicture
from player.planes import Composition, Screen, plane_strength
from player.rendering import LocalLayer, OutputComposition

OUTPUT = "hdmi1"
FULL = FrameRectangle(0.5, 0.5, 1.0)
GRANT = uuid4()
FRAME = 1 / 60


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
    answer: bool = True
    latency: float = 0.0            # seconds from a commit to its presentation
    busy_handles: set[int] = field(default_factory=set)
    commits: list[tuple[Composition, dict, str, int | None]] = field(default_factory=list)
    grounds: list = field(default_factory=list)          # each commit's ground
    forgotten: list[int] = field(default_factory=list)
    imported: int = 0
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

    def import_picture(self, target):
        self.imported += 1
        return ScreenPicture(1000 + target.framebuffer, target.size)

    def busy(self, picture):
        return picture.handle in self.busy_handles

    def forget(self, picture):
        self.forgotten.append(picture.handle)

    def present(self, composition, pictures, grant, tag, picture_change, ground=None):
        self._sequence += 1
        self._committed_at = self.clock()
        self.commits.append((composition, dict(pictures), tag, picture_change))
        self.grounds.append(ground)
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

    def target(self, size, *, opaque):
        self.made += 1
        return Target(size, self.made, self.made, 100 + self.made, size[0] * 4, XR24, LINEAR)

    def free(self, target):
        self.freed.append(target.framebuffer)

    def texture(self, size):
        self.textures += 1
        return 500 + self.textures

    def upload(self, texture, size, stride, pixels, rows):
        self.uploads.append(rows)

    def delete_texture(self, texture):
        pass

    def import_frame(self, frame):
        self.imports.append(frame.serial)
        return 900 + frame.serial

    def forget_frames(self, fds):
        pass

    def fence(self):
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

    def draw(self, request, target):
        self.draws.append((request, target.framebuffer))

    def black(self, target):
        self.blacks.append(target.framebuffer)


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
        self.device, self.fitter = FakeDevice(), FakeFitter()
        self.videos: list[FakeVideo] = []
        self.stills: list[Path] = []
        self.changes: list[str] = []
        self.renderer = PlaneRenderer(self.screens, self.device, self.fitter, NoLuts(),
                                      stills=self._still, videos=self._video,
                                      on_change=self.changes.append, clock=self.clock)

    def _still(self, path):
        self.stills.append(path)
        return FakeStill()

    def _video(self, path, on_frame):
        video = FakeVideo(on_frame)
        self.videos.append(video)
        return video

    def present(self, *layers: LocalLayer, frame: FrameOnScreen | None = None, timebase: float = 0.0):
        frame = frame or FrameOnScreen(binding(), FULL)
        return self.renderer.present(OutputComposition(frame.binding, layers, timebase=timebase), frame)

    def ready(self, *layers: LocalLayer, frame: FrameOnScreen | None = None) -> int:
        """Prepare `layers` a refresh at a time until each is prepared: the refreshes it took."""
        frame = frame or FrameOnScreen(binding(), FULL)
        for refreshes in range(1, 200):
            if all(self.renderer.prepare(local, frame).status == "prepared" for local in layers):
                return refreshes
            self.clock.tick()
        raise AssertionError("never prepared")

    def settle(self, *layers: LocalLayer, frame: FrameOnScreen | None = None, timebase: float = 0.0):
        """Present `layers` a refresh at a time until a present answers "presented"."""
        for _ in range(200):
            result = self.present(*layers, frame=frame, timebase=timebase)
            if result.status == "presented":
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
    frame = FrameOnScreen(binding(), FULL)
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
    frame = FrameOnScreen(binding(), FULL)
    assert rig.renderer.prepare(first, frame).status == "prepared"
    assert rig.renderer.prepare(second, frame).status == "prepared"
    assert len(rig.fitter.blacks) == 1 and rig.fitter.draws == []


def test_what_a_still_depends_on_finishes_it_again_and_nothing_else_does():
    lit = binding(lighting=FrameLighting(mat_depth_mm=0, paper=Paper(whiteness=.9, ink_density=2),
                                         lights=(WashLight(light="room", lux=200, reference=AT_FULL),)))
    rig = Rig()
    still = photo("still", start=0)
    frame = FrameOnScreen(lit, FULL)

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
    frame = FrameOnScreen(lit, FrameRectangle(0.5, 0.5, 0.5))
    assert draws_after(lambda: None) == 1               # a new rectangle size
    assert rig.fitter.draws[-1][0].placement.width == 1920
    assert draws_after(lambda: setattr(rig.screens, "screen_now", Screen(OUTPUT, (2160, 3840), 90))) == 1
    request = rig.fitter.draws[-1][0]
    assert request.turn == 90 and rig.screens.commits[-1][0].screen.turn == 90
    assert rig.screens.commits[0][3] is None            # the stand-in's draw names no change


def test_a_covered_layer_keeps_its_picture():
    rig = Rig()
    under, over = photo("under", start=0), photo("over", start=1)
    frame = FrameOnScreen(binding(), FULL)
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
    rig.present(gone)
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


def test_a_video_frame_is_drawn_into_a_released_target_and_said():
    rig = Rig()
    clip = video("clip")
    prepared_video(rig, clip)
    assert rig.present(clip).status == "presented"
    first = rig.screens.commits[-1][1]["clip"]
    rig.videos[0].push(2)
    rig.videos[0].on_frame()
    assert rig.changes == [OUTPUT]
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
    rig.present(clip)                                   # target 1 shown
    rig.videos[0].push(2)
    rig.videos[0].on_frame()                            # into target 3 (target 2: the ground)
    rig.screens.busy_handles.add(1003)
    rig.videos[0].push(3)
    rig.videos[0].on_frame()                            # 1 shown, 3 finished: target 4
    assert [target for _, target in rig.fitter.draws] == [1, 3, 4]
    rig.screens.busy_handles.add(1004)
    rig.videos[0].push(4)
    rig.videos[0].on_frame()                            # 1 shown, 3 busy, 4 finished: no target
    assert len(rig.fitter.draws) == VIDEO_TARGETS


def test_five_full_width_planes_are_cut_to_three_and_the_cut_are_absent():
    rig = Rig()
    layers = [photo("p0", start=0)] + [photo(f"p{index}", start=index, opacity=0.5) for index in range(1, 5)]
    rig.ready(*layers)
    result = rig.present(*layers)
    assert result.status == "presented"
    assert [key for key, _ in rig.strengths()] == ["p2", "p3", "p4"]
    assert [local.layer.assignment_id for local in result.composition.layers] == ["p2", "p3", "p4"]
    assert set(rig.screens.commits[-1][1]) == {"p2", "p3", "p4"}


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
        assert rig.renderer.prepare(clip, FrameOnScreen(binding(), FULL)).status == "prepared"
    assert len(rig.fitter.draws) == 1 and rig.changes == [] and rig.device.imports == [1]
    started = replace(clip, position=0.02)
    assert rig.present(started).status == "presented"
    assert decode.seeks == [0.0, 0.02]                  # started from where the layer is
    assert rig.screens.commits[-1][1]["clip"].handle == 1001       # the prepared frame meanwhile
    decode.push(30)
    decode.on_frame()
    assert rig.device.imports == [1, 30] and rig.changes == [OUTPUT]


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
    assert rig.device.imports == [1] and rig.changes == []


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
    off = FrameOnScreen(binding(), FrameRectangle(0.9, 0.5, 1.0))
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
    rig.present(still)
    candidate = rig.screens.grounds[-1]
    assert candidate is not None and candidate.size == (3840, 2160)
    rig.screens.admitted = True
    rig.clock.tick()
    rig.present(still)
    assert rig.screens.grounds[-1] == candidate
    assert len(rig.fitter.blacks) == 1
    black = LocalLayer(layer("black", presentation="black", variant=None, start=0), None, 0.0, 1.0)
    assert rig.renderer.prepare(black, FrameOnScreen(binding(), FULL)).status == "prepared"
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
    frame = FrameOnScreen(binding(), FULL)
    for _ in range(20):
        assert rig.renderer.prepare(still, frame).status == "pending"
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
    decode.on_frame()                                   # drawn, not finished
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
    rig.renderer = PlaneRenderer(rig.screens, rig.device, rig.fitter, NoLuts(), stills=lambda path: FakeStill((3840, 2160)),
                                 videos=rig._video, clock=rig.clock)
    first, second = photo("first", start=0), photo("second", start=1)
    frame = FrameOnScreen(binding(), FULL)
    bands = -(-3840 * 4 * 2160 // BAND_BYTES)
    steps = []
    for _ in range(2 * (bands + 2) + 2):
        for _ in range(3):                              # asked three times a refresh
            rig.renderer.prepare(first, frame)
            rig.renderer.prepare(second, frame)
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
    frame = FrameOnScreen(binding(), FULL)
    decoded = len(rig.stills)
    for _ in range(30):                                 # half a second into the fade
        rig.present(outgoing, incoming, timebase=timebase)
        assert rig.renderer.prepare(later, frame).status == "pending"
        rig.renderer.prepare(within_fade, frame)
        rig.clock.tick()
    assert rig.stills[decoded:] == [Path("/media/within.jpg")]
    assert rig.renderer.prepare(within_fade, frame).status == "prepared"
    rig.clock.now += 3.0                                # the fade is over
    rig.present(outgoing, incoming, timebase=timebase)
    rig.ready(later)
