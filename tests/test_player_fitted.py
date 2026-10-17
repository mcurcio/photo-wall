"""Fitted pictures: each photo's look on its Frame is drawn once, before it is first shown, and each
shown frame is one pass that mixes them (owner, 2026-10-17: "Each photo's finished look ... is
drawn once when it first appears, not every frame. A fade frame then only blends two finished
pictures ... It also removes the once-a-second redraw"; and asked: "Is the new picture fully
prepared before the fade starts?").

The faked GL (test_player_frame_clock.GL) records each fit pass (`fits`: a photo's fitted picture
drawn through the fit, crop and transfer) and each frame pass (`passes`, and `opacities`: its
layers' strengths), so these tests count the GPU work a frame costs. The pixels the two passes
make are held to the reference by tests/native_renderer_pixel_harness.py (real GL, CI's
display-harness leg).
"""

from __future__ import annotations

import pytest
from test_executor import layer
from test_player_frame_clock import (  # noqa: F401
    Frames,
    black,
    clock,
    composition,
    decoder,
    frame_renderer,
    paint,
)

from contracts.models import Calibration, CoverFit, FrameProfile, PhotoCrop
from contracts.node_frame import PictureOperations
from player.fitted import FitKey, prepared, redrawn_by_frames, shown_at_once
from player.mainloop import DisplayEvents
from player.rendering import REDRAW_RENEWAL, LocalLayer, OutputComposition

TURNED = Calibration(fit=CoverFit(rotation=5))


def local(item, at: float = 100.0) -> LocalLayer:
    return LocalLayer(item, None, item.position(at), item.alpha(at))


def gpu(renderer) -> tuple[int, int]:
    """(fit passes, frame passes) drawn so far."""
    return len(renderer.GL.fits), renderer.GL.passes


class TrialFrames(Frames):
    """Display Host faked with Position's trial drawing `calibration` (sequence: its edits)."""

    def __init__(self, calibration: Calibration = Calibration()):
        super().__init__()
        self.calibration = calibration

    def trial(self, composition_, grant):
        trial = super().trial(composition_, grant)
        return None if trial is None else replace_calibration(trial, self.calibration)


def replace_calibration(trial, calibration):
    trial.calibration = calibration
    return trial


# -- The one rule (pure) --------------------------------------------------------------------------

def key(**fields) -> FitKey:
    return FitKey(**{"decode": (1, 1), "sample": 1, "fit": CoverFit(), "aspect": 16 / 9,
                     "crop": None, "size": (64, 36), **fields})


def test_prepared_needs_the_decodes_fitted_picture_finished_and_an_edit_never_revokes_it():
    """Prepared: a fitted picture of this decode, the GPU done with it. A later edit (another fit,
    size, crop) leaves it prepared (it is drawn again before it is shown); another decode does
    not. Shown at once (a layer's first frame) needs the picture itself, any sample of it. The
    frames redraw an edit of a layer on screen; another decode is the planning tick's."""
    drawn = key()
    assert prepared(drawn, key(), done=True)
    assert not prepared(drawn, key(), done=False) and not prepared(None, key(), done=True)
    assert prepared(drawn, key(fit=CoverFit(rotation=5), size=(80, 45)), done=True)
    assert not prepared(drawn, key(decode=(1, 2)), done=True)
    assert shown_at_once(drawn, key(sample=7), done=True)
    for stale in (key(fit=CoverFit(rotation=5)), key(size=(80, 45)), key(crop=PhotoCrop(offset=0)),
                  key(aspect=4 / 3), key(decode=(2, 1))):
        assert not shown_at_once(drawn, stale, done=True), stale
    assert not shown_at_once(drawn, key(), done=False)
    assert redrawn_by_frames(drawn, key(fit=CoverFit(rotation=5), sample=2), on_screen=True)
    assert not redrawn_by_frames(drawn, key(fit=CoverFit(rotation=5)), on_screen=False)
    assert not redrawn_by_frames(drawn, key(decode=(1, 2)), on_screen=True), "a seek is prepared"


# -- Drawn before its fade -----------------------------------------------------------------------

def test_an_incoming_photo_is_fitted_before_its_fade_and_no_fade_frame_fits(clock):  # noqa: F811
    """A photo on screen; the next one fades in over it at plan time 105. The planning tick's
    prepare() within the lead, while the screen is still, draws the incoming photo's fitted
    picture and reports it prepared. Every frame of the fade is then one frame pass: no frame
    draws a fitted picture, and each mixes the two at that frame's strength."""
    showing = layer("showing", start=100, end=106)
    incoming = layer("incoming", data=b"incoming", start=105, end=200, fade_in=1, priority=1)
    renderer, surface, area = frame_renderer({"showing": decoder(showing), "incoming": decoder(incoming)})
    start = clock.now
    shown = composition(showing, incoming, timebase=100 - start)
    renderer.present(shown)
    paint(renderer, surface, area, start + .1)
    assert renderer.GL.fits == [surface.fitted["showing"].framebuffer]
    clock.now = start + 1                           # the lead: 4 s before the fade, screen still
    assert renderer.prepare(local(incoming, 101), shown.binding).status == "prepared"
    assert renderer.GL.fits == [surface.fitted["showing"].framebuffer,
                                surface.fitted["incoming"].framebuffer], "drawn ahead of its fade"
    fits, passes = gpu(renderer)
    clock.now = start + 4.95
    renderer.present(shown)                         # the tick that finds the fade near
    frames = [start + 5 + index / 60 for index in range(1, 61)]
    for at in frames:
        paint(renderer, surface, area, at)
    assert len(renderer.GL.fits) == fits, "no fade frame draws a fitted picture"
    assert renderer.GL.passes == passes + len(frames), "one frame pass per fade frame"
    mixed = renderer.GL.opacities[-2 * len(frames):]
    assert mixed[1::2] == [round(index / 60, 3) for index in range(1, 61)]
    assert set(mixed[0::2]) == {1.0}


def test_prepare_draws_while_its_output_is_still_and_a_seen_layer_whatever_it_shows(clock):  # noqa: F811
    """While the Output follows its frame clock (a fade on screen) prepare() draws no fitted
    picture for a layer still to come: pending, nothing drawn. Once the screen is still it draws
    it. A layer seen now (present()'s, late) is drawn whatever the Output shows."""
    fading = black("fading", start=100, end=200, fade_in=2)
    later = layer("later", start=110, end=200, fade_in=1, priority=1)
    renderer, surface, area = frame_renderer({"later": decoder(later)})
    start = clock.now
    shown = composition(fading, later, timebase=100 - start)
    renderer.present(shown)
    paint(renderer, surface, area, start + .5)
    assert surface.follower.following
    assert renderer.prepare(local(later, 105), shown.binding).status == "pending"
    assert renderer.GL.fits == []
    for at in (start + 1, start + 1.5, start + 2.1, start + 2.2):
        paint(renderer, surface, area, at)
    assert not surface.follower.following
    assert renderer.prepare(local(later, 105), shown.binding).status == "prepared"
    assert len(renderer.GL.fits) == 1

    seen = layer("seen", data=b"seen", start=100, end=200, priority=2)
    late, late_surface, late_area = frame_renderer({"seen": decoder(seen)})
    late_surface.follower.follow()                  # as while a fade is drawn
    assert late.present(composition(fading, seen, timebase=100 - start)).status == "pending"
    assert len(late.GL.fits) == 1, "a layer seen now is drawn though the Output fades"


def test_a_photo_late_for_its_fade_holds_the_screen_then_joins_at_plan_time(clock):  # noqa: F811
    """No prepare() drew the incoming photo before its fade (a late plan): its first fade frame
    draws nothing new, never a fitted picture, and waits unfollowed; the next planning tick draws
    its fitted picture, and the frame after it shows the fade at that frame's strength."""
    showing = layer("showing", start=100, end=200)
    incoming = layer("incoming", data=b"incoming", start=101, end=200, fade_in=1, priority=1)
    renderer, surface, area = frame_renderer({"showing": decoder(showing), "incoming": decoder(incoming)})
    start = clock.now
    shown = composition(showing, incoming, timebase=100 - start)
    renderer.present(shown)
    paint(renderer, surface, area, start + .5)
    clock.now = start + .95
    renderer.present(shown)                         # wakes the clock; incoming not seen yet
    fits = len(renderer.GL.fits)
    paint(renderer, surface, area, start + 1.1)
    assert len(renderer.GL.fits) == fits and renderer.GL.opacities == [1.0] and surface.waiting
    paint(renderer, surface, area, start + 1.1 + 1 / 60)
    assert not surface.follower.following, "waits unfollowed: frames cannot draw it"
    clock.now = start + 1.12
    renderer.present(shown)                         # the tick: seen now, so drawn now
    assert len(renderer.GL.fits) == fits + 1
    paint(renderer, surface, area, start + 1.15)
    assert renderer.GL.opacities[-2:] == [1.0, .15]


# -- What a frame costs --------------------------------------------------------------------------

def test_a_fade_frame_is_one_pass_and_only_an_edit_draws_its_fitted_pictures_again(clock):  # noqa: F811
    """Two photos on screen mid-dissolve: each frame is one frame pass and draws no fitted picture.
    Position's trial edit arrives: the next frame draws both fitted pictures again (on screen, so
    the frame redraws them as it draws any edit), then frames are one pass each again."""
    under = layer("under", start=100, end=200)
    over = layer("over", data=b"over", start=100, end=200, fade_in=4, priority=1)
    renderer, surface, area = frame_renderer({"under": decoder(under), "over": decoder(over)})
    renderer._frames = frames = TrialFrames()
    renderer._display_events = DisplayEvents(renderer.GLib, 9, renderer._arrived)
    start = clock.now
    shown = composition(under, over, timebase=100 - start)
    assert renderer.prepare(local(over), shown.binding).status == "prepared"   # within the lead
    renderer.present(shown)
    paint(renderer, surface, area, start + 1)
    fits, passes = gpu(renderer)
    for index in range(1, 11):
        paint(renderer, surface, area, start + 1 + index / 60)
    assert gpu(renderer) == (fits, passes + 10)
    frames.calibration, frames.sequence = TURNED, 2  # the owner moves Position
    renderer.GLib.readable(9)
    paint(renderer, surface, area, start + 1 + 11 / 60)
    assert gpu(renderer) == (fits + 2, passes + 11)
    for index in range(12, 22):
        paint(renderer, surface, area, start + 1 + index / 60)
    assert gpu(renderer) == (fits + 2, passes + 21)


def test_a_still_picture_costs_no_gpu_work_per_second(clock):  # noqa: F811
    """A still photo is drawn once. Each second the planning tick renews its draw (Display Host's
    lease and the presented freshness need fresh tagged commits): the renewal draws nothing on
    the GPU, while its tag is committed again and the draw is acknowledged anew."""
    still = layer("still", start=100, end=200)
    renderer, surface, area = frame_renderer({"still": decoder(still)})
    renderer._frames = frames = TrialFrames()
    frames.sequence = None
    start = clock.now
    shown = composition(still, timebase=100 - start)
    renderer.present(shown)
    paint(renderer, surface, area, start + .1)
    drawn, tags = gpu(renderer), len(frames.tags)
    assert drawn == (1, 1)
    for second in range(1, 11):
        clock.now = start + .1 + second * REDRAW_RENEWAL
        renderer.present(shown)
        paint(renderer, surface, area, clock.now)
        assert surface.acknowledged.completed_at == clock.now
    assert gpu(renderer) == drawn, "ten renewals, no GPU work"
    assert len(frames.tags) == tags + 10, "each renewal commits a fresh tag"


def test_a_picture_settings_edit_is_one_frame_pass_and_draws_no_fitted_picture():
    """A Live slider edit (new picture operations) is drawn by the next frame: one frame pass,
    no fitted picture (the settings apply after the mix, every frame)."""
    still = layer("still", start=100, end=200)
    renderer, surface, area = frame_renderer({"still": decoder(still)})
    renderer._frames = frames = TrialFrames()
    frames.sequence = None
    renderer._display_events = DisplayEvents(renderer.GLib, 9, renderer._arrived)
    import time
    start = time.monotonic()
    renderer.present(composition(still, timebase=100 - start))
    paint(renderer, surface, area, start + .1)
    assert gpu(renderer) == (1, 1)
    frames.operations = PictureOperations("hdmi1", 2, (1.0, .9, .8), .5, 1.1, 1.0)
    renderer.GLib.readable(9)
    paint(renderer, surface, area, start + .1 + 1 / 60)
    assert gpu(renderer) == (1, 2)


# -- What invalidates a fitted picture -----------------------------------------------------------

def _on_screen(clock_):
    """A photo drawn and on screen, with Position's trial faked."""
    still = layer("still", start=100, end=200)
    held = decoder(still)
    renderer, surface, area = frame_renderer({"still": held})
    renderer._frames = frames = TrialFrames()
    renderer._display_events = DisplayEvents(renderer.GLib, 9, renderer._arrived)
    shown = composition(still, timebase=100 - clock_.now)
    renderer.present(shown)
    paint(renderer, surface, area, clock_.now + .1)
    assert gpu(renderer) == (1, 1)
    return renderer, surface, area, frames, held, shown, still


def _decode(renderer, surface, area, frames, held, shown, still, at):
    held.generation += 1                            # sought, or a new decoder
    renderer.present(shown)


def _sample(renderer, surface, area, frames, held, shown, still, at):
    held.sample_serial += 1                         # a playing video's next picture
    renderer._sample_arrived("still", held.incarnation)


def _fit(renderer, surface, area, frames, held, shown, still, at):
    frames.calibration, frames.sequence = TURNED, 2
    renderer.GLib.readable(9)


def _crop(renderer, surface, area, frames, held, shown, still, at):
    cropped = still.model_copy(update={"crop": PhotoCrop(offset=0)})
    renderer.present(OutputComposition(shown.binding, shown.calibration,
                                       (LocalLayer(cropped, None, 0.0, 1.0),), timebase=shown.timebase))


def _profile(renderer, surface, area, frames, held, shown, still, at):
    square = shown.binding.model_copy(update={
        "profile": FrameProfile(width_px=1000, height_px=1000, diagonal_inches=20)})
    renderer.present(OutputComposition(square, shown.calibration, shown.layers, timebase=shown.timebase))


def _size(renderer, surface, area, frames, held, shown, still, at):
    area.get_allocated_width = lambda: 80           # the Output's mode changed
    renderer.present(shown)
    area.renders += 1                               # GTK queues the resized area's render


@pytest.mark.parametrize("change", [_decode, _sample, _fit, _crop, _profile, _size],
                         ids=["decode", "video sample", "fit", "crop", "frame aspect", "output size"])
def test_every_change_to_a_photos_look_on_its_frame_draws_its_fitted_picture_again(clock, change):  # noqa: F811
    """Each FitKey field's change (another decode, a video's next sample, Position's fit, the
    photo's crop, the Frame profile's aspect, the Output's size) draws the fitted picture again
    before the frame that shows it; then frames draw it no more."""
    renderer, surface, area, frames, held, shown, still = _on_screen(clock)
    if change is _size:
        clock.now += .2
        change(renderer, surface, area, frames, held, shown, still, clock.now)
        paint(renderer, surface, area, clock.now)   # resized: nothing on screen to keep
        renderer.present(shown)                     # the next tick draws it at the new size
        clock.now += .05
    else:
        change(renderer, surface, area, frames, held, shown, still, clock.now)
    paint(renderer, surface, area, clock.now + .2)
    assert surface.fitted["still"].key is not None
    total = len(renderer.GL.fits)
    assert total == 2, ("drawn again once", renderer.GL.fits)
    for index in range(1, 4):
        paint(renderer, surface, area, clock.now + .2 + index / 60)
    assert len(renderer.GL.fits) == total
