"""The Player draws every time-varying picture property at the time of the frame it draws (its
display's frame clock), never at the planning tick's pace (owner, 2026-10-16: "The crossfade
between photos is jittery"; "Make it a general render loop solution").

The planning tick decides what an Output shows; player/look.py evaluates how it looks at a frame
time; a surface follows its frame clock only while that look changes. Fades and a playing video
are two cases of one rule: two renders at two frame times with no tick between them draw two
looks, and a still picture is not followed.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from test_executor import Rig, binding, layer

from contracts.models import Calibration
from player import native as native_module
from player.look import changed, look, until_change
from player.luts import PlayerLuts
from player.mainloop import FrameFollower, frame_time
from player.native import NativeOutput, NativeRenderer
from player.rendering import LocalLayer, OutputComposition, PrepareResult


def composition(*layers, timebase: float | None = 100.0) -> OutputComposition:
    """`layers` bottom to top on hdmi1; with timebase 100, monotonic 0 is plan time 100."""
    return OutputComposition(binding(), Calibration(), tuple(
        LocalLayer(item, None, item.position(100), item.alpha(100)) for item in layers),
        timebase=timebase)


def black(key: str, **fields):
    return layer(key, presentation="black", variant=None, **fields)


# -- The look at a frame time, and whether it still changes (pure) -------------------------------

def test_a_composition_looks_as_at_the_frame_time_it_is_drawn():
    """Alpha (a fade) and position (a video) are both read at the frame time; a still composition
    (a fallback) is drawn as its layers say."""
    fading = composition(black("fade", start=100, end=110, fade_in=2))
    assert look(fading, .5).layers[0].alpha == .25
    assert look(fading, 1.5).layers[0].alpha == .75
    video = composition(layer("video", video=True, start=100, end=200, media_origin=100))
    assert look(video, .5).layers[0].position == .5
    assert look(video, 1.5).layers[0].position == 1.5
    kept = composition(black("kept", start=100, end=110, fade_in=2), timebase=None)
    assert look(kept, 1.5) == kept


def test_the_look_changes_only_while_something_moves():
    """A fade or a seen video changes now (0); a still photo waits for its fade-out; nothing after
    it changes (None); a video under an opaque photo does not change what is seen."""
    photo = composition(black("photo", start=100, end=110, fade_in=2, fade_out=2))
    assert until_change(photo, 1) == 0                      # fading in
    assert until_change(photo, 5) == 3                      # still until its fade-out at 108
    assert until_change(photo, 9) == 0                      # fading out
    assert until_change(photo, 10) is None                  # over
    assert until_change(composition(black("still", start=100, end=110)), 1) is None
    video = layer("video", video=True, start=100, end=200, media_origin=100)
    assert until_change(composition(video), 1) == 0
    assert until_change(composition(video, black("cover", start=100, end=200, priority=1)), 1) is None
    assert until_change(composition(video, timebase=None), 1) is None
    # A draw mid-fade is followed by one more at the fade's end, then none.
    assert changed(photo, 1.5, 2.5) and not changed(photo, 2.5, 3)
    assert changed(photo, 5, 8.5)


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
    assert until_change(drawn, at + 1) == 0 and until_change(drawn, at + 5) is None


# -- The frame clock (player/mainloop.py) --------------------------------------------------------

class Area:
    """A GtkGLArea as _render and FrameFollower use it: its frame clock, its tick callbacks."""

    def __init__(self):
        self.renders = 0
        self.callbacks: dict[int, object] = {}
        self.frame_us = 0

    def queue_render(self):
        self.renders += 1

    def add_tick_callback(self, callback):
        key = len(self.callbacks) + 1
        self.callbacks[key] = callback
        return key

    def remove_tick_callback(self, key):
        del self.callbacks[key]

    def get_frame_clock(self):
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

    def frame(self, seconds: float) -> bool:
        """The frame clock reaches `seconds` (monotonic): every tick callback runs, as GDK's
        update phase does; one returning False is removed."""
        self.frame_us = round(seconds * 1e6)
        clock = self.get_frame_clock()
        kept = True
        for key, callback in list(self.callbacks.items()):
            if not callback(self, clock):
                del self.callbacks[key]
                kept = False
        return kept


class GLib:
    @staticmethod
    def get_monotonic_time():
        return round(time.monotonic() * 1e6)


def test_frame_time_maps_gdk_microseconds_onto_the_monotonic_clock():
    glib = SimpleNamespace(get_monotonic_time=lambda: 5_000_000)
    clock = SimpleNamespace(get_frame_time=lambda: 4_750_000)
    assert frame_time(glib, clock, lambda: 1000.0) == 999.75


def test_a_follower_runs_on_each_frame_until_its_frame_says_stop():
    area, seen = Area(), []
    follower = FrameFollower(GLib, area, lambda at: seen.append(round(at, 3)) or len(seen) < 2)
    follower.follow()
    follower.follow()                       # once only
    assert len(area.callbacks) == 1 and follower.following
    now = time.monotonic()
    assert area.frame(now) and not area.frame(now + 1 / 60)
    assert seen == [round(now, 3), round(now + 1 / 60, 3)]
    assert not area.callbacks and not follower.following


# -- The native renderer on its frame clock ------------------------------------------------------

class GL:
    """Enough OpenGL for _render: every call a no-op but the layer pass's `opacity` uniform,
    recorded per draw, and the queries _render reads."""
    GL_NO_ERROR = 0

    def __init__(self):
        self.opacities: list[float] = []

    def __getattr__(self, name):
        if name.startswith("GL_"):
            return 1
        return lambda *_args: None

    def glGetUniformLocation(self, _program, name):
        return name

    def glUniform1f(self, location, value):
        if location == "opacity":
            self.opacities.append(round(value, 3))

    def glGetIntegerv(self, _name):
        return 0

    def glGetError(self):
        return 0

    def glGetString(self, _name):
        return b"fake"


def frame_renderer(decoders=None):
    """A NativeRenderer with one realized surface, faked GL and GTK, and real present, _render,
    _frame and FrameFollower."""
    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._owner, renderer._closed, renderer._serial = threading.get_ident(), False, 0
    renderer._luts, renderer._frames, renderer.GL, renderer.GLib = PlayerLuts(), None, GL(), GLib
    renderer.gl_renderer, renderer.texture_budget = "fake", 1 << 30
    renderer._decoders = decoders or {}
    area = Area()
    surface = native_module._Surface(NativeOutput("hdmi1", "photo-wall-hdmi1"), None, area,
                                     programs=(1, 2), size=(64, 36), targets=[(1, 1), (2, 2)],
                                     vao=1)
    surface.follower = FrameFollower(GLib, area, lambda at: renderer._frame(surface, at))
    renderer._surfaces = {"hdmi1": surface}
    renderer.capacity = lambda compositions: SimpleNamespace(available=True)
    renderer.prepare = lambda local: PrepareResult("prepared")
    return renderer, surface, area


def render(renderer, surface, area, at: float) -> None:
    """GDK paints the frame at `at`: tick callbacks first, then the queued render."""
    area.frame(at)
    renderer._render(area, None, surface)


def test_a_fade_is_drawn_at_each_frames_time_with_no_tick_between():
    """One present() (one planning tick); then the frame clock alone draws the fade, a new
    strength each frame, the last at full strength, and stops following once nothing changes."""
    renderer, surface, area = frame_renderer()
    start = time.monotonic()
    fading = composition(black("fade", start=100, end=200, fade_in=1), timebase=100 - start)
    assert renderer.present(fading).status == "pending" and area.renders == 1
    render(renderer, surface, area, start + .25)
    assert renderer.GL.opacities == [.25]
    assert surface.follower.following, "a changing look follows the frame clock"
    for at in (start + .5, start + .75):
        area.frame(at)                  # the follower queues each frame's render
        renderer._render(area, None, surface)
    assert renderer.GL.opacities == [.25, .5, .75] and area.renders == 3
    area.frame(start + 1.25)            # the fade ended since the last draw: one more
    renderer._render(area, None, surface)
    assert renderer.GL.opacities == [.25, .5, .75, 1.0] and area.renders == 4
    assert not area.frame(start + 1.5), "a still picture is not followed"
    assert not surface.follower.following and area.renders == 4
    renderer._render(area, None, surface)           # GTK repaints: nothing new is drawn
    assert renderer.GL.opacities == [.25, .5, .75, 1.0]
    assert renderer.present(fading).status == "presented"


def test_a_still_photo_is_not_followed_until_its_fade_out_and_then_each_frame():
    renderer, surface, area = frame_renderer()
    start = time.monotonic()
    photo = composition(black("photo", start=100, end=110, fade_out=2), timebase=100 - start)
    renderer.present(photo)
    render(renderer, surface, area, start + 1)
    assert renderer.GL.opacities == [1.0] and surface.follower.following
    area.frame(start + 5)
    assert area.renders == 1, "a still look queues no render while its fade-out lies ahead"
    area.frame(start + 9)
    assert area.renders == 2
    renderer._render(area, None, surface)
    assert renderer.GL.opacities == [1.0, .5]


def test_a_playing_video_is_drawn_on_each_frame_and_a_still_photo_never():
    """A seen video's look changes on every frame: each frame is drawn, with no tick. A still
    photo with no fade draws once and installs no frame callback."""
    video = layer("video", video=True, start=100, end=200, media_origin=100)
    decoder = SimpleNamespace(incarnation=1, generation=1, sample_serial=1, playing=True,
                              prepared=True, failure=None, sample=None,
                              local=SimpleNamespace(layer=video))
    renderer, surface, area = frame_renderer({"video": decoder})
    renderer._upload = lambda _surface, _local: 7
    start = time.monotonic()
    renderer.present(composition(video, timebase=100 - start))
    render(renderer, surface, area, start + .1)
    for index in range(1, 4):
        area.frame(start + .1 + index / 60)
        renderer._render(area, None, surface)
    assert area.renders == 4 and len(renderer.GL.opacities) == 4
    assert abs(surface.acknowledged.composition.layers[0].position - (.1 + 3 / 60)) < 1e-3
    assert surface.follower.following

    still, still_surface, still_area = frame_renderer()
    still.present(composition(black("still", start=100, end=200), timebase=100 - start))
    render(still, still_surface, still_area, start + 1)
    assert still.GL.opacities == [1.0] and not still_surface.follower.following
    assert not still_area.callbacks
