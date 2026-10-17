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
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from test_executor import Rig, binding, layer

from contracts.models import Calibration
from contracts.node_frame import PictureOperations
from contracts.time import ManualClock, TimeMapping
from player import native as native_module
from player.cache import Cache
from player.executor import Executor
from player.look import TIMEBASE_TOLERANCE, changed, look, retimed, until_change
from player.luts import PlayerLuts
from player.mainloop import WAKE_LEAD, WAYLAND_EVENTS, DisplayEvents, FrameFollower, frame_time
from player.native import NativeOutput, NativeRenderer
from player.rendering import LocalLayer, OutputComposition, PrepareResult, PresentationResult
from player.visibility import visible_now
from player.wayland_frames import FrameGrant


def composition(*layers, timebase: float | None = 100.0) -> OutputComposition:
    """`layers` bottom to top on hdmi1; with timebase 100, monotonic 0 is plan time 100."""
    return OutputComposition(binding(), Calibration(), tuple(
        LocalLayer(item, None, item.position(100), item.alpha(100)) for item in layers),
        timebase=timebase)


def black(key: str, **fields):
    return layer(key, presentation="black", variant=None, **fields)


# -- The look at a frame time, and whether it still changes (pure) -------------------------------

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


def test_what_is_shown_changes_only_while_something_moves():
    """A fade or a seen video changes now (0); a still photo waits for its fade-out; nothing after
    it changes (None); a video under an opaque photo does not change what is seen. A video's new
    picture is no plan-time change of its look (`changed`): its decoder's samples pace it."""
    photo = composition(black("photo", start=100, end=110, fade_in=2, fade_out=2))
    assert until_change(photo, 1) == 0                      # fading in
    assert until_change(photo, 5) == 3                      # still until its fade-out at 108
    assert until_change(photo, 9) == 0                      # fading out
    assert until_change(photo, 10) is None                  # over
    assert until_change(composition(black("still", start=100, end=110)), 1) is None
    video = layer("video", video=True, start=100, end=200, media_origin=100)
    assert until_change(composition(video), 1) == 0
    assert until_change(composition(video), 1, pictures=False) is None
    assert not changed(composition(video), 1, 2)
    assert until_change(composition(video, black("cover", start=100, end=200, priority=1)), 1) is None
    assert until_change(composition(video, timebase=None), 1) is None
    # A draw mid-fade is followed by one more at the fade's end, then none.
    assert changed(photo, 1.5, 2.5) and not changed(photo, 2.5, 3)
    assert changed(photo, 5, 8.5)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 20), st.integers(1, 20), st.integers(0, 6),
                          st.integers(0, 6), st.sampled_from([1.0, .5])), min_size=1, max_size=4),
       st.floats(-5, 45), st.floats(0, 20))
def test_what_is_seen_is_constant_wherever_the_look_does_not_change(shapes, since, gap):
    """`look` and `changed` are one model: wherever `changed` says no, the layers seen and their
    alphas are the same at both frame times, whatever the layers' fades and opacities."""
    layers = []
    for index, (start, length, fade_in, fade_out, opacity) in enumerate(shapes):
        length = max(length, fade_in + fade_out)
        layers.append(black(f"l{index}", start=100 + start, end=100 + start + length,
                            fade_in=fade_in, fade_out=fade_out, opacity=opacity, priority=index))
    shown = composition(*layers)
    if not changed(shown, since, since + gap):
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


def test_a_timebase_moved_beyond_its_tolerance_is_another_timeline():
    drawn = composition(black("a", start=100, end=110), timebase=100.0)
    assert not retimed(drawn, replace(drawn, timebase=100 + TIMEBASE_TOLERANCE / 2))
    assert retimed(drawn, replace(drawn, timebase=100 + TIMEBASE_TOLERANCE * 2))
    assert retimed(drawn, replace(drawn, timebase=None))
    assert not retimed(replace(drawn, timebase=None), replace(drawn, timebase=None))


# -- The executor hands over timing, not values baked at its tick --------------------------------

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
    """GLib's monotonic time, one-shot timeouts and fd watches, recorded rather than run."""
    IOCondition = SimpleNamespace(IN=1)

    def __init__(self):
        self.timeouts: dict[int, tuple[int, object]] = {}
        self.watches: dict[int, tuple[int, int, object]] = {}
        self.added = 0

    def unix_fd_add_full(self, priority, fd, condition, callback):
        assert condition == self.IOCondition.IN
        self.added += 1
        self.watches[self.added] = (priority, fd, callback)
        return self.added

    def readable(self, fd) -> None:
        """`fd` has input: each watch on it runs, as the main loop's dispatch would."""
        for key, (_priority, watched, callback) in list(self.watches.items()):
            if watched == fd and not callback(fd, self.IOCondition.IN):
                del self.watches[key]

    @staticmethod
    def get_monotonic_time():
        return round(time.monotonic() * 1e6)

    def timeout_add(self, milliseconds, callback):
        self.added += 1
        self.timeouts[self.added] = (milliseconds, callback)
        return self.added

    def source_remove(self, key):
        self.timeouts.pop(key, None) or self.watches.pop(key)

    def fire(self) -> None:
        """Every armed timeout runs, as the main loop would once it is due."""
        for key, (_ms, callback) in list(self.timeouts.items()):
            del self.timeouts[key]
            callback()


def test_frame_time_maps_gdk_microseconds_onto_the_monotonic_clock():
    glib = SimpleNamespace(get_monotonic_time=lambda: 5_000_000)
    clock = SimpleNamespace(get_frame_time=lambda: 4_750_000)
    assert frame_time(glib, clock, lambda: 1000.0) == 999.75


def test_a_follower_runs_on_each_frame_until_its_frame_says_stop():
    area, seen = Area(), []
    follower = FrameFollower(GLib(), area,
                             lambda at: seen.append(round(at, 3)) or (0.0 if len(seen) < 2 else None))
    follower.follow()
    follower.follow()                       # once only
    assert len(area.callbacks) == 1 and follower.following
    now = time.monotonic()
    assert area.frame(now) and not area.frame(now + 1 / 60)
    assert seen == [round(now, 3), round(now + 1 / 60, 3)]
    assert not area.callbacks and not follower.following


def test_a_follower_whose_frame_raises_stops_following():
    area, glib = Area(), GLib()

    def frame(_at):
        raise RuntimeError("frame")

    follower = FrameFollower(glib, area, frame)
    follower.follow()
    with pytest.raises(RuntimeError):
        area.frame(time.monotonic())
    assert not follower.following and not follower.sleeping and not glib.timeouts
    follower.follow()
    assert follower.following, "a later follow() installs it again"


def test_a_follower_sleeps_through_a_long_wait_and_wakes_ahead_of_it():
    """A wait beyond WAKE_LEAD removes the frame callback: one main-loop timeout follows again
    WAKE_LEAD before the change; a follow() meanwhile cancels it; stop() removes either."""
    area, glib, waits = Area(), GLib(), [3.0, WAKE_LEAD, 0.0]
    follower = FrameFollower(glib, area, lambda _at: waits.pop(0) if waits else None)
    follower.follow()
    assert not area.frame(time.monotonic())
    assert not follower.following and follower.sleeping and not area.callbacks
    assert [ms for ms, _ in glib.timeouts.values()] == [round((3.0 - WAKE_LEAD) * 1000)]
    glib.fire()
    assert follower.following and not follower.sleeping
    assert area.frame(time.monotonic()) and area.frame(time.monotonic())   # within the lead
    assert not area.frame(time.monotonic()) and not follower.sleeping      # None: stopped
    waits[:] = [5.0]
    follower.follow()
    area.frame(time.monotonic())
    assert follower.sleeping
    follower.follow()                       # a new draw follows at once: the wake is cancelled
    assert follower.following and not glib.timeouts
    follower.stop()
    assert not follower.following and not area.callbacks


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


def decoder(item, *, prepared=True):
    """A resident decoder as present() and _render read it; `sample_serial` is its newest sample."""
    return SimpleNamespace(incarnation=1, generation=1, sample_serial=1,
                           playing=bool(item.variant.duration),
                           prepared=prepared, failure=None, sample=None,
                           local=SimpleNamespace(layer=item))


def frame_renderer(decoders=None, area=None):
    """A NativeRenderer with one realized surface, faked GL and GTK, and real present, _render,
    _frame and FrameFollower. Decoded pictures upload as texture 7 and read no new sample."""
    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._owner, renderer._closed, renderer._serial = threading.get_ident(), False, 0
    renderer._luts, renderer._frames, renderer.GL, renderer.GLib = PlayerLuts(), None, GL(), GLib()
    renderer.gl_renderer, renderer.texture_budget = "fake", 1 << 30
    renderer._decoders = decoders or {}
    area = area or Area()
    window = SimpleNamespace(destroyed=False, get_window=lambda: None)
    window.destroy = lambda: setattr(window, "destroyed", True)
    surface = native_module._Surface(NativeOutput("hdmi1", "photo-wall-hdmi1"), window, area,
                                     programs=(1, 2), size=(64, 36), targets=[(1, 1), (2, 2)],
                                     vao=1)
    surface.follower = FrameFollower(renderer.GLib, area, lambda at: renderer._frame(surface, at))
    renderer._surfaces = {"hdmi1": surface}
    renderer.capacity = lambda compositions: SimpleNamespace(available=True)
    renderer.prepare = lambda local: PrepareResult("prepared")
    renderer._upload = lambda _surface, _local: 7
    renderer._read_sample = lambda _decoder: None
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
    assert not surface.follower.following and not surface.follower.sleeping and area.renders == 4
    renderer._render(area, None, surface)           # GTK repaints: nothing new is drawn
    assert renderer.GL.opacities == [.25, .5, .75, 1.0]
    assert renderer.present(fading).status == "presented"


def test_a_still_photo_sleeps_until_its_fade_out_and_then_is_drawn_each_frame():
    """A still look with a fade-out ahead removes its frame callback (no call per frame while
    nothing changes) and wakes WAKE_LEAD before the fade-out, which the frame clock draws."""
    renderer, surface, area = frame_renderer()
    start = time.monotonic()
    photo = composition(black("photo", start=100, end=110, fade_out=2), timebase=100 - start)
    renderer.present(photo)
    render(renderer, surface, area, start + 1)
    assert renderer.GL.opacities == [1.0] and surface.follower.following
    assert not area.frame(start + 5)
    assert area.renders == 1 and not area.callbacks and surface.follower.sleeping
    (ms, _), = renderer.GLib.timeouts.values()
    assert abs(ms - (3 - WAKE_LEAD) * 1000) <= 1
    renderer.GLib.fire()                # the main loop wakes it ahead of the fade-out
    assert surface.follower.following
    area.frame(start + 9)
    assert area.renders == 2
    renderer._render(area, None, surface)
    assert renderer.GL.opacities == [1.0, .5]


def test_a_playing_video_is_drawn_on_the_frame_after_each_new_sample_only():
    """A seen video keeps its surface following; a frame with no new decoded sample draws nothing
    (its pixels would be the same); the frame after a new one draws it, with no tick. A still
    photo with no fade draws once and installs no frame callback."""
    video = layer("video", video=True, start=100, end=200, media_origin=100)
    playing = decoder(video)
    renderer, surface, area = frame_renderer({"video": playing})
    start = time.monotonic()
    renderer.present(composition(video, timebase=100 - start))
    render(renderer, surface, area, start + .1)
    assert area.renders == 1 and renderer.GL.opacities == [1.0]
    for index in range(1, 4):           # 60 Hz frames, no new sample
        render(renderer, surface, area, start + .1 + index / 60)
    assert area.renders == 1 and renderer.GL.opacities == [1.0]
    assert surface.follower.following, "a playing video is watched on every frame"
    playing.sample_serial += 1          # the decoder brings its next picture
    render(renderer, surface, area, start + .1 + 4 / 60)
    assert area.renders == 2 and renderer.GL.opacities == [1.0, 1.0]
    render(renderer, surface, area, start + .1 + 5 / 60)
    assert area.renders == 2, "drawn once per sample"

    still, still_surface, still_area = frame_renderer()
    still.present(composition(black("still", start=100, end=200), timebase=100 - start))
    render(still, still_surface, still_area, start + 1)
    assert still.GL.opacities == [1.0] and not still_surface.follower.following
    assert not still_area.callbacks


def test_a_layer_seen_only_at_a_later_frame_waits_for_its_decode_and_does_not_fail():
    """A photo fading in whose first draw had alpha 0 becomes visible at a later frame time
    before its decoder holds a picture: that frame draws nothing new and the decoder is not
    failed; it is drawn on a frame once decoded."""
    base = black("base", start=100, end=200)
    photo = layer("photo", start=101, end=200, fade_in=1, priority=1)
    decoding = decoder(photo, prepared=False)
    renderer, surface, area = frame_renderer({"photo": decoding})
    start = time.monotonic()
    renderer.present(composition(base, photo, timebase=100 - start))
    render(renderer, surface, area, start + .5)
    assert renderer.GL.opacities == [1.0]           # the base; the photo has alpha 0
    render(renderer, surface, area, start + 1.5)
    assert renderer.GL.opacities == [1.0] and decoding.failure is None
    decoding.prepared = True
    render(renderer, surface, area, start + 1.75)
    assert renderer.GL.opacities == [1.0, 1.0, .75]


def test_a_pending_draw_not_yet_decoded_at_the_frame_time_is_drawn_at_the_ticks_look():
    """present() prepared what the tick sees (the photo at alpha .5); at the frame time a layer
    is not decoded for the frame's look, so the pending draw is drawn as the tick saw it."""
    base = black("base", start=100, end=200)
    photo = layer("photo", start=99.5, end=200, fade_in=1, priority=1)
    renderer, surface, area = frame_renderer({"photo": decoder(photo, prepared=False)})
    start = time.monotonic()
    renderer.present(composition(base, photo, timebase=100 - start))
    render(renderer, surface, area, start + .25)
    assert renderer.GL.opacities == [1.0, .5]


def test_a_draw_whose_decoder_was_sought_is_not_redrawn_on_the_frame_clock():
    """A decoder sought or replaced since the draw (a new generation) holds another picture: the
    frame clock does not draw the old draw again; present() owes the next one."""
    photo = layer("photo", start=99.5, end=200, fade_in=1)
    held = decoder(photo)
    renderer, surface, area = frame_renderer({"photo": held})
    start = time.monotonic()
    renderer.present(composition(photo, timebase=100 - start))
    render(renderer, surface, area, start + .25)
    assert renderer.GL.opacities == [.75]
    render(renderer, surface, area, start + .3)
    assert renderer.GL.opacities == [.75, .8] and area.renders == 2
    held.generation += 1
    render(renderer, surface, area, start + .4)
    assert renderer.GL.opacities == [.75, .8] and area.renders == 2


def test_a_moved_timebase_reaches_the_drawn_surface_and_its_jitter_does_not():
    """A re-anchored clock mapping, same layers: present() makes a new draw on the new timeline,
    and the frame clock draws on from it. A timebase moved within the tolerance draws nothing."""
    renderer, surface, area = frame_renderer()
    start = time.monotonic()
    fading = composition(black("fade", start=100, end=200, fade_in=2), timebase=100 - start)
    renderer.present(fading)
    render(renderer, surface, area, start + .5)
    assert renderer.GL.opacities == [.25]
    renderer.present(replace(fading, timebase=fading.timebase + TIMEBASE_TOLERANCE / 2))
    assert area.renders == 1
    renderer.present(replace(fading, timebase=fading.timebase + .5))
    assert area.renders == 2
    render(renderer, surface, area, start + .5)
    assert renderer.GL.opacities == [.25, .5]
    render(renderer, surface, area, start + 1)
    assert renderer.GL.opacities == [.25, .5, .75]


class Frames:
    """Display Host faked: one admitted grant, a calibration trial at `sequence` (None: none)."""

    def __init__(self):
        self.grant_ = FrameGrant(grant_id=uuid.uuid4(), binding_generation=1, config_revision=1,
                                 frame_id="frame1", admitted=True)
        self.trial_id = uuid.uuid4()
        self.sequence: int | None = 1
        self.tags: list[str] = []
        self.operations: PictureOperations | None = None
        self.picture_changes: list[int | None] = []

    def grant(self, _output_id, _composition=None):
        return self.grant_

    def trial(self, _composition, _grant):
        if self.sequence is None:
            return None
        return SimpleNamespace(
            candidate=SimpleNamespace(trial_id=self.trial_id, generation=1, sequence=self.sequence,
                                      candidate_sha256="0" * 64),
            calibration=Calibration(), mat=None, primitives=None)

    def picture(self, _output_id):
        return self.operations

    def tag_rendered_buffer(self, _window, _grant, tag, _primitives=None, *, picture_change=None):
        self.tags.append(tag)
        self.picture_changes.append(picture_change)

    def close(self):
        pass


def test_a_trial_edit_is_drawn_on_the_frame_clock_and_its_end_stops_following():
    """While a calibration trial is drawn its surface follows the frame clock: an edit (a new
    sequence) is drawn on the next frame with no tick, and the trial's end is drawn too, after
    which the still picture is not followed."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Frames()
    start = time.monotonic()
    still = composition(black("still", start=100, end=200), timebase=100 - start)
    renderer.present(still)
    render(renderer, surface, area, start + .1)
    assert surface.acknowledged.applied_calibration.trial_sequence == 1
    assert surface.follower.following, "a drawn trial is watched on each frame"
    render(renderer, surface, area, start + .1 + 1 / 60)
    assert area.renders == 1, "no edit, no draw"
    frames.sequence = 2
    render(renderer, surface, area, start + .1 + 2 / 60)
    assert area.renders == 2 and surface.acknowledged.applied_calibration.trial_sequence == 2
    frames.sequence = None
    render(renderer, surface, area, start + .1 + 3 / 60)
    assert area.renders == 3 and surface.acknowledged.applied_calibration.trial_id is None
    assert not area.frame(start + .1 + 4 / 60) and not surface.follower.following


def test_close_removes_the_frame_callback():
    renderer, surface, area = frame_renderer()
    start = time.monotonic()
    renderer.present(composition(black("fade", start=100, end=200, fade_in=1),
                                 timebase=100 - start))
    render(renderer, surface, area, start + .25)
    assert surface.follower.following
    renderer.close()
    assert not area.callbacks and not surface.follower.following and surface.window.destroyed


def test_a_surface_with_no_frame_clock_draws_at_now():
    renderer, surface, area = frame_renderer(area=Area(frame_clock=False))
    before = time.monotonic()
    assert before <= renderer._frame_time(area) <= time.monotonic()


# -- Display Host's changes arrive as events, drawn on the next frame -----------------------------

def test_display_events_call_on_each_read_of_the_display_and_stop():
    """The watch sits on the display's socket at GDK's event priority (never above it: GDK's
    check reads the socket), calls `arrived` once per readable wake, survives a raising one, and
    stop() removes it."""
    glib, arrivals = GLib(), []

    def arrived():
        arrivals.append(1)
        if len(arrivals) == 2:
            raise RuntimeError("arrived")

    with pytest.raises(ValueError):
        DisplayEvents(glib, 9, arrived, priority=WAYLAND_EVENTS - 1)
    events = DisplayEvents(glib, 9, arrived)
    assert [(priority, fd) for priority, fd, _ in glib.watches.values()] == [(WAYLAND_EVENTS, 9)]
    glib.readable(9)
    glib.readable(9)
    glib.readable(9)
    assert len(arrivals) == 3 and glib.watches, "a raising arrival keeps the watch"
    events.stop()
    events.stop()
    assert not glib.watches


def operations(change: int, light: float = 1.0) -> PictureOperations:
    return PictureOperations("hdmi1", change, (1.0, 1.0, 1.0), light, 1.0, 1.0)


def test_picture_operations_arriving_between_ticks_are_drawn_on_the_next_frame():
    """A still photo is drawn once and not followed. A Live slider edit (new picture operations)
    arrives as Display Host's event: the surface follows its frame clock again and the next frame
    draws it, with no planning tick between; then it is not followed again. An event that changes
    nothing installs no frame callback, so nothing runs per frame while no edit arrives."""
    renderer, surface, area = frame_renderer()
    renderer._frames = frames = Frames()
    frames.sequence = None                          # no calibration trial drawn
    frames.operations = operations(1)
    renderer._display_events = DisplayEvents(renderer.GLib, 9, renderer._arrived)
    start = time.monotonic()
    renderer.present(composition(black("still", start=100, end=200), timebase=100 - start))
    render(renderer, surface, area, start + .1)
    assert renderer.GL.opacities == [1.0] and area.renders == 1
    assert frames.picture_changes[-1] == 1
    assert not area.callbacks, "a still picture is not followed"

    renderer.GLib.readable(9)                       # an event that changes nothing on screen
    assert not area.callbacks and not surface.follower.following and area.renders == 1

    frames.operations = operations(2, light=.5)     # the owner moves a slider
    renderer.GLib.readable(9)
    assert surface.follower.following, "the edit wakes the frame clock"
    render(renderer, surface, area, start + .1 + 2 / 60)
    assert area.renders == 2 and renderer.GL.opacities == [1.0, 1.0]
    assert frames.picture_changes[-1] == 2, "the tag names the edit the buffer shows"
    assert not area.frame(start + .1 + 3 / 60) and not area.callbacks
    renderer._render(area, None, surface)           # GTK repaints: nothing new is drawn
    assert renderer.GL.opacities == [1.0, 1.0]

    renderer.close()
    assert not renderer.GLib.watches
