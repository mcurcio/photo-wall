"""The calibration target reader on synthetic photos (decision 0026, slice R1): it finds both
targets, reads the QR and reads the white, the greys and the near-black steps through the review's
attacks, or refuses. The scenes are tests/calibration_target_scenes.py; a real ProRAW photo is the
bench step (NOT RUN here)."""
from __future__ import annotations

import functools
import random
import warnings
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from calibration_target_scenes import (
    CODE,
    Display,
    Scene,
    band,
    blob,
    codes,
    entry,
    group_patch,
    jpeg_photo,
    paired_groups,
    raw_photo,
    screen_canvas,
    summed,
    truth,
    uniform,
    wall_point,
)

from contracts.calibration_target import (
    PAPER_MARKER_IDS,
    SCREEN_MARKER_IDS,
    WHITE_COPIES,
    Role,
    Surface,
    TargetCode,
    calibration_target,
    screen_point,
)
from contracts.tuning import SIGNAL_LEVELS, TestPattern
from scripts.calibration_target.locate import Located, _markers_found, _qr_text, _view, locate
from scripts.calibration_target.model import (
    DropReason,
    PhotoKind,
    Place,
    Reading,
    Refusal,
    Refused,
    to_json,
)
from scripts.calibration_target.photos import load_photo
from scripts.calibration_target.read import read_photos

CLEAN = Scene()
SECOND = dict(yaw_deg=-12.0, pitch_deg=-3.0, seed=2)        # the second photo, from the other side


@functools.cache
def located(scene: Scene) -> Located:
    found = locate(raw_photo(scene))
    assert isinstance(found, Located), found
    return found


def read(*scenes: Scene) -> Reading | Refused:
    return read_photos(tuple(located(s) for s in scenes), entry(scenes[0]))  # type: ignore[arg-type]


def reading(*scenes: Scene) -> Reading:
    out = read(*scenes)
    assert isinstance(out, Reading), out
    return out


def white_error(found: Reading, scene: Scene) -> np.ndarray:
    return np.abs(np.array(found.white.value) / truth(scene).white - 1)


def grey_error(found: Reading, scene: Scene) -> float:
    true = truth(scene).tone
    assert {step.signal for step in found.tone} == set(true)
    return max(float(np.max(np.abs(codes(np.array(step.share.value)) - codes(true[step.signal]))))
               for step in found.tone)


def screen_place(index: int, role: Role) -> Place:
    return Place(Surface.SCREEN, *group_patch(index, Surface.SCREEN, role).rect.centre())


def patch_box(index: int, surface: Surface, role: Role, peak: float, scene: Scene = CLEAN):
    """A reflection exactly over one patch."""
    patch = group_patch(index, surface, role)
    x, y = wall_point(scene, patch)
    w, h = patch.rect.width_mm, patch.rect.height_mm
    return band(x - w / 2, x + w / 2, y - h / 2, y + h / 2, peak)


# -- The scene draws what the layout says ---------------------------------------------------------


def test_the_scenes_screen_is_screen_point() -> None:
    signal, raw = screen_canvas()
    rng = random.Random(5)
    from calibration_target_scenes import MARGIN_MM, PX_PER_MM
    for _ in range(400):
        row, column = rng.randrange(signal.shape[0]), rng.randrange(signal.shape[1])
        point = screen_point((column + 0.5) / PX_PER_MM - MARGIN_MM, (row + 0.5) / PX_PER_MM - MARGIN_MM)
        assert (signal[row, column], raw[row, column]) == (pytest.approx(point.signal), point.raw)


# -- 1. The tracer --------------------------------------------------------------------------------


def test_tracer_reads_the_pre_software_target_from_a_raw_photo() -> None:
    photo = raw_photo(CLEAN)
    assert set(_markers_found(_view(photo))) == set(SCREEN_MARKER_IDS) | set(PAPER_MARKER_IDS)
    found = locate(photo)
    assert isinstance(found, Located) and found.code == CODE
    out = read_photos((found,), entry(CLEAN))
    assert isinstance(out, Reading)
    assert (out.kind, out.stage) == (PhotoKind.RAW, TestPattern.TARGET_PRE_SOFTWARE)
    assert np.all(white_error(out, CLEAN) <= 0.005)
    assert out.white.spread <= 0.01 and out.white.copies >= WHITE_COPIES
    assert grey_error(out, CLEAN) <= 0.3
    assert out.lowest_distinct == truth(CLEAN).lowest_distinct == 1
    assert out.dropped == ()


def test_a_display_crushing_steps_1_to_3_reads_lowest_distinct_4() -> None:
    scene = Scene(display=Display(crush=3))
    assert reading(scene).lowest_distinct == 4


def test_the_post_software_target_is_read_through_the_chain() -> None:
    from contracts.node_picture import NEUTRAL_SOFTWARE
    scene = Scene(stage=TestPattern.TARGET_POST_SOFTWARE,
                  software=replace(NEUTRAL_SOFTWARE, brightness_percent=80, gamma_tenths=24))
    out = reading(scene)
    assert out.stage is TestPattern.TARGET_POST_SOFTWARE
    assert np.all(white_error(out, scene) <= 0.005)
    assert grey_error(out, scene) <= 0.3
    # The chain is in the light: the 0.5 grey reads darker than the display's own sRGB step.
    half = next(step for step in out.tone if step.signal == 128 / SIGNAL_LEVELS)
    assert half.share.value[1] < 0.95 * truth(CLEAN).tone[half.signal][1]


# -- 2. The TV surface's own glare (screen only) ---------------------------------------------------


def test_tv_surface_glare_is_read_away_against_the_screens_own_black() -> None:
    scene = Scene(tv_glare=uniform(0.03))
    out = reading(scene)
    assert np.all(white_error(out, scene) <= 0.005)
    assert grey_error(out, scene) <= 0.5


# -- 3. Spots ---------------------------------------------------------------------------------------


def test_a_spot_on_one_screen_grey_copy_drops_that_copy() -> None:
    index = paired_groups()[2]
    grey = group_patch(index, Surface.SCREEN, Role.GREY)
    scene = Scene(reflection=blob(*wall_point(CLEAN, grey), 6.0, 0.15))
    out = reading(scene)
    assert [(d.value, d.where, d.why) for d in out.dropped] == [
        (f"grey {round(grey.signal * SIGNAL_LEVELS)}", screen_place(index, Role.GREY), DropReason.COPY_UNLIKE_THE_REST)]
    assert grey_error(out, scene) <= 0.3


def test_a_spot_on_one_paper_white_drops_that_white_copy() -> None:
    index = paired_groups()[3]
    scene = Scene(reflection=blob(*wall_point(CLEAN, group_patch(index, Surface.PAPER, Role.WHITE)), 6.0, 0.15))
    out = reading(scene)
    assert [(d.value, d.why) for d in out.dropped] == [("white", DropReason.COPY_UNLIKE_THE_REST)]
    assert np.all(white_error(out, scene) <= 0.005)


# -- 4. Panel unevenness ----------------------------------------------------------------------------


@pytest.mark.parametrize("edge_dim", [0.05, 0.10, 0.20])
def test_a_panel_dimmer_at_its_edges_reads_each_grey_against_its_own_groups_white(edge_dim: float) -> None:
    scene = Scene(display=Display(edge_dim=edge_dim))
    out = reading(scene)
    assert np.all(white_error(out, scene) <= 0.01)
    assert grey_error(out, scene) <= 0.3


# -- 5. A reflection edge across a group, wherever it is --------------------------------------------


def _middle_edge() -> tuple[Scene, int]:
    """A reflection over the middle of the target whose top edge runs between the top-middle
    group's screen row and its paper row (owner: "what if the reflection is only in the middle?")."""
    index = paired_groups()[2]
    x, _ = wall_point(CLEAN, group_patch(index, Surface.SCREEN, Role.WHITE))
    box = calibration_target().dark_box
    return Scene(reflection=band(x - 25, x + 25, box.y_mm + 2, box.y_mm + box.height_mm / 2, 0.06)), index


def _side_edge() -> tuple[Scene, int]:
    """A reflection at the target's right edge, over one right group's screen patches and not its
    paper twins."""
    index = next(i for i in paired_groups() if group_patch(i, Surface.SCREEN, Role.WHITE).rect.x_mm
                 > calibration_target().dark_box.x_mm + calibration_target().dark_box.width_mm)
    index += 1
    _, y = wall_point(CLEAN, group_patch(index, Surface.SCREEN, Role.WHITE))
    box = calibration_target().dark_box
    return Scene(reflection=band(box.x_mm + box.width_mm - 0.5, 420, y - 15, y + 15, 0.06)), index


@pytest.mark.parametrize("edge", [_middle_edge, _side_edge], ids=["middle", "side"])
def test_a_reflection_edge_across_a_group_drops_the_group(edge) -> None:
    scene, index = edge()
    out = reading(scene)
    edges = [d for d in out.dropped if d.why is DropReason.REFLECTION_EDGE]
    assert [d.value for d in edges] == ["group"]
    white = group_patch(index, Surface.SCREEN, Role.WHITE).rect.centre()
    assert abs(edges[0].where.x_mm - white[0]) < 13 and abs(edges[0].where.y_mm - white[1]) < 13
    assert np.all(white_error(out, scene) <= 0.005)
    assert grey_error(out, scene) <= 0.3


# -- 6. Too many groups crossed ----------------------------------------------------------------------


def test_too_many_groups_crossed_is_refused_white_unread_naming_where() -> None:
    box = calibration_target().dark_box
    cx, cy = box.x_mm + box.width_mm / 2, box.y_mm + box.height_mm / 2
    inside = band(box.x_mm, box.x_mm + box.width_mm, box.y_mm, box.y_mm + box.height_mm, 1.0)

    def round_the_box(x, y):     # stronger the further round the box: every group's edge differs
        return 0.1 * (np.arctan2(y - cy, x - cx) + np.pi) / (2 * np.pi) * (1 - inside(x, y))

    out = read(Scene(reflection=round_the_box))
    assert isinstance(out, Refused) and out.why is Refusal.WHITE_UNREAD
    assert len(out.where) > len(paired_groups()) / 2
    assert all(place.surface is Surface.SCREEN for place in out.where)


# -- 7. Two photos -----------------------------------------------------------------------------------


def test_a_shadow_over_the_whole_paper_in_one_of_two_photos_is_refused() -> None:
    shadowed = Scene(room=uniform(0.85))
    alone = reading(shadowed)               # the stated cost: one photo alone reads it too bright
    assert np.all(np.abs(np.array(alone.white.value) / truth(shadowed).white - 1.176) <= 0.01)
    out = read(shadowed, Scene(**SECOND))
    assert isinstance(out, Refused) and out.why is Refusal.PHOTOS_DISAGREE


def test_a_reflection_moving_between_two_photos_takes_the_darker_reading() -> None:
    groups = paired_groups()
    first = Scene(reflection=summed(*(patch_box(g, Surface.PAPER, Role.WHITE, 0.008) for g in groups[:8])))
    second = Scene(reflection=summed(*(patch_box(g, Surface.PAPER, Role.WHITE, 0.008) for g in groups[8:])),
                   **SECOND)
    assert not np.all(white_error(reading(first), first) <= 0.005)     # either alone is off
    out = reading(first, second)
    assert np.all(white_error(out, first) <= 0.005)


# -- 8. A normal photo -------------------------------------------------------------------------------


def test_a_normal_photo_gives_the_white_only(tmp_path: Path) -> None:
    path = tmp_path / "IMG_0001.jpg"
    path.write_bytes(jpeg_photo(CLEAN))
    photo = load_photo(path)
    found = locate(photo)
    assert isinstance(found, Located)
    out = read_photos((found,), entry(CLEAN))
    assert isinstance(out, Reading)
    assert out.kind is PhotoKind.NORMAL and out.tone == () and out.lowest_distinct is None
    assert np.all(white_error(out, CLEAN) <= 0.05)


def test_a_raw_and_a_normal_photo_together_are_read_as_normal() -> None:
    """A normal photo's values carry the phone's tone curve: with one in the pair, no tone and no
    black are read, whichever photo it is."""
    raw = located(CLEAN)
    v = raw.photo.pixels
    encoded = np.where(v <= 0.0031308, 12.92 * v, 1.055 * np.maximum(v, 0) ** (1 / 2.4) - 0.055).astype(np.float32)
    normal = replace(raw, photo=replace(raw.photo, name="IMG_0003.jpg", kind=PhotoKind.NORMAL, pixels=encoded))
    for pair in ((raw, normal), (normal, raw)):
        out = read_photos(pair, entry(CLEAN))
        assert isinstance(out, Reading), out
        assert out.kind is PhotoKind.NORMAL and out.tone == () and out.lowest_distinct is None


@pytest.mark.parametrize("count", [0, 3])
def test_read_photos_takes_one_or_two(count: int) -> None:
    with pytest.raises(ValueError, match="located"):
        read_photos((located(CLEAN),) * count, entry(CLEAN))  # type: ignore[arg-type]


# -- 9. Refusals ---------------------------------------------------------------------------------


def test_two_paper_markers_hidden_is_target_not_found_naming_them() -> None:
    hidden = PAPER_MARKER_IDS[:2]
    out = locate(raw_photo(Scene(hidden_markers=frozenset(hidden))))
    assert isinstance(out, Refused) and out.why is Refusal.TARGET_NOT_FOUND
    markers = {m.marker_id: m for m in calibration_target().markers}
    assert out.where == tuple(Place(Surface.PAPER, *markers[i].rect.centre()) for i in hidden)


def test_one_marker_hidden_is_still_located() -> None:
    assert isinstance(locate(raw_photo(Scene(hidden_markers=frozenset({SCREEN_MARKER_IDS[1]})))), Located)


def test_a_covered_qr_is_code_unreadable() -> None:
    out = locate(raw_photo(Scene(qr_covered=True)))
    assert isinstance(out, Refused) and out.why is Refusal.CODE_UNREADABLE
    assert out.where == (Place(Surface.SCREEN, *calibration_target().qr.centre()),)


def test_paper_printed_at_97_percent_is_a_scale_mismatch() -> None:
    out = locate(raw_photo(Scene(paper_scale=0.97)))
    assert isinstance(out, Refused) and out.why is Refusal.SCALE_MISMATCH
    assert "97 %" in out.detail


def test_a_corner_cut_off_with_three_markers_found_is_outside_photo_naming_where() -> None:
    photo = raw_photo(CLEAN)
    cut = replace(photo, pixels=photo.pixels[175:], clipped=photo.clipped[175:])     # one top marker gone
    out = locate(cut)
    assert isinstance(out, Refused) and out.why is Refusal.OUTSIDE_PHOTO
    assert out.where and all(place.words().startswith("the screen target's top") for place in out.where)


@pytest.mark.parametrize(("offset", "words"), [((0.0, 8.0), "8 mm low"), ((-9.0, 0.0), "9 mm left")])
def test_a_sheet_laid_over_the_screen_patches_is_paper_misplaced(offset: tuple[float, float], words: str) -> None:
    out = locate(raw_photo(Scene(paper_offset_mm=offset)))
    assert isinstance(out, Refused) and out.why is Refusal.PAPER_MISPLACED, out
    assert words in out.detail
    assert out.where and all(place.surface is Surface.SCREEN for place in out.where)


def test_a_flat_qr_square_reads_no_text_without_dividing_by_zero() -> None:
    found = located(CLEAN)
    flat = np.full(_view(found.photo).shape, 128, np.uint8)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _qr_text(flat, found.screen, calibration_target().qr) is None


def test_a_clipped_white_is_refused() -> None:
    scene = Scene(exposure=1.1)
    out = read_photos((located(scene),), entry(scene))
    assert isinstance(out, Refused) and out.why is Refusal.CLIPPED and out.where


def test_an_entry_showing_no_target_is_not_a_target() -> None:
    out = read_photos((located(CLEAN),), entry(CLEAN, test_pattern=TestPattern.WHITE))
    assert isinstance(out, Refused) and out.why is Refusal.NOT_A_TARGET


def test_two_photos_naming_two_entries_are_refused() -> None:
    other = Scene(code=TargetCode(CODE.player_id, CODE.output_id, CODE.picture_change + 1), **SECOND)
    out = read(CLEAN, other)
    assert isinstance(out, Refused) and out.why is Refusal.DIFFERENT_ENTRIES


# -- The JSON and the words ---------------------------------------------------------------------


def test_places_in_plain_words() -> None:
    assert Place(Surface.PAPER, 10, 190).words() == "the paper's lower left"
    assert Place(Surface.SCREEN, 190, 5).words() == "the screen target's top middle"
    assert Place(Surface.SCREEN, 190, 170).words() == "the screen target's middle"


def test_a_reading_is_plain_json() -> None:
    import json
    out = to_json(reading(CLEAN))
    assert json.loads(json.dumps(out)) == out
    assert out["stage"] == "target-pre-software" and out["kind"] == "raw"
    assert out["entry"]["number"] == CODE.picture_change
    assert len(out["white"]["value"]) == 3
