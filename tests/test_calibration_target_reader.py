"""The calibration target reader on synthetic photos (decision 0027, slice R1; owner, 2026-10-14:
"Start working on a v2 that isn't so picky", "Some of my displays are smaller than a sheet of
paper"): it reads what the photo shows (any display, any piece of the paper at any scale, place and
turn, any framing, grey ink, uneven reflections, lens falloff), states what it dropped and where, and
refuses only for a real failure. The scenes are tests/calibration_target_scenes.py; a real ProRAW
photo is the bench's (NOT RUN here)."""
from __future__ import annotations

import functools
from dataclasses import replace

import numpy as np
import pytest
from calibration_target_scenes import (
    FIVE_INCH,
    PAPER_SQUARE_MM,
    PHOTO_SIZE,
    TV43,
    Scene,
    board_draw,
    camera,
    entry,
    focal_px,
    paper_middle,
    raw_photo,
    screen_to_wall,
    true_angle,
    truth,
)

from contracts.calibration_target import (
    PAPER_COLUMNS,
    PAPER_ROWS,
    SCREEN_ROWS,
    Square,
    Surface,
    square,
)
from contracts.tuning import TestPattern
from scripts.calibration_target.locate import Located, locate
from scripts.calibration_target.model import (
    DropReason,
    FocalSource,
    Photo,
    Reading,
    Refusal,
    Refused,
)
from scripts.calibration_target.read import read_photo


def _framed(scene: Scene, left_mm: float, right_mm: float, middle_y_mm: float) -> Scene:
    """`scene` photographed square-on so the photo's width spans the wall from left_mm to right_mm."""
    distance = scene.focal_widths * PHOTO_SIZE[0] * (right_mm - left_mm) / PHOTO_SIZE[0]
    return replace(scene, distance_mm=distance, aim_mm=((left_mm + right_mm) / 2, middle_y_mm))


def _closer(scene: Scene, square_px: float = 32.0) -> Scene:
    """`scene` with the camera aimed between the paper and the opening's middle, close enough for the
    paper's squares to span `square_px` (never farther than the default framing): any framing reads."""
    framed = scene.focal_widths * PHOTO_SIZE[0] * (scene.opening_mm[0] + 50) / (0.92 * PHOTO_SIZE[0])
    distance = min(framed, scene.focal_widths * PHOTO_SIZE[0] * PAPER_SQUARE_MM * scene.paper_scale / square_px)
    paper, opening = np.array(paper_middle(scene)), np.array(scene.opening_mm) / 2
    aim = tuple(float(v) for v in (paper + opening) / 2)
    return replace(scene, distance_mm=distance, aim_mm=aim)


def _inside(rng: np.random.Generator, opening: tuple[float, float], piece: tuple[int, int, int, int],
            scale: float) -> tuple[float, float]:
    """A middle for the piece whose circle (with the card) stays inside the opening, when it can."""
    radius = 0.5 * np.hypot(piece[2], piece[3]) * PAPER_SQUARE_MM * scale + 6
    width, height = opening
    return (float(rng.uniform(min(radius, width / 2), max(width - radius, width / 2))),
            float(rng.uniform(min(radius, height / 2), max(height - radius, height / 2))))


def _piece(rng: np.random.Generator) -> tuple[int, int, int, int]:
    if rng.random() < 0.3:
        return (0, 0, PAPER_ROWS, PAPER_COLUMNS)
    rows, columns = int(rng.integers(4, PAPER_ROWS)), int(rng.integers(4, PAPER_COLUMNS))
    return (int(rng.integers(0, PAPER_ROWS - rows + 1)), int(rng.integers(0, PAPER_COLUMNS - columns + 1)), rows, columns)


def anywhere(seed: int) -> Scene:
    """The paper anywhere on a 43-inch Frame: any piece, turn and place, 50-120 % scale, a mild
    reflection, any room light; the photo taken at a small angle, framed on the paper."""
    rng = np.random.default_rng(seed)
    piece = _piece(rng)
    scale = float(rng.uniform(0.5, 1.2))
    scene = Scene(piece=piece, paper_scale=scale, paper_mm=_inside(rng, TV43, piece, scale),
                  paper_turn_deg=float(rng.uniform(0, 360)), yaw_deg=float(rng.uniform(-10, 10)),
                  pitch_deg=float(rng.uniform(-8, 8)), paper_white=float(rng.uniform(0.3, 1.0)),
                  reflections=(("blob", float(rng.uniform(0, TV43[0])), float(rng.uniform(0, TV43[1])),
                                float(rng.uniform(0.1, 0.25)) * TV43[1], float(rng.uniform(0, 0.03))),),
                  seed=seed)
    return _closer(scene)


def heavy(seed: int, ink: float) -> Scene:
    """Uneven reflections on a 43-inch Frame's glass (the prototype's): 1-2 sharp windows (5-20 % of
    the screen's white), 1-3 blobs (2-15 %) and a gradient (up to 5 %)."""
    rng = np.random.default_rng(seed)
    width, height = TV43
    reflections = []
    for _ in range(int(rng.integers(1, 3))):
        w, h = float(rng.uniform(0.1, 0.4)) * width, float(rng.uniform(0.15, 0.6)) * height
        reflections.append(("window", float(rng.uniform(-0.1, 0.9)) * width, float(rng.uniform(-0.1, 0.8)) * height,
                            w, h, float(rng.uniform(0.05, 0.2))))
    for _ in range(int(rng.integers(1, 4))):
        reflections.append(("blob", float(rng.uniform(0, width)), float(rng.uniform(0, height)),
                            float(rng.uniform(0.05, 0.3)) * height, float(rng.uniform(0.02, 0.15))))
    reflections.append(("grad", float(rng.uniform(0, 0.05)), float(rng.uniform(0, 0.05))))
    return Scene(ink=ink, reflections=tuple(reflections), paper_turn_deg=float(rng.uniform(0, 360)),
                 paper_scale=0.9, paper_white=float(rng.uniform(0.4, 1.0)), seed=seed)


def tracer_scene() -> Scene:
    """A 5-inch 16:9 opening (110 x 62 mm, 17 columns) with a 5 x 6-square piece printed small
    (4 mm squares) in ink at 40 % of paper white, turned 30 degrees, photographed at a 2x lens so
    a third of the board's columns lie outside the photo."""
    draw = board_draw(Scene(opening_mm=FIVE_INCH))
    square_mm = FIVE_INCH[1] / SCREEN_ROWS
    board_left = (FIVE_INCH[0] - draw.columns * square_mm) / 2
    scene = Scene(opening_mm=FIVE_INCH, piece=(2, 4, 5, 6), paper_scale=4 / PAPER_SQUARE_MM, paper_mm=(60.0, 45.0),
                  paper_turn_deg=30.0, ink=0.4, focal_widths=1.5, yaw_deg=0.0, pitch_deg=0.0)
    return _framed(scene, board_left + draw.columns / 3 * square_mm, FIVE_INCH[0] + 15, FIVE_INCH[1] / 2)


@functools.cache
def located(scene: Scene, *, focal: bool = False) -> Located:
    photo = raw_photo(scene)
    if focal:
        photo = replace(photo, focal_px=focal_px(scene))
    out = locate(photo)
    assert isinstance(out, Located), out
    return out


def reading(scene: Scene, *, focal: bool = False) -> Reading:
    out = read_photo(located(scene, focal=focal), entry(scene))
    assert isinstance(out, Reading), out
    return out


def refused(scene: Scene, photo: Photo | None = None, test_pattern: TestPattern | None = None) -> Refused:
    found = locate(raw_photo(scene) if photo is None else photo)
    if isinstance(found, Refused):
        return found
    out = read_photo(found, entry(scene, test_pattern=test_pattern))
    assert isinstance(out, Refused), out
    return out


def error(scene: Scene, read: Reading) -> float:
    """The reading's white's largest relative error over the channels."""
    return float(np.max(np.abs(np.array(read.white.value) / truth(scene).white - 1)))


def assert_places(read: Reading | Refused, items: object = None) -> None:
    places = [d.where for d in read.dropped] if isinstance(read, Reading) else list(read.where)
    for where in places:
        assert 0 <= where.x <= 1 and 0 <= where.y <= 1, where
        assert where.words().startswith(f"the {where.surface.value}, the photo's ")


# -- RA0: the tracer ------------------------------------------------------------------------------------


def test_tracer_a_5_inch_frame_with_a_grey_ink_piece_turned_and_a_third_cut_off() -> None:
    scene = tracer_scene()
    found = located(scene)
    columns = board_draw(scene).columns
    seen = {square_column for square_column in range(columns)
            if any(abs(p[0] - square_column - 0.5) < 0.5 for p in found.screen.object_points)}
    assert len(seen) <= 2 * columns / 3 + 1             # a third of the board is out of the photo
    read = reading(scene)
    assert error(scene, read) <= 0.015, (read.white, truth(scene).white)
    assert read.ink == pytest.approx(0.4, abs=0.03)


# -- RA1: the paper anywhere -------------------------------------------------------------------------------


def test_the_uncut_sheet_centred_square_on() -> None:
    scene = Scene(yaw_deg=0.0, pitch_deg=0.0)
    assert error(scene, reading(scene)) <= 0.015


CODE_CAUSES = ("needs about", "clear of the paper")


@pytest.mark.parametrize("seed", range(24))
def test_the_paper_anywhere_reads_or_names_why_the_code_is_unread(seed: int) -> None:
    """Every seed of anywhere(), none left out: the photo reads within 1.5 %, or is refused only
    because no QR copy could be read, the detail naming which of the two causes it was (the squares
    too small for the QR, or no whole copy clear of the paper in the photo). 18 of these 24 read
    (decision 0027)."""
    scene = anywhere(seed)
    found = locate(raw_photo(scene))
    out = found if isinstance(found, Refused) else read_photo(found, entry(scene))
    if isinstance(out, Refused):
        assert out.why is Refusal.CODE_UNREADABLE, out
        assert any(cause in out.detail for cause in CODE_CAUSES), out.detail
        assert_places(out)
        return
    assert error(scene, out) <= 0.015, (out.white, truth(scene).white)


@pytest.mark.parametrize(("card_mm", "turn"), [(45.0, 0.0), (30.0, 20.0)])
def test_a_black_card_larger_than_the_paper_still_reads(card_mm: float, turn: float) -> None:
    """A dark card reaching over the screen squares round the paper: the screen whites it covers
    drop, the rest read."""
    scene = Scene(card_mm=card_mm, card=0.005, paper_turn_deg=turn)
    assert error(scene, reading(scene)) <= 0.015


def test_a_lamps_glint_on_the_screen_white_nearest_a_small_piece_is_outvoted() -> None:
    """A point lamp's glint lights one screen white beside the tracer's piece (its blacks untouched, so
    row 6 keeps it): paired with the median of its NEAREST screen whites, no paper white takes it as
    the screen's white."""
    scene = tracer_scene()
    wall = screen_to_wall(scene) @ np.array([7.5, 4.5, 1.0])             # the WHITE square at row 4, column 7
    assert square(Surface.SCREEN, 4, 7) is Square.WHITE
    glinted = replace(scene, reflections=(("blob", float(wall[0] / wall[2]), float(wall[1] / wall[2]), 1.0, 0.4),))
    assert error(glinted, reading(glinted)) <= 0.015


# -- RA2: a third of a 43-inch Frame cut off, 12 % lens falloff --------------------------------------------


def test_a_third_of_a_43_inch_frame_cut_off_with_12_percent_lens_falloff() -> None:
    draw = board_draw(Scene())
    square_mm = TV43[1] / SCREEN_ROWS
    board_left = (TV43[0] - draw.columns * square_mm) / 2
    scene = _framed(Scene(vignette=0.12, paper_scale=0.8, paper_mm=(560.0, 300.0), paper_turn_deg=-20.0,
                          yaw_deg=0.0, pitch_deg=0.0),
                    board_left + draw.columns / 3 * square_mm, TV43[0] + 40, TV43[1] / 2)
    read = reading(scene)
    assert error(scene, read) <= 0.015, (read.white, truth(scene).white)
    assert read.falloff == pytest.approx(0.12, abs=0.04)


# -- RA3: uneven reflections, black and grey ink -------------------------------------------------------


UNEVEN_TOLERANCE = 0.035


@pytest.mark.parametrize("ink", [0.04, 0.4])
@pytest.mark.parametrize("seed", range(16))
def test_uneven_reflections_read_within_tolerance_or_say_how_unsure(seed: int, ink: float) -> None:
    """Every seed of heavy(), none left out. A reflection over the paper alone looks like greyer ink
    (Detection row 7; .claude/errata.md, calibration targets v2): a reading off by more than 3.5 %
    must say so, its white's spread over 3.5 %, never a confident wrong white. Every drop has its
    place, a refusal is white-unread, and the ink lies in [0, 1).

    Mutation: the white's spread without the paper's unevenness -> seeds 3, 4, 7, 14 and 15 read
    5-35 % off with a spread of about 1 %."""
    scene = heavy(seed, ink)
    found = locate(raw_photo(scene))
    assert isinstance(found, Located), found
    out = read_photo(found, entry(scene))
    assert_places(out)
    if isinstance(out, Refused):
        assert out.why is Refusal.WHITE_UNREAD, out
        return
    assert 0 <= out.ink < 1, out.ink
    off = error(scene, out)
    if off > UNEVEN_TOLERANCE:
        assert out.white.spread > UNEVEN_TOLERANCE, (off, out.white)


def test_a_reflection_edge_across_a_white_drops_it_with_its_place() -> None:
    scene = heavy(20, 0.04)
    read = reading(scene)
    assert any(d.why is DropReason.REFLECTION_EDGE for d in read.dropped)
    assert_places(read)


# -- RA4: the viewing angle ---------------------------------------------------------------------------------


def _screen_whites_true_angle(scene: Scene) -> float:
    """The true angle at the screen's white squares in the photo, clear of the paper."""
    _, to_photo, _ = camera(scene)
    to_wall = screen_to_wall(scene)
    middle = np.array(paper_middle(scene))
    reach = 0.5 * np.hypot(PAPER_ROWS, PAPER_COLUMNS) * PAPER_SQUARE_MM * scene.paper_scale + 6
    points = []
    for row in range(SCREEN_ROWS):
        for column in range(board_draw(scene).columns):
            if square(Surface.SCREEN, row, column) is not Square.WHITE:
                continue
            wall = to_wall @ np.array([column + 0.5, row + 0.5, 1.0])
            if np.linalg.norm(wall[:2] / wall[2] - middle) < reach:
                continue
            p = to_photo @ wall
            x, y = p[0] / p[2], p[1] / p[2]
            if 0 <= x < PHOTO_SIZE[0] and 0 <= y < PHOTO_SIZE[1]:
                points.append((x, y))
    return true_angle(scene, np.array(points))


ANGLED = Scene(yaw_deg=35.0, pitch_deg=0.0, focal_widths=1.2, paper_scale=0.9, paper_mm=(380.0, 270.0))


def test_the_angle_with_the_exif_focal_length_is_within_a_degree() -> None:
    read = reading(ANGLED, focal=True)
    assert read.angle.focal is FocalSource.EXIF
    assert read.angle.degrees == pytest.approx(_screen_whites_true_angle(ANGLED), abs=1.0)


def test_the_angle_without_a_focal_length_is_estimated_within_5_degrees() -> None:
    read = reading(ANGLED)
    assert read.angle.focal is FocalSource.ESTIMATED
    assert read.angle.degrees == pytest.approx(_screen_whites_true_angle(ANGLED), abs=5.0)


# -- RA5: clipped whites ------------------------------------------------------------------------------------


def test_clipped_whites_drop_and_the_rest_read() -> None:
    """A bright window's reflection clips the paper whites under it; the rest read."""
    scene = Scene(paper_white=1.2, reflections=(("window", 340.0, 160.0, 130.0, 100.0, 0.8),), yaw_deg=0.0,
                  pitch_deg=0.0)
    read = reading(scene)
    clipped = [d for d in read.dropped if d.why is DropReason.CLIPPED]
    assert clipped and all(d.where.surface is Surface.PAPER for d in clipped)
    assert_places(read)
    assert error(scene, read) <= 0.015


def test_every_paper_white_clipped_is_white_unread_naming_the_clip() -> None:
    out = refused(Scene(paper_white=3.0))
    assert out.why is Refusal.WHITE_UNREAD and "clipped" in out.detail
    assert out.where and all(w.surface is Surface.PAPER for w in out.where)
    assert_places(out)


# -- RA6: each refusal for its real failure -----------------------------------------------------------------


def test_no_screen_target_is_target_not_found() -> None:
    out = refused(Scene(screen_shown=False))
    assert out.why is Refusal.TARGET_NOT_FOUND and "screen" in out.detail


def test_a_paper_too_small_in_the_photo_says_come_closer_or_print_larger() -> None:
    out = refused(Scene(paper_scale=0.15))
    assert out.why is Refusal.TARGET_NOT_FOUND and "come closer or print it larger" in out.detail


def test_a_screen_found_with_squares_too_small_gives_their_size() -> None:
    """Photographed from 3.6 m, the 43-inch Frame's squares span about 19 px: the screen board's one
    marker found is too few, and the refusal says how small its squares were and what is needed."""
    out = refused(Scene(distance_mm=3600.0, yaw_deg=0.0, pitch_deg=0.0))
    assert out.why is Refusal.TARGET_NOT_FOUND
    assert "the screen target's squares span" in out.detail and "about 20 are needed" in out.detail, out.detail


def test_the_paper_beside_the_frame_is_off_the_screen() -> None:
    seven = (155.0, 87.0)
    scene = Scene(opening_mm=seven, on_screen=False, paper_scale=0.5, paper_mm=(270.0, 50.0), yaw_deg=0.0,
                  pitch_deg=0.0)
    scene = _framed(scene, -30.0, 340.0, 60.0)
    out = refused(scene)
    assert out.why is Refusal.PAPER_OFF_SCREEN
    assert len(out.where) == 1 and out.where[0].surface is Surface.PAPER and out.where[0].x > 0.6
    assert_places(out)


def test_no_code_drawn_is_code_unreadable() -> None:
    out = refused(Scene(code_drawn=False))
    assert out.why is Refusal.CODE_UNREADABLE and out.where
    assert_places(out)


def test_an_entry_showing_no_target_is_not_a_target() -> None:
    out = refused(Scene(), test_pattern=TestPattern.WHITE)
    assert out.why is Refusal.NOT_A_TARGET


def test_the_paper_whites_under_a_black_card_are_white_unread() -> None:
    out = refused(Scene(paper_whites_covered=True))
    assert out.why is Refusal.WHITE_UNREAD and "covered" in out.detail
    assert out.where and all(w.surface is Surface.PAPER for w in out.where)
    assert_places(out)
