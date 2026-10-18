"""The Player's render loop: the planning tick decides what an Output shows; every picture change
is drawn at the time of the frame that shows it (owner, 2026-10-16: "The crossfade between photos
is jittery"; "Don't make a fix specific to fade. Make it a general render loop solution").

A change has two kinds, each with one home. A change over time (a fade, a calibration trial
running out) comes from `player/look.py` `next_change`, and the surface follows its display's
frame clock only while one is due. A change that arrives as an event (a new composition, a
video's new sample, Display Host's edit) queues the next frame's render. A still picture costs
nothing per frame.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from test_executor import Rig, binding, layer

from contracts.models import Calibration
from contracts.node_frame import PictureOperations
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT
from contracts.time import ManualClock, TimeMapping
from player import native as native_module
from player.cache import Cache
from player.executor import TIMEBASE_TOLERANCE, Executor
from player.look import look, next_change
from player.luts import PlayerLuts
from player.mainloop import (
    GDK_PRIORITY_REDRAW,
    REARM,
    WAYLAND_EVENTS,
    DisplayEvents,
    FrameFollower,
    frame_time,
)
from player.native import FOLLOW_LEAD, NativeOutput, NativeRenderer
from player.pacing import Word
from player.plane_screen import FrameGrant
from player.rendering import LocalLayer, OutputComposition, PrepareResult, PresentationResult
from player.visibility import visible_now


def composition(*layers, timebase: float | None = 100.0) -> OutputComposition:
    """`layers` bottom to top on hdmi1; with timebase 100, monotonic 0 is plan time 100."""
    return OutputComposition(binding(), tuple(
        LocalLayer(item, None, item.position(100), item.alpha(100)) for item in layers),
        timebase=timebase)


def black(key: str, **fields):
    return layer(key, presentation="black", variant=None, **fields)


# -- The look at a frame time, and when it next changes over time (pure) -------------------------

def test_a_composition_looks_as_at_the_frame_time_it_is_drawn():
    """Alpha (a fade) is read at the frame time; a video's position is its decoder's sample, never
    a plan-time value of the look; a still composition (a fallback) is drawn as its layers say."""
    fading = composition(black("fade", start=100, end=110, fade_in=2))
    assert look(fading, .5).layers[0].alpha == .25
    assert look(fading, 1.5).layers[0].alpha == .75
    video = composition(layer("video", video=True, start=100, end=200, media_origin=100))
    assert look(video, 1.5).layers[0].position == video.layers[0].position
    kept = composition(black("kept", start=100, end=110, fade_in=2), timebase=None)
    assert look(kept, 1.5) == kept


def test_the_next_change_is_now_while_a_seen_layer_fades_else_the_next_fade_or_expiry():
    """While a seen layer fades the look changes now; a still photo next changes at its fade-out;
    nothing after it changes; a fade beginning at the frame is a change of that frame; a covered
    layer's fade is unseen; a video's pictures are no change over time; a drawn trial's expiry
    is one, the sooner of it and a fade."""
    photo = composition(black("photo", start=100, end=110, fade_in=2, fade_out=2))
    assert next_change(photo, 1) == 1                       # fading in
    assert next_change(photo, 5) == 8                       # still until its fade-out at 108
    assert next_change(photo, 9) == 9                       # fading out
    assert next_change(photo, 10) is None                   # over
    assert next_change(photo, 0) == 0                       # its fade-in begins at this frame
    assert next_change(composition(black("still", start=100, end=110)), 1) is None
    under = black("under", start=100, end=110, fade_in=4)
    assert next_change(composition(under, black("cover", start=100, end=110, priority=1)), 1) is None
    video = layer("video", video=True, start=100, end=200, media_origin=100)
    assert next_change(composition(video), 1) is None
    assert next_change(composition(video), 1, expires=3) == 3
    assert next_change(photo, 5, expires=6) == 6 and next_change(photo, 5, expires=9) == 8
    assert next_change(composition(video, timebase=None), 1) is None


@settings(max_examples=300, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 20), st.integers(1, 20), st.integers(0, 6),
                          st.integers(0, 6), st.sampled_from([1.0, .5])), min_size=1, max_size=4),
       st.floats(-5, 45), st.floats(0, 20))
def test_what_is_seen_is_constant_until_the_next_change(shapes, since, gap):
    """`look` and `next_change` are one model: until the next change, the layers seen and their
    alphas are those of the frame drawn at `since`, whatever the layers' fades and opacities."""
    layers = []
    for index, (start, length, fade_in, fade_out, opacity) in enumerate(shapes):
        length = max(length, fade_in + fade_out)
        layers.append(black(f"l{index}", start=100 + start, end=100 + start + length,
                            fade_in=fade_in, fade_out=fade_out, opacity=opacity, priority=index))
    shown = composition(*layers)
    change = next_change(shown, since)
    if change is None or change > since + gap:
        def seen(at):
            return [(local.layer.assignment_id, local.alpha)
                    for local in visible_now(look(shown, at).layers)]
        assert seen(since) == seen(since + gap)


def test_a_fades_spans_are_where_its_alpha_runs_and_its_opaque_span_between_them():
    """`Layer.fades` is the one home of the fade boundaries: alpha is the layer's opacity between
    them and changes inside them; player/visibility.py's opaque span is the gap between them."""
    from player.visibility import opaque_span
    item = black("fade", start=100, end=110, fade_in=2, fade_out=3)
    assert item.fades() == ((100, 102), (107, 110))
    assert opaque_span(item) == (102, 107)
    assert item.alpha(101) == .5 and item.alpha(104) == 1 and item.alpha(108.5) == .5


# -- The executor hands over a timeline, not values baked at its tick -----------------------------

def test_the_tick_hands_the_renderer_a_composition_it_reads_at_any_frame_time(tmp_path):
    """The executor's composition carries its timebase: read at two later frame times, with no
    tick between, it shows two strengths of its fade (plan time 100 is monotonic 0 here)."""
    rig = Rig(tmp_path)
    rig.play(layer(start=100, end=200, fade_in=4))
    drawn = rig.renderer.outputs["hdmi1"]
    assert drawn.timebase == rig.clock.utc() - rig.clock.monotonic()
    at = rig.clock.monotonic()
    assert look(drawn, at + 1).layers[0].alpha == .25
    assert look(drawn, at + 3).layers[0].alpha == .75
    assert next_change(drawn, at + 1) == at + 1 and next_change(drawn, at + 5) is None


def test_the_executor_re_anchors_its_timeline_only_when_it_really_moved(tmp_path):
    """Each tick re-anchors a healthy clock mapping from two clock readings; a move within
    TIMEBASE_TOLERANCE hands over the same timebase (the renderer compares it by equality), a
    larger one the new timebase."""
    rig = Rig(tmp_path)
    rig.play(layer(start=100, end=200, fade_in=4))
    first = rig.renderer.outputs["hdmi1"].timebase
    rig.clock.step_utc(TIMEBASE_TOLERANCE / 2)
    rig.advance(.033)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].timebase == first
    rig.clock.step_utc(TIMEBASE_TOLERANCE * 2)
    rig.advance(.033)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].timebase == rig.clock.utc() - rig.clock.monotonic()


def test_a_continuation_keeps_the_executors_timebase(tmp_path):
    """When a Frame's replacement fails and the executor keeps its previous composition, that
    continuation still carries the timebase, so its fade keeps running on the frame clock."""
    rig = Rig(tmp_path)

    class FailingNew(type(rig.renderer)):
        def present(self, shown):
            if any(local.layer.assignment_id == "second" for local in shown.layers):
                return PresentationResult("failed", "decode")
            return super().present(shown)

    rig.renderer = rig.executor.renderer = FailingNew()
    rig.play(layer(start=100, end=200, fade_out=50))
    rig.offer(layer(start=100, end=200, fade_out=50),
              layer("second", data=b"second", start=100.5, end=200, priority=1), revision=2)
    rig.secure("second", b"second")
    rig.executor.prepare_imminent()
    rig.commit("picture", "second")
    rig.advance(1)
    rig.executor.tick()
    kept = rig.renderer.outputs["hdmi1"]
    assert [local.layer.assignment_id for local in kept.layers] == ["picture"]
    assert kept.timebase == rig.clock.utc() - rig.clock.monotonic()
    at = rig.clock.monotonic()
    assert look(kept, at + 99).layers[0].alpha < look(kept, at + 74).layers[0].alpha


def test_a_mapping_gone_unhealthy_keeps_its_anchor_through_a_utc_step(tmp_path):
    """Anchored while healthy; once the mapping ages out a UTC step is not a new timeline: the
    executor keeps handing over the anchor it took while the mapping was healthy."""
    clock = ManualClock(100)
    mapping = TimeMapping(clock)
    mapping.establish(.01)
    executor = Executor("player1", Cache(tmp_path / "cache", 4096), Rig(tmp_path / "r").renderer,
                        clock, mapping)
    anchored = executor._timebase()
    clock.advance(mapping.max_age + 1)
    assert not mapping.healthy()
    clock.step_utc(5)
    assert executor._timebase() == anchored


def test_an_unanchored_clock_mapping_hands_over_utc_less_monotonic(tmp_path):
    """Never healthy, so nothing is anchored: the timebase is this moment's UTC less monotonic."""
    clock = ManualClock(100)
    clock.step_utc(5)
    clock.advance(3)
    executor = Executor("player1", Cache(tmp_path / "cache", 4096), Rig(tmp_path / "r").renderer,
                        clock, TimeMapping(clock))
    assert executor._timebase() == clock.utc() - clock.monotonic()


# -- The frame clock (player/mainloop.py) --------------------------------------------------------

class Area:
    """A GtkGLArea as _render and FrameFollower use it: its frame clock, its tick callbacks."""

    def __init__(self, frame_clock: bool = True):
        self.renders = 0
        self.callbacks: dict[int, object] = {}
        self.frame_us = 0
        self.frame_clock = frame_clock
        self.added = 0
        self.painted = 0

    def queue_render(self):
        self.renders += 1

    def add_tick_callback(self, callback):
        self.added += 1
        self.callbacks[self.added] = callback
        return self.added

    def remove_tick_callback(self, key):
        del self.callbacks[key]

    def get_frame_clock(self):
        if not self.frame_clock:
            return None
        return SimpleNamespace(get_frame_time=lambda: self.frame_us)

    def make_current(self):
        pass

    def get_error(self):
        return None

    def get_allocated_width(self):
        return 64

    def get_allocated_height(self):
        return 36

    def get_scale_factor(self):
        return 1

    def frame(self, seconds: float) -> int:
        """The frame clock reaches `seconds` (monotonic): every tick callback runs, as GDK's
        update phase does, and one returning False is removed. Answers how many ran."""
        self.frame_us = round(seconds * 1e6)
        clock = self.get_frame_clock()
        ran = 0
        for key, callback in list(self.callbacks.items()):
            ran += 1
            if not callback(self, clock):
                del self.callbacks[key]
        return ran


class GLib:
    """GLib's monotonic time, fd watches and idles, recorded rather than run."""
    IOCondition = SimpleNamespace(IN=1)

    def __init__(self):
        self.watches: dict[int, tuple[int, int, object]] = {}
        self.idles: dict[int, tuple[int, object]] = {}
        self.added = 0

    def unix_fd_add_full(self, priority, fd, condition, callback):
        assert condition == self.IOCondition.IN
        self.added += 1
        self.watches[self.added] = (priority, fd, callback)
        return self.added

    def idle_add(self, callback, priority):
        self.added += 1
        self.idles[self.added] = (priority, callback)
        return self.added

    def readable(self, fd, *, idle: bool = True) -> None:
        """`fd` has input: each watch on it runs, as the main loop's dispatch would; then, unless
        `idle` is False (a higher-priority source stays ready), the idles queued run."""
        for key, (_priority, watched, callback) in list(self.watches.items()):
            if watched == fd and not callback(fd, self.IOCondition.IN):
                del self.watches[key]
        if idle:
            for key, (_priority, callback) in list(self.idles.items()):
                del self.idles[key]
                callback()

    @staticmethod
    def get_monotonic_time():
        return round(time.monotonic() * 1e6)

    def source_remove(self, key):
        if self.watches.pop(key, None) is None:
            self.idles.pop(key)


def test_frame_time_maps_gdk_microseconds_onto_the_monotonic_clock():
    glib = SimpleNamespace(get_monotonic_time=lambda: 5_000_000)
    clock = SimpleNamespace(get_frame_time=lambda: 4_750_000)
    assert frame_time(glib, clock, lambda: 1000.0) == 999.75


def test_a_follower_runs_on_each_frame_until_its_frame_says_stop():
    area, seen = Area(), []
    follower = FrameFollower(GLib(), area,
                             lambda at: seen.append(round(at, 3)) or len(seen) < 2)
    follower.follow()
    follower.follow()                       # once only
    assert len(area.callbacks) == 1 and follower.following
    now = time.monotonic()
    area.frame(now)
    area.frame(now + 1 / 60)
    assert seen == [round(now, 3), round(now + 1 / 60, 3)]
    assert not area.callbacks and not follower.following
    follower.follow()
    follower.stop()
    assert not area.callbacks and not follower.following


def test_a_follower_whose_frame_raises_stops_following():
    area = Area()

    def frame(_at):
        raise RuntimeError("frame")

    follower = FrameFollower(GLib(), area, frame)
    follower.follow()
    with pytest.raises(RuntimeError):
        area.frame(time.monotonic())
    assert not follower.following
    follower.follow()
    assert follower.following, "a later follow() installs it again"


# -- The native renderer's render loop -----------------------------------------------------------

class GL:
    """Enough OpenGL for _render and the fit pass, every other call a no-op. It records, per draw,
    the program drawn with: a fit pass (program 1, `fits`: the framebuffer it drew into) or a
    frame pass (program 2, `passes`, and `opacities`: each of its layers' strengths, bottom to
    top). Names are fresh integers and every framebuffer complete. A fence is passed at once,
    unless `held`: then it stays in flight (the GPU still drawing) until `signal()`. With
    `failing_fits` a fit pass reports GL_OUT_OF_MEMORY."""
    GL_NO_ERROR = 0
    GL_OUT_OF_MEMORY = 0x0505
    GL_ALREADY_SIGNALED, GL_TIMEOUT_EXPIRED, GL_CONDITION_SATISFIED = 0x911A, 0x911B, 0x911C

    def __init__(self):
        self.opacities: list[float] = []
        self.fits: list[int] = []
        self.passes = 0
        self.program = self.framebuffer = 0
        self.names = 100
        self.count = 0
        self.alphas: list[float] = []
        self.held = False
        self.in_flight: set[int] = set()
        self.failing_fits = False
        self.error = 0

    def glFenceSync(self, *_args):
        fence = self._name()
        if self.held:
            self.in_flight.add(fence)
        return fence

    def signal(self) -> None:
        """The GPU passes every fence issued so far."""
        self.in_flight.clear()

    def __getattr__(self, name):
        if name.startswith("GL_"):
            return 1
        return lambda *_args: None

    def _name(self, *_args):
        self.names += 1
        return self.names

    glGenTextures = glGenFramebuffers = glGenVertexArrays = _name

    def glCheckFramebufferStatus(self, _target):
        return self.GL_FRAMEBUFFER_COMPLETE

    def glClientWaitSync(self, fence, _flags, _timeout):
        return self.GL_TIMEOUT_EXPIRED if fence in self.in_flight else self.GL_ALREADY_SIGNALED

    def glBindFramebuffer(self, _target, framebuffer):
        self.framebuffer = framebuffer

    def glUseProgram(self, program):
        self.program = program

    def glGetUniformLocation(self, _program, name):
        return name

    def glUniform1i(self, location, value):
        if location == "layer_count":
            self.count = value

    def glUniform1fv(self, location, _count, values):
        if location == "layer_alpha":
            self.alphas = list(values)

    def glDrawArrays(self, *_args):
        if self.program == 1:
            self.fits.append(self.framebuffer)
            if self.failing_fits:
                self.error = self.GL_OUT_OF_MEMORY
        else:
            self.passes += 1
            self.opacities.extend(round(alpha, 3) for alpha in self.alphas[:self.count])

    def glGetIntegerv(self, _name):
        return 0

    def glGetError(self):
        error, self.error = self.error, 0
        return error

    def glGetString(self, _name):
        return b"fake"


def decoder(item, *, prepared=True):
    """A resident decoder as present() and _render read it; `sample_serial` is its newest sample."""
    return SimpleNamespace(incarnation=1, generation=1, sample_serial=1,
                           playing=bool(item.variant.duration),
                           prepared=prepared, failure=None, sample=None,
                           local=SimpleNamespace(layer=item))


def frame_renderer(decoders=None, area=None):
    """A NativeRenderer with one realized surface, faked GL and GTK, and real present, prepare's
    fitted pictures, _render, _frame and FrameFollower. Every layer decodes at once; decoded
    pictures upload as texture 7 and read no new sample."""
    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._owner, renderer._closed, renderer._serial = threading.get_ident(), False, 0
    renderer._luts, renderer._frames, renderer.GL, renderer.GLib = PlayerLuts(), None, GL(), GLib()
    renderer._room_lights = None
    renderer.gl_renderer, renderer.texture_budget = "fake", 1 << 30
    renderer._decoders = decoders or {}
    area = area or Area()
    window = SimpleNamespace(destroyed=False, get_window=lambda: None)
    window.destroy = lambda: setattr(window, "destroyed", True)
    surface = native_module._Surface(NativeOutput("hdmi1", "photo-wall-hdmi1"), window, area,
                                     programs=(1, 2), size=(64, 36), vao=1)
    surface.follower = FrameFollower(renderer.GLib, area, lambda at: renderer._frame(surface, at))
    renderer._surfaces = {"hdmi1": surface}
    renderer.capacity = lambda compositions: SimpleNamespace(available=True)
    renderer._decode = lambda local: PrepareResult("prepared")
    renderer._program = lambda _source: 2           # every step set's frame program
    renderer._upload = lambda _surface, _local: 7
    renderer._read_sample = lambda _decoder: None
    return renderer, surface, area


def paint(renderer, surface, area, at: float) -> None:
    """GDK's frame at `at`: tick callbacks first (the update phase), then the GLArea's render
    when one was queued since the last paint."""
    area.frame(at)
    if area.renders > area.painted:
        area.painted = area.renders
        renderer._render(area, None, surface)


@pytest.fixture
def clock(monkeypatch):
    """The renderer's monotonic clock, set by the test (`clock.now`)."""
    fake = SimpleNamespace(now=time.monotonic())
    monkeypatch.setattr(native_module, "time", SimpleNamespace(monotonic=lambda: fake.now))
    return fake


def test_a_fade_is_drawn_at_each_frames_time_with_no_tick_between():
    """One present() (one planning tick); then the frame clock alone draws the fade, a new
    strength each frame, the last at full strength, and stops following once nothing changes."""
    renderer, surface, area = frame_renderer()
    start = time.monotonic()
    fading = composition(black("fade", start=100, end=200, fade_in=1), timebase=100 - start)
    assert renderer.present(fading).status == "pending" and area.renders == 1
    paint(renderer, surface, area, start + .25)
    assert renderer.GL.opacities == [.25]
    assert surface.follower.following, "a changing look follows the frame clock"
    for at in (start + .5, start + .75, start + 1.25):
        paint(renderer, surface, area, at)
    assert renderer.GL.opacities == [.25, .5, .75, 1.0] and area.renders == 4
    assert area.frame(start + 1.5) == 1 and not area.callbacks, "a still picture is not followed"
    renderer._render(area, None, surface)           # GTK repaints: nothing new is drawn
    assert renderer.GL.opacities == [.25, .5, .75, 1.0]
    assert renderer.present(fading).status == "presented"


def test_a_still_picture_installs_no_frame_callback_after_its_draw():
    """A still photo is drawn once on the frame after its present(); no frame callback is ever
    installed for it, so nothing runs per frame however many frames pass."""
    renderer, surface, area = frame_renderer()
    start = time.monotonic()
    still = composition(black("still", start=100, end=200), timebase=100 - start)
    renderer.present(still)
    paint(renderer, surface, area, start + .1)
    assert renderer.GL.opacities == [1.0] and area.renders == 1
    assert not area.callbacks and area.added == 0
    for index in range(1, 30):
        paint(renderer, surface, area, start + .1 + index / 60)
        renderer.present(still)                     # each planning tick
    assert renderer.GL.opacities == [1.0] and area.renders == 1 and area.added == 0


def test_a_fade_far_ahead_is_woken_by_the_tick_and_drawn_on_the_frame_clock(clock):
    """A still photo whose fade-out is seconds away installs no frame callback (each tick's
    renewal draw included); the planning tick that finds it within FOLLOW_LEAD follows the frame
    clock, which draws the fade-out at its frames' times."""
    renderer, surface, area = frame_renderer()
    start = clock.now
    photo = composition(black("photo", start=100, end=110, fade_out=2), timebase=100 - start)
    for after in (0, 2, 4, 6, 7.8):                 # ticks, each followed by a frame
        clock.now = start + after
        renderer.present(photo)
        paint(renderer, surface, area, start + after)
        assert not area.callbacks and area.added == 0
    clock.now = start + 8 - FOLLOW_LEAD / 2
    renderer.present(photo)
    assert surface.follower.following
    drawn = len(renderer.GL.opacities)
    paint(renderer, surface, area, start + 8 - FOLLOW_LEAD / 4)
    assert len(renderer.GL.opacities) == drawn, "followed ahead of the change, drawn from it"
    paint(renderer, surface, area, start + 9)
    paint(renderer, surface, area, start + 9.5)
    assert renderer.GL.opacities[drawn:] == [.5, .25]


def test_a_video_is_drawn_once_per_new_sample_not_per_refresh():
    """A playing video installs no frame callback: refreshes with no new sample draw nothing and
    call nothing. Each sample its decoder brings hops to the main loop once and is drawn on the
    next frame, with no tick; a covered video's sample draws nothing."""
    video = layer("video", video=True, start=100, end=200, media_origin=100)
    playing = decoder(video)
    renderer, surface, area = frame_renderer({"video": playing})
    start = time.monotonic()
    renderer.present(composition(video, timebase=100 - start))
    paint(renderer, surface, area, start + .1)
    assert area.renders == 1 and renderer.GL.opacities == [1.0]
    for index in range(1, 4):                       # 60 Hz refreshes, no new sample
        paint(renderer, surface, area, start + .1 + index / 60)
    assert area.renders == 1 and not area.callbacks and area.added == 0
    fits = len(renderer.GL.fits)
    playing.sample_serial += 1                      # the sample _read_sample takes
    renderer._sample_arrived("video", playing.incarnation)
    paint(renderer, surface, area, start + .1 + 4 / 60)
    assert area.renders == 2 and renderer.GL.opacities == [1.0, 1.0]
    assert len(renderer.GL.fits) == fits + 1, "its fitted picture is drawn again with the sample"
    paint(renderer, surface, area, start + .1 + 5 / 60)
    assert area.renders == 2, "drawn once per sample"
    renderer._sample_arrived("video", playing.incarnation + 1)     # a replaced decoder's
    assert area.renders == 2

    cover = black("cover", start=100, end=200, priority=1)
    paused = decoder(video)
    paused.playing = False
    covered, covered_surface, covered_area = frame_renderer({"video": paused})
    covered.present(composition(video, cover, timebase=100 - start))
    paint(covered, covered_surface, covered_area, start + .1)
    covered._sample_arrived("video", 1)
    assert covered_area.renders == 1


def test_a_re_anchor_within_tolerance_draws_nothing_and_a_real_one_redraws(tmp_path):
    """The executor's ticks with a clock mapping that moved by microseconds hand the renderer the
    same timeline: no new draw. A real re-anchor is a new timeline, drawn on the next frame."""
    rig = Rig(tmp_path)
    rig.play(layer(start=100, end=200))
    picture = rig.renderer.outputs["hdmi1"].layers[0].layer
    renderer, surface, area = frame_renderer({"picture": decoder(picture)})
    renderer.present(rig.renderer.outputs["hdmi1"])
    paint(renderer, surface, area, time.monotonic())
    assert area.renders == 1 and renderer.GL.opacities == [1.0]
    for _ in range(3):
        rig.clock.step_utc(TIMEBASE_TOLERANCE / 4)
        rig.advance(.033)
        rig.executor.tick()
        renderer.present(rig.renderer.outputs["hdmi1"])
    assert area.renders == 1 and not area.callbacks
    rig.clock.step_utc(.5)
    rig.advance(.033)
    rig.executor.tick()
    renderer.present(rig.renderer.outputs["hdmi1"])
    assert area.renders == 2


def test_a_layer_seen_only_at_a_later_frame_waits_for_its_decode_and_does_not_fail(clock):
    """A photo fading in whose first draw had alpha 0 becomes visible at a later frame time
    before its decoder holds a picture: that frame draws nothing new and the decoder is not
    failed. Frames cannot decode it, so the surface stops following its frame clock (no render
    per refresh); the next planning tick, whose prepare() reads the decoder, wakes it and the
    next frame draws it."""
    base = black("base", start=100, end=200)
    photo = layer("photo", start=101, end=200, fade_in=1, priority=1)
    decoding = decoder(photo, prepared=False)
    renderer, surface, area = frame_renderer({"photo": decoding})
    start = clock.now
    shown = composition(base, photo, timebase=100 - start)
    renderer.present(shown)
    paint(renderer, surface, area, start + .5)
    assert renderer.GL.opacities == [1.0]           # the base; the photo has alpha 0
    clock.now = start + .95
    renderer.present(shown)                         # the tick before its fade-in wakes the clock
    paint(renderer, surface, area, start + 1.5)
    assert renderer.GL.opacities == [1.0] and decoding.failure is None
    renders = area.renders
    for index in range(1, 30):                      # half a second of refreshes, no tick
        paint(renderer, surface, area, start + 1.5 + index / 60)
    assert area.renders == renders and not area.callbacks, "not rendered per refresh"
    decoding.prepared = True
    clock.now = start + 1.98
    renderer.present(shown)                         # the tick: its prepare() read the decode
    paint(renderer, surface, area, start + 2)
    assert renderer.GL.opacities == [1.0, 1.0, 1.0]


def test_a_pending_draw_not_decoded_for_its_frame_waits_and_is_drawn_at_its_frames_look():
    """present() made a draw, but at the frame's time a layer it shows is not decoded yet: the
    frame draws nothing (never the tick's look) and the surface waits, unfollowed; the frame
    after the next tick (whose prepare() read the decode) draws it at that frame's look."""
    base = black("base", start=100, end=200)
    photo = layer("photo", start=99.5, end=200, fade_in=1, priority=1)
    decoding = decoder(photo, prepared=False)
    renderer, surface, area = frame_renderer({"photo": decoding})
    start = time.monotonic()
    renderer.present(composition(base, photo, timebase=100 - start))
    paint(renderer, surface, area, start + .25)
    assert renderer.GL.opacities == [] and surface.pending is not None
    assert not surface.follower.following
    decoding.prepared = True
    renderer.present(composition(base, photo, timebase=100 - start))
    paint(renderer, surface, area, start + .3)
    assert renderer.GL.opacities == [1.0, .8]


def test_a_draw_whose_decoder_was_sought_is_not_redrawn_on_the_frame_clock():
    """A decoder sought or replaced since the draw (a new generation) holds another picture: the
    frame clock does not draw the old draw again; present() owes the next one."""
    photo = layer("photo", start=99.5, end=200, fade_in=1)
    held = decoder(photo)
    renderer, surface, area = frame_renderer({"photo": held})
    start = time.monotonic()
    renderer.present(composition(photo, timebase=100 - start))
    paint(renderer, surface, area, start + .25)
    assert renderer.GL.opacities == [.75]
    paint(renderer, surface, area, start + .3)
    assert renderer.GL.opacities == [.75, .8] and area.renders == 2
    held.generation += 1
    paint(renderer, surface, area, start + .4)
    assert renderer.GL.opacities == [.75, .8]


class Frames:
    """Display Host faked: one admitted grant, a calibration trial at `sequence` (None: none) that
    runs out at `expires` (monotonic), and the Output's picture operations."""

    def __init__(self):
        self.grant_ = FrameGrant(grant_id=uuid.uuid4(), binding_generation=1, config_revision=1,
                                 frame_id="frame1", admitted=True)
        self.trial_id = uuid.uuid4()
        self.sequence: int | None = 1
        self.expires = time.monotonic() + 3600
        self.trials = 0
        self.tags: list[str] = []
        self.operations: PictureOperations | None = None
        self.picture_changes: list[int | None] = []

    def grant(self, _output_id, _composition=None):
        return self.grant_

    def trial(self, _composition, _grant):
        self.trials += 1
        if self.sequence is None:
            return None
        # Under the `clock` fixture the renderer reads no CLOCK_BOOTTIME: boot time is monotonic.
        return SimpleNamespace(
            candidate=SimpleNamespace(trial_id=self.trial_id, generation=1, sequence=self.sequence,
                                      candidate_sha256="0" * 64,
                                      expires_boottime_ms=round(self.expires * 1000)),
            calibration=Calibration(), lighting=None, primitives=None)

    def picture(self, _output_id):
        return self.operations

    def tag_rendered_buffer(self, _window, _grant, tag, _primitives=None, *, picture_change=None):
        self.tags.append(tag)
        self.picture_changes.append(picture_change)

    def close(self):
        pass


def operations(change: int, contrast: float = 1.0) -> PictureOperations:
    return PictureOperations("hdmi1", change, (1.0, 1.0, 1.0), contrast, 1.0, UPGRADE_DISPLAY_LIGHT)


def test_a_slider_edit_between_ticks_is_drawn_on_the_next_frame():
    """A still photo is drawn once and not followed. A Live slider edit (new picture operations)
    arrives as Display Host's event: the next frame draws it, with no planning tick between, and
    its tag names the edit. An event that changes nothing draws nothing."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Frames()
    frames.sequence = None                          # no calibration trial drawn
    frames.operations = operations(1)
    renderer._display_events = DisplayEvents(renderer.GLib, 9, renderer._arrived)
    start = time.monotonic()
    renderer.present(composition(black("still", start=100, end=200), timebase=100 - start))
    paint(renderer, surface, area, start + .1)
    assert renderer.GL.opacities == [1.0] and area.renders == 1
    assert frames.picture_changes[-1] == 1 and not area.callbacks

    renderer.GLib.readable(9)                       # an event that changes nothing on screen
    assert area.renders == 1 and not area.callbacks

    frames.operations = operations(2, contrast=.5)  # the owner moves a slider
    renderer.GLib.readable(9)
    renderer.GLib.readable(9)                       # another event before the frame
    assert area.renders == 2, "the edit queues the next frame, once"
    paint(renderer, surface, area, start + .1 + 2 / 60)
    assert renderer.GL.opacities == [1.0, 1.0]
    assert frames.picture_changes[-1] == 2, "the tag names the edit the buffer shows"
    renderer.GLib.readable(9)
    paint(renderer, surface, area, start + .1 + 3 / 60)
    assert renderer.GL.opacities == [1.0, 1.0] and not area.callbacks

    renderer.close()
    assert not renderer.GLib.watches


def test_a_trial_is_never_polled_per_frame_its_edit_is_drawn_next_frame_and_its_end_at_expiry(
        clock):
    """A drawn calibration trial installs no frame callback until its expiry comes near: frames
    read nothing from Display Host. An edit arrives as an event and is drawn on the next frame.
    The trial's running out is a change over time, drawn on the frame at its expiry."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Frames()
    renderer._display_events = DisplayEvents(renderer.GLib, 9, renderer._arrived)
    start = clock.now
    frames.expires = start + .5
    still = composition(black("still", start=100, end=200), timebase=100 - start)
    renderer.present(still)
    paint(renderer, surface, area, start + .1)
    assert surface.acknowledged.applied_calibration.trial_sequence == 1
    assert not area.callbacks
    read = frames.trials
    for index in range(1, 4):
        area.frame(start + .1 + index / 60)
    assert frames.trials == read and area.renders == 1, "no per-frame polling"

    frames.sequence = 2                             # the owner edits the trial
    renderer.GLib.readable(9)
    paint(renderer, surface, area, start + .2)
    assert area.renders == 2 and surface.acknowledged.applied_calibration.trial_sequence == 2

    clock.now = start + .45                         # the tick before the expiry wakes the clock
    renderer.present(still)
    assert surface.follower.following and area.renders == 2
    frames.sequence = None                          # Display Host's trial state at its expiry
    paint(renderer, surface, area, start + .48)
    assert area.renders == 2
    paint(renderer, surface, area, start + .501)     # the first frame at its expiry (in ms)
    assert area.renders == 3 and surface.acknowledged.applied_calibration.trial_id is None
    area.frame(start + .5 + 1 / 60)
    assert not area.callbacks


def test_a_draw_whose_grant_was_withdrawn_before_its_frame_is_dropped_undrawn():
    """present() made a draw under an admitted grant; Display Host withdrew the grant for that
    composition before the frame: the frame draws nothing and the draw is dropped (the next
    present() decides again)."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Frames()
    frames.sequence = None
    start = time.monotonic()
    renderer.present(composition(black("still", start=100, end=200), timebase=100 - start))
    exact = frames.grant
    frames.grant = lambda output_id, shown=None: None if shown is not None else exact(output_id)
    paint(renderer, surface, area, start + .1)
    assert renderer.GL.opacities == [] and surface.pending is None
    assert surface.acknowledged is None and not surface.failed()


def test_a_trials_end_on_boot_time_is_drawn_at_its_expiry_on_the_monotonic_clock(monkeypatch):
    """Display Host's trial runs out on CLOCK_BOOTTIME, ahead of the monotonic clock by the time
    the machine was suspended: the drawn trial ends at its expiry less that lead."""
    now, ahead = 1000.0, 250.0
    monkeypatch.setattr(native_module, "time", SimpleNamespace(
        monotonic=lambda: now, CLOCK_BOOTTIME=7,
        clock_gettime=lambda which: now + ahead if which == 7 else math.nan))
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Frames()
    frames.expires = now + ahead + .5               # boot time, as Display Host writes it
    renderer.present(composition(black("still", start=100, end=200), timebase=100 - now))
    paint(renderer, surface, area, now + .1)
    assert surface.acknowledged.applied_calibration.trial_sequence == 1
    assert surface.acknowledged.expires == pytest.approx(now + .5)


def test_close_removes_the_frame_callback():
    renderer, surface, area = frame_renderer()
    start = time.monotonic()
    renderer.present(composition(black("fade", start=100, end=200, fade_in=1),
                                 timebase=100 - start))
    paint(renderer, surface, area, start + .25)
    assert surface.follower.following
    renderer.close()
    assert not area.callbacks and not surface.follower.following and surface.window.destroyed


def test_a_surface_with_no_frame_clock_draws_at_now():
    renderer, surface, area = frame_renderer(area=Area(frame_clock=False))
    before = time.monotonic()
    assert before <= renderer._frame_time(area) <= time.monotonic()


def test_display_events_call_once_per_readable_wake_and_re_arm_behind_gdks_paint():
    """The watch sits on the display's socket at GDK's event priority (never above it: GDK's
    check reads the socket), calls `arrived` once per readable wake and then waits for an idle
    below GDK's paint before it watches again: while GDK pauses its events nothing reads the
    socket, and a watch still armed at this priority would starve the paint forever. It survives
    a raising `arrived`, and stop() removes the watch or the idle, whichever is armed."""
    glib, arrivals = GLib(), []

    def arrived():
        arrivals.append(1)
        if len(arrivals) == 2:
            raise RuntimeError("arrived")

    with pytest.raises(ValueError):
        DisplayEvents(glib, 9, arrived, priority=WAYLAND_EVENTS - 1)
    events = DisplayEvents(glib, 9, arrived)
    assert [(priority, fd) for priority, fd, _ in glib.watches.values()] == [(WAYLAND_EVENTS, 9)]
    for _ in range(5):                              # paused: the paint has not run
        glib.readable(9, idle=False)
    assert len(arrivals) == 1 and not glib.watches, "disarmed until the paint has run"
    assert [priority for priority, _ in glib.idles.values()] == [REARM]
    assert REARM > GDK_PRIORITY_REDRAW
    glib.readable(9)                                # the paint ran: the idle re-armed the watch
    glib.readable(9)
    glib.readable(9)
    assert len(arrivals) == 3 and glib.watches, "a raising arrival keeps the watch"
    glib.readable(9, idle=False)
    events.stop()
    events.stop()
    assert not glib.watches and not glib.idles


# -- Render only what reaches the screen (owner, 2026-10-17: "The render loop should not fire if
# it there is nothing to render.") ---------------------------------------------------------------

REFRESH = 1 / 60


class Compositor(Frames):
    """Display Host with Weston's presentation feedback faked: a tagged commit made at frame time
    t turns to light at the first vblank after t + `cost` (vblanks every REFRESH from 0); the
    frame client's word for the newest commit answered is readable once that time has passed."""

    def __init__(self, clock, cost: float = .067):
        super().__init__()
        self.sequence = None                        # no calibration trial
        self.clock, self.cost = clock, cost
        self.commits: list[tuple[float, float]] = []   # (made at, presented at)

    def tag_rendered_buffer(self, window, grant, tag, primitives=None, *, picture_change=None):
        super().tag_rendered_buffer(window, grant, tag, primitives, picture_change=picture_change)
        made = self.clock.now
        self.commits.append((made, math.ceil((made + self.cost) / REFRESH - 1e-9) * REFRESH))
        return len(self.commits)

    def presentation(self, _window, now):
        answered = [index for index, (_, at) in enumerate(self.commits) if at <= now]
        if not answered:
            return None
        return Word(answered[-1] + 1, self.commits[answered[-1]][1], REFRESH)


def frames_at(clock, renderer, surface, area, start: float, seconds: float, *, shown=None):
    """`seconds` of 60 Hz frames from `start` (each 4 ms after a vblank), with a planning tick
    (present(shown)) every other frame when `shown` is given."""
    for index in range(round(seconds / REFRESH)):
        clock.now = start + .004 + index * REFRESH
        if shown is not None and index % 2 == 0:
            renderer.present(shown)
        paint(renderer, surface, area, clock.now)


def test_a_still_minute_draws_copies_and_commits_nothing(clock):
    """A still photo is drawn and committed once. A minute of planning ticks (every 33 ms) and
    60 Hz frames after it installs no frame callback, queues no render (so GTK copies nothing),
    runs no GL pass and makes no commit, and every present() reads "presented"."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Compositor(clock)
    start = round(clock.now)
    still = composition(black("still", start=100, end=10_000), timebase=100 - start)
    frames_at(clock, renderer, surface, area, start, .5, shown=still)
    drawn = (area.renders, renderer.GL.passes, len(frames.commits))
    assert drawn == (1, 1, 1)
    statuses = set()
    for index in range(round(60 / REFRESH)):
        clock.now = start + .5 + .004 + index * REFRESH
        if index % 2 == 0:
            statuses.add(renderer.present(still).status)
        paint(renderer, surface, area, clock.now)
    assert (area.renders, renderer.GL.passes, len(frames.commits)) == drawn
    assert not area.callbacks and area.added == 0
    assert statuses == {"presented"}


def test_a_fade_draws_each_frame_only_after_the_last_was_presented_on_a_steady_beat(clock):
    """A two-second fade whose frames cost about 67 ms to reach the screen (the 4K Pi): no frame
    is drawn while the last commit has no word from the compositor (none is replaced unseen), and
    after the first the frames land every 5th refresh, steady. Then the still end costs nothing."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Compositor(clock)
    start = round(clock.now)
    fading = composition(black("fade", start=100, end=10_000, fade_in=2), timebase=100 - start)
    frames_at(clock, renderer, surface, area, start, 3, shown=fading)
    made = [commit[0] for commit in frames.commits]
    shown_at = [commit[1] for commit in frames.commits]
    assert all(made[index] >= shown_at[index - 1] for index in range(1, len(made))), \
        "a frame drawn before the last was presented"
    beat = {round((b - a) / REFRESH) for a, b in zip(shown_at[1:], shown_at[2:]) if b < start + 2}
    assert beat == {5}, beat
    assert renderer.GL.opacities[-1] == 1.0
    commits = len(frames.commits)
    frames_at(clock, renderer, surface, area, start + 3, 2, shown=fading)
    assert len(frames.commits) == commits and not area.callbacks


def test_a_slider_edit_while_a_frame_is_in_flight_is_drawn_on_the_frame_after_its_word(clock):
    """Display Host's edit arrives while the last frame has no word yet: the surface follows its
    frame clock and draws the edit on the first frame after that frame was presented."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Compositor(clock)
    renderer._display_events = DisplayEvents(renderer.GLib, 9, renderer._arrived)
    frames.operations = operations(1)
    start = round(clock.now)
    still = composition(black("still", start=100, end=10_000), timebase=100 - start)
    clock.now = start + .004
    renderer.present(still)
    paint(renderer, surface, area, clock.now)
    first_light = frames.commits[0][1]
    frames.operations = operations(2, contrast=.5)
    clock.now = start + .004 + REFRESH
    renderer.GLib.readable(9)
    assert surface.follower.following and area.renders == 1, "held: the first frame is in flight"
    frames_at(clock, renderer, surface, area, start + REFRESH, .2)
    assert len(frames.commits) == 2 and frames.picture_changes[-1] == 2
    assert first_light <= frames.commits[1][0] < first_light + REFRESH
    assert not area.callbacks


def test_the_first_present_after_admission_draws_with_no_renewal(clock):
    """Before admission the candidate colour is drawn on every planning tick; those renders set
    the surface up (its programs and size, which capacity() needs), so the first present() after
    admission asks for its draw and the next frame draws it. Nothing renews a frame, so nothing
    else could set it up (the display harness's picture leg found this)."""
    renderer, surface, area = frame_renderer()
    del renderer.capacity                           # the real capacity(): it reads the setup
    renderer.decoder_limit = 4
    surface.programs, surface.size = None, (0, 0)
    renderer._frames = frames = Compositor(clock)
    admitted = frames.grant_
    frames.grant_ = replace(admitted, admitted=False)
    start = round(clock.now)
    still = composition(black("still", start=100, end=10_000), timebase=100 - start)
    frames_at(clock, renderer, surface, area, start, .2, shown=still)
    assert renderer.GL.passes == 0 and frames.tags
    assert all(tag.startswith("candidate-") for tag in frames.tags)
    frames.grant_ = admitted
    frames_at(clock, renderer, surface, area, start + .2, .3, shown=still)
    assert renderer.GL.passes == 1 and renderer.present(still).status == "presented"


@pytest.mark.parametrize("fps", [24, 25, 30])
def test_a_video_draws_every_sample_with_presentation_feedback(clock, fps):
    """A video's samples arrive as events between frames, so nobody follows the frame clock and
    each word is read only at the next sample: that wait is idle time, never a cost the pacer
    learns. Three seconds of 24, 25 or 30 fps video costing 10 ms a frame draw every sample."""
    video = layer("video", video=True, start=100, end=10_000, media_origin=100)
    playing = decoder(video)
    renderer, surface, area = frame_renderer({"video": playing})
    renderer._frames = Compositor(clock, cost=.01)
    start = round(clock.now)
    clock.now = start + .004
    renderer.present(composition(video, timebase=100 - start))
    paint(renderer, surface, area, clock.now)
    samples = [start + .01 + index / fps for index in range(3 * fps)]
    expected = set(range(2, len(samples) + 2))
    drawn, tick, end = set(), start + .004, samples[-1] + .2
    while tick < end:
        tick += REFRESH
        while samples and samples[0] <= tick:
            clock.now = samples.pop(0)
            playing.sample_serial += 1
            renderer._sample_arrived("video", playing.incarnation)
        clock.now = tick
        passes = renderer.GL.passes
        paint(renderer, surface, area, tick)
        if renderer.GL.passes > passes:
            drawn.add(playing.sample_serial)
    assert expected <= drawn, sorted(expected - drawn)


@pytest.mark.parametrize("pause", [.3, .6])
@pytest.mark.parametrize(("cost", "within"), [(.01, 1), (.03, 3)])
def test_a_slider_drag_after_a_pause_draws_each_edit_on_the_next_frame_it_may(
        clock, pause, cost, within):
    """A still photo, then a pause shorter than the pacer's window, then the owner drags a slider
    (an edit every 50 ms): the still's word sat unread through the pause, which the pacer never
    takes for latency, so every edit is drawn within the frames its cost needs (one refresh at
    10 ms a frame, three at 30 ms), never held for the length of the pause."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Compositor(clock, cost=cost)
    renderer._display_events = DisplayEvents(renderer.GLib, 9, renderer._arrived)
    frames.operations = operations(1)
    start = round(clock.now)
    clock.now = start + .004
    renderer.present(composition(black("still", start=100, end=10_000), timebase=100 - start))
    paint(renderer, surface, area, clock.now)
    edits = [start + pause + index * .05 for index in range(10)]
    waiting, delays, change, tick, end = [], [], 1, start + .004, edits[-1] + .5
    while tick < end:
        tick += REFRESH
        while edits and edits[0] <= tick:
            clock.now = edits.pop(0)
            change += 1
            frames.operations = operations(change, contrast=1 - change / 100)
            waiting.append((clock.now, change))
            renderer.GLib.readable(9)
        clock.now = tick
        commits = len(frames.commits)
        paint(renderer, surface, area, tick)
        if len(frames.commits) > commits:
            drawn = frames.picture_changes[-1]
            delays += [tick - at for at, edit in waiting if edit <= drawn]
            waiting = [(at, edit) for at, edit in waiting if edit > drawn]
    assert len(delays) == 10 and not waiting
    assert max(delays) < within * REFRESH + 1e-6, [round(d * 1000) for d in delays]


def test_before_admission_only_display_hosts_colour_is_drawn_and_only_as_often_as_handoff_needs(
        clock):
    """Before admission the planning tick (every 33 ms) asks for no render on its own. A candidate
    grant's colour is drawn when the grant comes and once a second after (the handoff needs a
    presentation newer than the shell's 5 s lease); with no grant the Output is cleared once and
    nothing more; an admitted grant for an older revision of the composition keeps its buffer
    (no render at all until the new revision's grant comes)."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Compositor(clock)
    admitted = frames.grant_
    frames.grant_ = replace(admitted, admitted=False)
    start = round(clock.now)
    still = composition(black("still", start=100, end=10_000), timebase=100 - start)
    frames_at(clock, renderer, surface, area, start, 3, shown=still)
    assert frames.tags == ["candidate-" + str(admitted.grant_id)] * 3, frames.tags
    assert renderer.GL.passes == 0 and area.renders == 3

    frames.grant_ = None                             # the grant withdrawn
    frames_at(clock, renderer, surface, area, start + 3, 2, shown=still)
    assert area.renders == 4 and len(frames.tags) == 3, "one clear, untagged, then nothing"

    successor = replace(admitted, grant_id=uuid.uuid4(), admitted=False)
    frames.grant_ = successor                        # a new candidate grant: drawn at once
    frames_at(clock, renderer, surface, area, start + 5, .1, shown=still)
    assert area.renders == 5 and frames.tags[-1] == "candidate-" + str(successor.grant_id)

    frames.grant_ = admitted                         # admitted, but for an older revision
    frames.grant = lambda output_id, shown=None: None if shown is not None else admitted
    frames_at(clock, renderer, surface, area, start + 6, 2, shown=still)
    assert area.renders == 5 and renderer.GL.passes == 0, "the buffer is kept"


def test_the_identify_banner_shown_or_hidden_over_a_still_is_committed_tagged(clock):
    """GTK paints the window again when the identify banner appears or goes, and commits it: the
    still under it is tagged again in that paint (no frame pass: the buffer holds it), so the
    Output's newest commit stays named, which the shell's ping needs to keep the lease. The
    planning tick sets the banner on every tick: only a change asks for a paint."""
    class Banner:
        def __init__(self):
            self.visible = False
        def show_all(self): self.visible = True
        def hide(self): self.visible = False
        def get_visible(self): return self.visible
        def get_child(self): return SimpleNamespace(set_text=lambda _text: None)

    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Compositor(clock)
    surface.identify_banner = Banner()
    start = round(clock.now)
    still = composition(black("still", start=100, end=10_000), timebase=100 - start)
    frames_at(clock, renderer, surface, area, start, .5, shown=still)
    assert (len(frames.tags), renderer.GL.passes) == (1, 1)
    for shown in ("hdmi1", "hdmi1", None, None):
        renderer.set_identify_output(shown)
        frames_at(clock, renderer, surface, area, clock.now + REFRESH, .3, shown=still)
    assert (len(frames.tags), renderer.GL.passes) == (3, 1), (frames.tags, renderer.GL.passes)
