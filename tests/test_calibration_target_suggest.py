"""What the calibration target reader suggests, by stage (decisions 0026, 0027; owner,
2026-10-13: "The best possible display calibration is one that doesnt require any software
adjustments."; the order is "the users choice -- not something that the software enforces").
Readings come from synthetic photos (tests/calibration_target_scenes.py); the displays are matched
to the paper white as the reader reads it (`paper_matched`), with no correction for the paper's
ink (owner, 2026-10-14: "Getting into the blackness of ink and the whiteness of paper is a level of
nuance that probably doesn’t matter in practice"). The photos are taken squarer than the scenes'
default (a 2x lens from farther, its focal length in EXIF), so the viewing angle's bound
(`angle_dimming`) stays a few percent; the cases at 30 and 35 degrees hold the bound itself."""
from __future__ import annotations

import functools
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from calibration_target_scenes import (
    CODE,
    Scene,
    entry,
    focal_px,
    jpeg_photo,
    paper_matched,
    raw_photo,
)

from contracts.calibration_target import TargetCode
from contracts.node_picture import NEUTRAL_SOFTWARE, SoftwarePicture
from contracts.tuning import TestPattern
from scripts.calibration_target.locate import Located, locate
from scripts.calibration_target.model import (
    Direction,
    PostSoftwareSuggestion,
    PreProperty,
    PreSoftwareStep,
    PreSoftwareSuggestion,
    Reading,
    SuggestionRefusal,
    SuggestionRefused,
    Unit,
)
from scripts.calibration_target.photos import load_photo
from scripts.calibration_target.read import read_photo
from scripts.calibration_target.suggest import suggest

POST = TestPattern.TARGET_POST_SOFTWARE


def shot(**fields: object) -> Scene:
    """A scene photographed nearly square-on through a 2x lens from farther (the same framing)."""
    return Scene(**{"focal_widths": 1.5, "yaw_deg": 3.0, "pitch_deg": 2.0, **fields})  # type: ignore[arg-type]


BASE = shot()
RIGHT = paper_matched(BASE)
THREE = ("contrast_percent", "colour_temperature_kelvin", "gamma_tenths")


@functools.cache
def reading(scene: Scene) -> Reading:
    found = locate(replace(raw_photo(scene), focal_px=focal_px(scene)))
    assert isinstance(found, Located), found
    out = read_photo(found, entry(scene))
    assert isinstance(out, Reading), out
    return out


def steps(scene: Scene) -> dict[PreProperty, PreSoftwareStep]:
    out = suggest(reading(scene))
    assert isinstance(out, PreSoftwareSuggestion)
    return {step.property: step for step in out.steps}


def post(scene: Scene) -> PostSoftwareSuggestion:
    out = suggest(reading(scene))
    assert isinstance(out, PostSoftwareSuggestion), out
    return out


def next_change(scene: Scene, software: SoftwarePicture) -> Scene:
    """The same wall after the operator tries `software` (the next picture change)."""
    code = TargetCode(CODE.player_id, CODE.output_id, scene.code.picture_change + 1)
    return replace(scene, software=software, code=code)


# -- Pre-software: the display's own controls --------------------------------------------------------


def test_pre_a_right_display_keeps_everything() -> None:
    found = steps(shot(display=RIGHT))
    assert list(found) == [PreProperty.WHITE_LEVEL, PreProperty.WHITE_COLOUR, PreProperty.TONE, PreProperty.BLACK]
    assert all(step.direction is Direction.KEEP for step in found.values()), found


@pytest.mark.parametrize(("level", "direction", "gap"),
                         [(1.06, Direction.LOWER, 6.0), (1.03, Direction.LOWER, 3.0), (0.90, Direction.RAISE, 10.0)])
def test_pre_white_level(level: float, direction: Direction, gap: float) -> None:
    step = steps(shot(display=paper_matched(BASE, level=level)))[PreProperty.WHITE_LEVEL]
    assert (step.direction, step.unit) == (direction, Unit.PERCENT)
    assert step.gap == pytest.approx(gap, abs=1.0)


def test_pre_a_screen_white_1000_k_warmer_than_the_paper_wants_cooler() -> None:
    daylight = shot(room_kelvin=6500)
    found = steps(replace(daylight, display=paper_matched(daylight, kelvin=5500)))
    step = found[PreProperty.WHITE_COLOUR]
    assert (step.direction, step.unit) == (Direction.COOLER, Unit.KELVIN)
    assert step.gap == pytest.approx(1000, abs=300)
    assert found[PreProperty.WHITE_LEVEL].direction is Direction.KEEP


def test_pre_a_display_at_gamma_2_5_wants_its_gamma_lowered() -> None:
    step = steps(shot(display=paper_matched(BASE, gamma=2.5)))[PreProperty.TONE]
    assert (step.direction, step.unit) == (Direction.LOWER, Unit.GAMMA)
    assert step.gap == pytest.approx(0.3, abs=0.1)


def test_pre_black_after_pluge() -> None:
    crushing = steps(shot(display=paper_matched(BASE, crush=3)))[PreProperty.BLACK]
    assert (crushing.direction, crushing.gap, crushing.unit) == (Direction.RAISE, 3.0, Unit.STEP)
    assert steps(shot(display=RIGHT))[PreProperty.BLACK].direction is Direction.KEEP


def test_pre_a_normal_photo_gives_the_white_steps_only(tmp_path: Path) -> None:
    scene = shot(display=paper_matched(BASE, level=1.06))
    path = tmp_path / "IMG_0002.jpg"
    path.write_bytes(jpeg_photo(scene))
    found = locate(load_photo(path))
    assert isinstance(found, Located)
    out = suggest(read_photo(found, entry(scene)))  # type: ignore[arg-type]
    assert isinstance(out, PreSoftwareSuggestion)
    assert [step.property for step in out.steps] == [PreProperty.WHITE_LEVEL, PreProperty.WHITE_COLOUR]


# -- The viewing angle's bound: a dimmer screen may be the panel's off-axis loss ------------------------


# Post-software, the white reads short through the software (contrast 90): no software step can
# raise the panel's own white past its peak, so the dim picture is the software's.
DIMMED = replace(NEUTRAL_SOFTWARE, contrast_percent=90)


def test_a_screen_10_percent_dim_at_30_degrees_is_unsure_before_and_after_software() -> None:
    angled = shot(display=paper_matched(BASE, level=0.9), yaw_deg=30.0, pitch_deg=0.0)
    assert reading(angled).angle.degrees == pytest.approx(30, abs=5)
    step = steps(angled)[PreProperty.WHITE_LEVEL]
    assert (step.direction, step.unit) == (Direction.UNSURE, Unit.PERCENT)
    assert step.gap == pytest.approx(10.0, abs=1.0)
    out = post(shot(display=RIGHT, stage=POST, software=DIMMED, yaw_deg=30.0, pitch_deg=0.0))
    assert out.unsure == (PreProperty.WHITE_LEVEL,)
    # Contrast held, and never "no change": the reading does not prove the white matched.
    assert out.to.contrast_percent == DIMMED.contrast_percent and not out.no_change


def test_the_same_square_on_is_raised() -> None:
    square_on = shot(display=paper_matched(BASE, level=0.9), yaw_deg=0.0, pitch_deg=0.0)
    step = steps(square_on)[PreProperty.WHITE_LEVEL]
    assert (step.direction, step.unit) == (Direction.RAISE, Unit.PERCENT)
    out = post(shot(display=RIGHT, stage=POST, software=DIMMED, yaw_deg=0.0, pitch_deg=0.0))
    assert out.unsure == () and out.to == NEUTRAL_SOFTWARE


def test_a_display_10_percent_bright_at_35_degrees_is_lowered() -> None:
    step = steps(shot(display=paper_matched(BASE, level=1.1), yaw_deg=35.0, pitch_deg=0.0))[PreProperty.WHITE_LEVEL]
    assert (step.direction, step.unit) == (Direction.LOWER, Unit.PERCENT)
    assert step.gap == pytest.approx(10.0, abs=1.0)


# -- Post-software: the residual, preferring neutral ------------------------------------------------


def test_post_a_right_display_with_neutral_software_is_no_change() -> None:
    out = post(shot(display=RIGHT, stage=POST))
    assert out.no_change and out.to == NEUTRAL_SOFTWARE


def test_post_a_display_right_within_its_spread_is_no_change() -> None:
    """0.75 % bright reads inside KEEP_SPREADS of its spread: one brightness step would fit the
    noise a little better, and neutral is preferred."""
    scene = shot(display=paper_matched(BASE, level=1.0075), stage=POST)
    gap = float(np.mean(reading(scene).white.value)) - 1
    assert 0.004 < gap <= 2 * reading(scene).white.spread
    out = post(scene)
    assert out.no_change and out.to == NEUTRAL_SOFTWARE


def test_post_refuses_without_the_greys(tmp_path: Path) -> None:
    """A normal photo gives the white only: the display's response is unread, so nothing is
    suggested (never a fit on an assumed display)."""
    scene = shot(display=paper_matched(BASE, level=1.1), stage=POST)
    path = tmp_path / "IMG_0004.jpg"
    path.write_bytes(jpeg_photo(scene))
    found = locate(load_photo(path))
    assert isinstance(found, Located)
    normal = read_photo(found, entry(scene))
    assert isinstance(normal, Reading) and normal.tone == ()
    out = suggest(normal)
    assert isinstance(out, SuggestionRefused) and out.why is SuggestionRefusal.TONE_UNREAD
    every_grey_dropped = replace(reading(scene), tone=())
    out = suggest(every_grey_dropped)
    assert isinstance(out, SuggestionRefused) and out.why is SuggestionRefusal.TONE_UNREAD


def test_post_a_right_display_goes_back_to_neutral() -> None:
    out = post(shot(display=RIGHT, stage=POST, software=DIMMED))
    assert not out.no_change and out.to == NEUTRAL_SOFTWARE


def test_post_a_display_10_percent_too_bright_is_never_no_change() -> None:
    """The white the Display sends is the paper match's (decision 0028: no brightness slider), so the
    three cannot close a white level gap: it is pre-software work, never "no change"."""
    out = post(shot(display=paper_matched(BASE, level=1.1), stage=POST))
    assert not out.no_change


LOOKS = {
    "gamma 2.5": shot(display=paper_matched(BASE, gamma=2.5), stage=POST),
    "warm": shot(display=paper_matched(BASE, kelvin=4500), stage=POST),
    "dim and cool": shot(display=paper_matched(BASE, level=0.9, kelvin=5600), stage=POST),
}


@pytest.mark.parametrize("name", list(LOOKS))
def test_post_moves_only_the_displays_three(name: str) -> None:
    out = post(LOOKS[name])
    moved = {knob for knob in ("contrast_percent", "colour_temperature_kelvin", "gamma_tenths")
             if getattr(out.to, knob) != getattr(NEUTRAL_SOFTWARE, knob)}
    assert moved and moved <= set(THREE)
    assert out.to == SoftwarePicture(contrast_percent=out.to.contrast_percent,
                                     colour_temperature_kelvin=out.to.colour_temperature_kelvin,
                                     gamma_tenths=out.to.gamma_tenths)
    assert not out.no_change


# -- Keep only what the reading proves (owner, 2026-10-14: "It shouldn’t say “change nothing” unless it
# can prove the system is perfectly calibrated. “Retake the photo” is closer to the truth") ---------------


def loose(scene: Scene, spread: float) -> Reading:
    """The scene's reading with its white's spread widened to `spread` (as an uneven reflection on
    the paper widens it, read.py row 7)."""
    found = reading(scene)
    return replace(found, white=replace(found.white, spread=spread))


def test_pre_a_right_display_read_too_loosely_says_retake_never_keep() -> None:
    out = suggest(loose(shot(display=RIGHT), 0.03))
    assert isinstance(out, PreSoftwareSuggestion)
    found = {step.property: step for step in out.steps}
    assert found[PreProperty.WHITE_LEVEL].direction is Direction.RETAKE
    assert found[PreProperty.WHITE_LEVEL].spread == pytest.approx(3.0)
    assert found[PreProperty.WHITE_COLOUR].direction is Direction.RETAKE
    assert found[PreProperty.TONE].direction is Direction.KEEP          # the greys were read tightly


def test_pre_a_gap_the_loose_reading_still_shows_is_named() -> None:
    step = {s.property: s for s in suggest(loose(shot(display=paper_matched(BASE, level=1.1)), 0.03)
                                            ).steps}[PreProperty.WHITE_LEVEL]  # type: ignore[union-attr]
    assert (step.direction, step.gap) == (Direction.LOWER, pytest.approx(10.0, abs=1.0))


def test_post_a_right_display_read_too_loosely_is_never_no_change() -> None:
    out = suggest(loose(shot(display=RIGHT, stage=POST), 0.03))
    assert isinstance(out, PostSoftwareSuggestion)
    assert out.to == NEUTRAL_SOFTWARE and not out.no_change
    assert PreProperty.WHITE_LEVEL in out.retake


def test_a_right_display_read_tightly_is_proven_matched() -> None:
    out = post(shot(display=RIGHT, stage=POST))
    assert out.no_change and out.retake == ()
    assert all(step.direction is Direction.KEEP for step in steps(shot(display=RIGHT)).values())


# -- No order (owner R8) ------------------------------------------------------------------------------


def test_suggest_remembers_nothing() -> None:
    first = reading(shot(display=paper_matched(BASE, level=1.06)))
    second = reading(shot(display=paper_matched(BASE, level=1.1), stage=POST))
    alone = (suggest(first), suggest(second))
    assert (suggest(second), suggest(first)) == alone[::-1]
    assert (suggest(first), suggest(second)) == alone
