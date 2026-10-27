"""A photo larger than its Frame needs always fits, and its copy always holds the Frame (owner,
2026-10-26: "I'm ok with the too-small pictures, but the too-large picture being rejected is
ridiculous"). For any Frame output and any photo that fills it (rule 1 at the widest crop limit,
and at least the Frame output in its window), every larger photo of the same shape:
(a) passes the size part of the fit rule (central/wall_plan/fit.py `shape_fit`): no refusal for
    having more pixels than the Frame needs;
(b) is prepared (media/prepare.py `Preparer._size`, the copy's real size) at a size whose window
    still holds the Frame output, at any `scaled_copy_ratio`: never a pixel short.
Pure: the fit rule, the wall need and preparation's size, no database and no image."""
from __future__ import annotations

from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st
from support.frame_outputs import frame_output_of, target_of

from central.photo_library.rendition import FrameNeed, copy_edge, need_at, need_step, wall_need
from central.wall_plan.fit import band, crop_share, shape_fit
from central.wall_plan.fit_settings import FitSettings
from contracts.frame_fit import window_holds
from media.prepare import PreparationLimits, Preparer, StillRecipe

WIDEST = 0.6
SIDES = st.integers(min_value=64, max_value=3840)


@st.composite
def larger_photos(draw) -> tuple[tuple[int, int], tuple[int, int]]:
    """(the Frame output, a photo larger than it needs): a photo of a shape within the Frame's
    band at WIDEST, at its smallest size holding the Frame (`copy_edge`), scaled by 1 to 4."""
    frame = (draw(SIDES), draw(SIDES))
    aspect = frame[0] / frame[1]
    low, high = band(aspect, WIDEST)
    ratio = draw(st.floats(min_value=low, max_value=high))
    shape = (max(1, round(1000 * ratio)), 1000) if ratio >= 1 else (1000, max(1, round(1000 / ratio)))
    smallest = need_at(shape, copy_edge(shape, FrameNeed(aspect=aspect, width=frame[0], height=frame[1])))
    scale = draw(st.floats(min_value=1.0, max_value=4.0))
    photo = (round(smallest.width * scale), round(smallest.height * scale))
    return frame, photo


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@given(larger_photos(), st.sampled_from([1.0, 1.5, 4.0]))
# Copies at a need above the ladder whose binding side a copy rounded down would leave a pixel short.
@example(((3840, 2159), (12255, 8214)), 1.0)
@example(((1528, 3840), (4093, 7151)), 1.0)
def test_a_photo_larger_than_its_frame_needs_fits_and_its_copy_holds_the_frame(case, ratio):
    """Mutation probes AC1 (refuse a copy longer than 4,096: (a) fails), AC2a (the step above the
    ladder falls back to its largest: (b) fails) and AC2b (`_size` rounds down: (b) fails)."""
    frame, photo = case
    aspect = frame[0] / frame[1]
    assume(crop_share(*photo, aspect) <= WIDEST and window_holds(*photo, aspect, frame))   # it fills the Frame
    assume(max(photo) <= PreparationLimits().max_dimension)     # preparation's own ceiling
    assert shape_fit("image", *photo, target_of(*frame), WIDEST, FitSettings(), size_known=True) is None
    need = wall_need(photo, [frame_output_of("frame", frame)], FitSettings().copy_steps, ratio)
    copy = Preparer._size(*photo, StillRecipe(long_edge=need_step(need)))
    assert window_holds(*copy, aspect, frame), (frame, photo, need, copy)
