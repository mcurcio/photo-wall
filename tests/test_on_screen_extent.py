"""When a planned photo may be on screen (roadmap 5a, run ledger .claude/runs/photo-5a.md,
slice R1, cases 1-5), through real `contracts.models.Layer` values."""
from __future__ import annotations

import math

import pytest

from contracts.models import AfterEnd, Layer, Variant
from contracts.on_screen import Extent, kept_from, on_screen_extent
from contracts.photo_crop import CENTRED

STILL = Variant(asset_id="asset-a", recipe_id="e" * 64, sha256="a" * 64, size=1, media_type="image/jpeg", width=4000, height=3000)


def _layer(start: float, end: float, after_end: AfterEnd = "leave_as_is", *,
           fade_in: float = 0) -> Layer:
    return Layer(assignment_id="a", run_id="r", output_id="o", frame_id="f",
                 binding_generation=1, start=start, end=end, media_origin=start, variant=STILL,
                 crop=CENTRED, fade_in=fade_in, after_end=after_end)


def test_a_layer_left_as_is_spans_its_interval_widened_by_twice_the_uncertainty() -> None:
    assert on_screen_extent(_layer(10, 40), (), .1) == Extent(start=9.8, end=40.2)


def test_a_kept_still_holds_until_a_later_layer_replaces_what_the_frame_keeps() -> None:
    kept = _layer(10, 40, "keep_this_photo")
    assert on_screen_extent(kept, (), .1).end is None
    extent = on_screen_extent(kept, (_layer(40, 70, "keep_this_photo", fade_in=2),), .1)
    assert extent.end == pytest.approx(42.2)
    assert on_screen_extent(kept, (_layer(40, 70, "keep_nothing"),), .1).end == pytest.approx(40.2)
    assert on_screen_extent(kept, (_layer(40, 70),), .1).end is None


def test_a_replacement_that_starts_early_still_lets_the_kept_layer_draw_to_its_end() -> None:
    kept = _layer(10, 40, "keep_this_photo")
    on_top = _layer(20, 30, "keep_nothing")
    assert on_screen_extent(kept, (on_top,), .1).end == pytest.approx(40.2)


def test_the_earliest_replacement_wins_and_earlier_layers_do_not_count() -> None:
    kept = _layer(10, 40, "keep_this_photo")
    before = _layer(0, 5, "keep_nothing")
    later = (_layer(80, 90, "keep_nothing"), _layer(60, 70, "keep_nothing"), before)
    assert on_screen_extent(kept, later, .1).end == pytest.approx(60.2)
    assert on_screen_extent(kept, (before,), .1).end is None


def test_touching_extents_do_not_overlap_and_an_open_one_overlaps_every_later_one() -> None:
    first, second = Extent(start=0, end=10), Extent(start=10, end=20)
    assert not first.overlaps(second) and not second.overlaps(first)
    assert Extent(start=0, end=10.5).overlaps(second)
    open_ended = Extent(start=5)
    assert open_ended.overlaps(Extent(start=1000, end=1001))
    assert Extent(start=1000).overlaps(open_ended)
    assert not open_ended.overlaps(Extent(start=0, end=5))


@pytest.mark.parametrize("uncertainty", [-.1, math.nan, math.inf])
def test_a_bad_uncertainty_is_refused(uncertainty) -> None:
    with pytest.raises(ValueError):
        on_screen_extent(_layer(10, 40), (), uncertainty)


def test_a_kept_still_takes_effect_once_its_fade_in_is_over() -> None:
    assert kept_from(_layer(10, 40, "keep_this_photo", fade_in=3)) == 13


def test_an_extent_must_run_forward() -> None:
    with pytest.raises(ValueError):
        Extent(start=10, end=10)
