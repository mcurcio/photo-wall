"""A composition's look over time (player/look.py): `moments`, the frame times at which a
composition takes every set of seen layers it will take, so the controller budget is judged at a
fade's busiest instant even when the fade starts between two planning ticks (render redesign S3,
decision 3)."""
from __future__ import annotations

from test_executor import binding, layer

from player.look import moments
from player.rendering import LocalLayer, OutputComposition


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
