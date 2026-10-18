"""The Player's new path through fakes of its five ports (player/plane_renderer.py): each picture is
finished once for its Frame as it is now and finished again only when something its pixels depend
on changes; a fade is a plane strength, never a draw; the commit is paced, cut to the controller's
budget and tagged as the screen's grant asks."""
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
from player.plane_renderer import VIDEO_TARGETS, FrameOnScreen, PlaneRenderer
from player.plane_screen import ScreenPicture
from player.planes import Composition, Screen, plane_strength
from player.rendering import LocalLayer, OutputComposition
from player.wayland_frames import FrameGrant

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
    busy_handles: set[int] = field(default_factory=set)
    commits: list[tuple[Composition, dict, str, int | None]] = field(default_factory=list)
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

    def present(self, composition, pictures, grant, tag, picture_change):
        self._sequence += 1
        self._committed_at = self.clock()
        self.commits.append((composition, dict(pictures), tag, picture_change))
        return self._sequence

    def presentation(self, output_id, now):
        if not self._sequence or not self.answer:
            return None
        return Word(self._sequence, self._committed_at, DEFAULT_REFRESH)


@dataclass
class FakeDevice:
    made: int = 0
    freed: list[int] = field(default_factory=list)
    uploads: int = 0
    imports: list[int] = field(default_factory=list)

    def target(self, size, *, opaque):
        self.made += 1
        return Target(size, self.made, self.made, 100 + self.made, size[0] * 4, XR24, LINEAR)

    def free(self, target):
        self.freed.append(target.framebuffer)

    def upload(self, size, stride, pixels):
        self.uploads += 1
        return 500 + self.uploads

    def delete_texture(self, texture):
        pass

    def import_frame(self, frame):
        self.imports.append(frame.serial)
        return 900 + frame.serial

    def forget_frames(self, fds):
        pass

    def flush(self):
        pass


@dataclass
class FakeFitter:
    draws: list = field(default_factory=list)          # (request, target framebuffer)
    blacks: list = field(default_factory=list)

    def draw(self, request, target):
        self.draws.append((request, target.framebuffer))

    def black(self, target):
        self.blacks.append(target.framebuffer)


class FakeStill:
    def result(self):
        return StillPicture((1920, 1080), 1920 * 4, b"", False)

    def close(self):
        pass


@dataclass
class FakeVideo:
    on_frame: object
    frames: list[DmabufFrame] = field(default_factory=list)
    returned: list[int] = field(default_factory=list)

    def push(self, serial: int) -> None:
        self.frames.append(DmabufFrame(serial, (1920, 1080), NV12, 0, (0, 0), (1920, 1920), serial, serial / 30))

    def seek(self, position):
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
        self.changes: list[str] = []
        self.renderer = PlaneRenderer(self.screens, self.device, self.fitter, NoLuts(),
                                      stills=lambda path: FakeStill(), videos=self._video,
                                      on_change=self.changes.append, clock=self.clock)

    def _video(self, path, on_frame):
        video = FakeVideo(on_frame)
        self.videos.append(video)
        return video

    def present(self, *layers: LocalLayer, frame: FrameOnScreen | None = None, timebase: float = 0.0):
        frame = frame or FrameOnScreen(binding(), FULL)
        return self.renderer.present(OutputComposition(frame.binding, layers, timebase=timebase), frame)

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


def video(key: str) -> LocalLayer:
    return LocalLayer(layer(key, video=True), Path(f"/media/{key}.mp4"), 0.0, 1.0)


def test_a_dissolve_draws_each_still_once_and_commits_the_plane_strengths():
    """60 frame times through a one-second dissolve: two draws, 60 commits whose incoming strength
    is its alpha (over a picture) and whose outgoing photo stays at full."""
    rig = Rig()
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=1.0)
    for _ in range(60):
        result = rig.present(outgoing, incoming, timebase=100.0)
        assert result.status == "presented"
        rig.clock.tick()
    assert len(rig.fitter.draws) == 2
    assert len(rig.screens.commits) == 60
    for index, (composition, _pictures, _tag, _change) in enumerate(rig.screens.commits):
        alpha = incoming.layer.alpha(100.0 + index * FRAME)
        wanted = [("out", 1.0)] + ([("in", round(plane_strength(alpha, True), 9))] if alpha else [])
        assert rig.strengths(index) == wanted


def test_a_dip_to_black_commits_the_srgb_strength():
    rig = Rig()
    leaving = photo("leaving", start=0, end=101, fade_out=1.0)
    rig.present(leaving, timebase=100.5)
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
        assert rig.present(still, frame=frame).status == "presented"
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
    assert rig.renderer.prepare(under, frame).status == "prepared"
    assert rig.renderer.prepare(over, frame).status == "prepared"
    rig.present(under, over)
    assert rig.strengths() == [("over", 1.0)]
    assert rig.screens.forgotten == [] and rig.device.freed == []
    assert rig.renderer.prepare(under, frame).status == "prepared"
    assert len(rig.fitter.draws) == 2


def test_a_released_layer_is_freed_once_no_commit_shows_it():
    rig = Rig()
    gone, stays = photo("gone", start=0), photo("stays", start=1)
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


def test_a_video_frame_is_drawn_into_a_released_target_and_said():
    rig = Rig()
    clip = video("clip")
    assert rig.renderer.prepare(clip, FrameOnScreen(binding(), FULL)).status == "pending"
    rig.videos[0].push(1)
    assert rig.present(clip).status == "presented"
    first = rig.screens.commits[-1][1]["clip"]
    rig.videos[0].push(2)
    rig.videos[0].on_frame()
    assert rig.changes == [OUTPUT]
    assert rig.fitter.draws[-1][1] != first.handle - 1000      # not the shown one
    assert rig.fitter.draws[-1][0].source.external and rig.device.imports == [1, 2]
    assert rig.videos[0].returned == [1]                # the older frame went back once drawn over
    rig.clock.tick()
    rig.present(clip)
    assert rig.screens.commits[-1][1]["clip"] != first


def test_a_busy_target_is_never_drawn_into():
    rig = Rig()
    clip = video("clip")
    rig.renderer.prepare(clip, FrameOnScreen(binding(), FULL))
    rig.videos[0].push(1)
    rig.present(clip)                                   # target 1 shown
    rig.videos[0].push(2)
    rig.videos[0].on_frame()                            # into target 2
    rig.screens.busy_handles.add(1002)
    rig.videos[0].push(3)
    rig.videos[0].on_frame()                            # 1 shown, 2 busy: target 3
    assert [target for _, target in rig.fitter.draws] == [1, 2, 3]
    rig.screens.busy_handles.add(1003)
    rig.videos[0].push(4)
    rig.videos[0].on_frame()                            # every one of VIDEO_TARGETS shown or busy
    assert len(rig.fitter.draws) == VIDEO_TARGETS and rig.changes == [OUTPUT, OUTPUT]


def test_five_full_width_planes_are_cut_to_three_and_the_cut_are_absent():
    rig = Rig()
    layers = [photo("p0", start=0)] + [photo(f"p{index}", start=index, opacity=0.5) for index in range(1, 5)]
    result = rig.present(*layers)
    assert result.status == "presented"
    assert [key for key, _ in rig.strengths()] == ["p2", "p3", "p4"]
    assert [local.layer.assignment_id for local in result.composition.layers] == ["p2", "p3", "p4"]
    assert set(rig.screens.commits[-1][1]) == {"p2", "p3", "p4"}


def test_no_grant_is_pending_and_commits_nothing():
    rig = Rig(admitted=None)
    assert rig.present(photo("p", start=0)).status == "pending"
    assert rig.screens.commits == [] and rig.fitter.draws == []


def test_a_candidate_grant_commits_the_screens_black_under_the_candidate_tag_once():
    rig = Rig(admitted=False)
    assert rig.present(photo("p", start=0)).status == "pending"
    rig.clock.tick()
    assert rig.present(photo("p", start=0)).status == "pending"
    assert [(commit[0].planes, commit[2], commit[3]) for commit in rig.screens.commits] == [
        ((), f"candidate-{GRANT}", None)]


def test_while_a_commit_is_in_flight_a_present_commits_nothing():
    rig = Rig(answer=False)
    outgoing, incoming = photo("out", start=0, end=200), photo("in", start=100, fade_in=1.0)
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
    result = rig.present(still)
    assert result.presented_at == 0.0 and result.composition.layers[0].layer == still.layer
    assert rig.screens.commits[-1][2] != f"candidate-{GRANT}"
    assert result.composition.layers[0] == replace(still, alpha=1.0)
