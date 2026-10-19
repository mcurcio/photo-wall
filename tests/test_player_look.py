"""A composition's look over time (player/look.py), each change with one home: a change over time (a
fade, a calibration trial running out) comes from `next_change`, and `look` is the composition as
at the frame time it is committed, so a still picture costs nothing per frame (owner, 2026-10-16:
"Don't make a fix specific to fade. Make it a general render loop solution"). `moments` gives the
frame times at which a composition takes every set of seen layers it will take, so the controller
budget is judged at a fade's busiest instant even when the fade starts between two planning ticks
(render redesign S3, decision 3). The executor hands the renderer a timeline (its timebase), not
values baked at its tick.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from test_executor import Rig, binding, layer

from contracts.time import ManualClock, TimeMapping
from player.cache import Cache
from player.executor import TIMEBASE_TOLERANCE, Executor
from player.look import look, moments, next_change
from player.rendering import LocalLayer, OutputComposition, PresentationResult
from player.visibility import visible_now


def composition(*layers, timebase: float | None = 100.0) -> OutputComposition:
    """`layers` bottom to top on hdmi1; with timebase 100, monotonic 0 is plan time 100."""
    return OutputComposition(binding(), tuple(
        LocalLayer(item, None, item.position(100), item.alpha(100)) for item in layers), timebase=timebase)


def test_moments_are_now_and_the_middle_of_each_fade_ahead_in_time_order():
    held = layer("held", start=90, end=110, fade_out=4.0)          # fades out over plan 106..110
    incoming = layer("incoming", start=102, end=200, fade_in=2.0)  # fades in over plan 102..104
    assert moments(composition(held, incoming), 0.0) == (0.0, 3.0, 8.0)


def test_a_fade_under_way_is_covered_by_now_and_its_middle_once_it_is_ahead():
    under_way = layer("under-way", start=99, end=200, fade_in=4.0)     # plan 99..103, now 100
    assert moments(composition(under_way), 0.0) == (0.0, 1.0)
    past_middle = layer("past", start=96, end=200, fade_in=6.0)        # middle 99, before now
    assert moments(composition(past_middle), 0.0) == (0.0,)


def test_fades_over_and_empty_fades_add_no_moment_and_repeats_are_one():
    over = layer("over", start=90, end=200, fade_in=2.0)
    cut = layer("cut", start=105, end=200)                             # no fade: an empty span
    twins = (layer("a", start=102, end=200, fade_in=2.0), layer("b", start=102, end=200, fade_in=2.0))
    assert moments(composition(over, cut, *twins), 0.0) == (0.0, 3.0)


def test_a_still_composition_has_one_moment():
    incoming = layer("incoming", start=102, end=200, fade_in=2.0)
    assert moments(composition(incoming, timebase=None), 5.0) == (5.0,)


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
