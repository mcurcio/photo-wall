"""The calibration targets' layout and code (contracts/calibration_target.py, decision 0026): the
invariants its `calibration_target()` docstring names, the owner's reflection plan (2026-10-13:
tight screen/paper pairs, a screen black and a paper black in every local group, repeated groups
round the whole box), the markers' bits against OpenCV's own dictionary, the point lookups the
Player and the PDF draw from, the print fit on Letter and A4, and the QR's text."""
from __future__ import annotations

import itertools
import math

import cv2
import pytest

from contracts.calibration_target import (
    A4_MM,
    BOX_SLACK_MM,
    DARK_BOX_MM,
    GREY_COPIES,
    GREY_SIGNALS,
    LETTER_MM,
    MARKER_BITS,
    MARKER_MODULES,
    NEAR_BLACK_COPIES,
    NEAR_BLACK_STEPS,
    PAIR_MM,
    PAPER_MARKER_IDS,
    PAPER_MARKER_MM,
    PAPER_TARGET_MM,
    PRINT_MARGIN_MM,
    SCREEN_MARKER_IDS,
    SCREEN_MARKER_MM,
    SCREEN_TARGET_MM,
    WHITE_COPIES,
    GroupKind,
    Marker,
    Patch,
    Rect,
    Role,
    Surface,
    TargetCode,
    calibration_target,
    code_text,
    paper_ink,
    parsed_code,
    screen_point,
)
from contracts.node_output import OUTPUT_IDS
from contracts.tuning import SIGNAL_LEVELS

TARGET = calibration_target()
SCREEN_PATCHES = [patch for group in TARGET.groups for patch in group.patches if patch.surface is Surface.SCREEN]
PAPER_PATCHES = [patch for group in TARGET.groups for patch in group.patches if patch.surface is Surface.PAPER]
SCREEN_MARKERS = [marker for marker in TARGET.markers if marker.surface is Surface.SCREEN]
PAPER_MARKERS = [marker for marker in TARGET.markers if marker.surface is Surface.PAPER]
PAIRED = [group for group in TARGET.groups if group.kind is GroupKind.PAIRED]
RULERS = [group for group in TARGET.groups if group.kind is GroupKind.RULER]
PLAYER_ID = "p-" + "0123456789abcdef" * 2


def module_mm(marker: Marker) -> float:
    return marker.rect.width_mm / MARKER_MODULES


def with_quiet_zone(marker: Marker) -> Rect:
    module = module_mm(marker)
    rect = marker.rect
    return Rect(rect.x_mm - module, rect.y_mm - module, rect.width_mm + 2*module, rect.height_mm + 2*module)


def inside(inner: Rect, outer: Rect) -> bool:
    return (outer.x_mm <= inner.x_mm and inner.x_mm + inner.width_mm <= outer.x_mm + outer.width_mm
            and outer.y_mm <= inner.y_mm and inner.y_mm + inner.height_mm <= outer.y_mm + outer.height_mm)


def overlap(a: Rect, b: Rect) -> bool:
    return (a.x_mm < b.x_mm + b.width_mm and b.x_mm < a.x_mm + a.width_mm
            and a.y_mm < b.y_mm + b.height_mm and b.y_mm < a.y_mm + a.height_mm)


def in_screen_plane(rect: Rect) -> Rect:
    """A paper rect where it lies on the screen when the sheet is centred in the dark box."""
    x, y = TARGET.paper_in_box
    return Rect(rect.x_mm + x, rect.y_mm + y, rect.width_mm, rect.height_mm)


def side(patch: Patch) -> str:
    """Which side of the dark box a screen patch is on."""
    box, (x, y) = TARGET.dark_box, patch.rect.centre()
    if y < box.y_mm:
        return "top"
    if y >= box.y_mm + box.height_mm:
        return "bottom"
    return "left" if x < box.x_mm else "right"


def one(group, surface: Surface, role: Role) -> Patch:
    (patch,) = [p for p in group.patches if p.surface is surface and p.role is role]
    return patch


# -- 1. The markers are OpenCV's ---------------------------------------------------------------

def test_marker_bits_are_opencvs_dict_4x4_50():
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for marker_id in (*SCREEN_MARKER_IDS, *PAPER_MARKER_IDS):
        image = cv2.aruco.generateImageMarker(dictionary, marker_id, MARKER_MODULES)
        rows = tuple("".join("1" if image[r, c] > 127 else "0" for c in range(1, 5)) for r in range(1, 5))
        assert MARKER_BITS[marker_id] == rows, marker_id
    assert set(MARKER_BITS) == {*SCREEN_MARKER_IDS, *PAPER_MARKER_IDS}


# -- 2. Layout invariants ----------------------------------------------------------------------

def test_screen_rects_lie_in_their_plane_outside_the_box_and_qr():
    plane = Rect(0.0, 0.0, *SCREEN_TARGET_MM)
    for rect in [p.rect for p in SCREEN_PATCHES] + [with_quiet_zone(m) for m in SCREEN_MARKERS]:
        assert inside(rect, plane), rect
        assert not overlap(rect, TARGET.dark_box), rect
        assert not overlap(rect, TARGET.qr), rect
    assert inside(TARGET.dark_box, plane) and inside(TARGET.qr, plane)
    assert not overlap(TARGET.qr, TARGET.dark_box)


def test_no_two_screen_rects_overlap():
    rects = [p.rect for p in SCREEN_PATCHES] + [with_quiet_zone(m) for m in SCREEN_MARKERS]
    for a, b in itertools.combinations(rects, 2):
        assert not overlap(a, b), (a, b)


def test_paper_rects_lie_in_the_content_and_never_overlap():
    plane = Rect(0.0, 0.0, *PAPER_TARGET_MM)
    rects = [p.rect for p in PAPER_PATCHES] + [m.rect for m in PAPER_MARKERS]
    for rect in rects:
        assert inside(rect, plane), rect
    for a, b in itertools.combinations(rects, 2):
        assert not overlap(a, b), (a, b)


def test_the_paper_content_lies_inside_the_dark_box():
    assert inside(in_screen_plane(Rect(0.0, 0.0, *PAPER_TARGET_MM)), TARGET.dark_box)


def test_at_least_white_copies_paired_groups_each_one_pair():
    assert len(PAIRED) >= WHITE_COPIES
    for group in PAIRED:
        assert sorted((p.surface.value, p.role.value) for p in group.patches) == sorted([
            ("screen", "black"), ("screen", "white"), ("screen", "grey"), ("paper", "black"), ("paper", "white")])
        assert len({side(p) for p in group.patches if p.surface is Surface.SCREEN}) == 1


def test_paired_groups_lie_on_all_four_sides():
    assert {side(one(g, Surface.SCREEN, Role.WHITE)) for g in PAIRED} == {"top", "right", "bottom", "left"}


def test_groups_are_numbered_in_order():
    assert [group.index for group in TARGET.groups] == list(range(len(TARGET.groups)))


def test_each_grey_has_its_copies_on_two_sides():
    for grey in GREY_SIGNALS:
        holders = [g for g in PAIRED if one(g, Surface.SCREEN, Role.GREY).signal == grey]
        assert len(holders) >= GREY_COPIES, grey
        assert len({side(one(g, Surface.SCREEN, Role.GREY)) for g in holders}) >= 2, grey
    assert {one(g, Surface.SCREEN, Role.GREY).signal for g in PAIRED} == set(GREY_SIGNALS)


def test_each_near_black_step_has_its_copies_each_touching_a_black():
    steps = [(group, p) for group in RULERS for p in group.patches if p.role is Role.NEAR_BLACK]
    for k in NEAR_BLACK_STEPS:
        assert sum(1 for _, p in steps if p.signal == k / SIGNAL_LEVELS) >= NEAR_BLACK_COPIES, k
    for group, step in steps:
        assert step.rect.width_mm >= 8 and step.rect.height_mm >= 8
        blacks = [p.rect for p in group.patches if p.role is Role.BLACK]
        grown = Rect(step.rect.x_mm - 1e-6, step.rect.y_mm - 1e-6, step.rect.width_mm + 2e-6, step.rect.height_mm + 2e-6)
        assert any(overlap(grown, black) for black in blacks), step
    assert {p.surface for g in RULERS for p in g.patches} == {Surface.SCREEN}


def test_paired_patches_are_at_least_ten_millimetres():
    for group in PAIRED:
        for patch in group.patches:
            assert patch.rect.width_mm >= 10 and patch.rect.height_mm >= 10, patch


def test_markers_sit_at_the_corners_in_order():
    def corners(width: float, height: float, inset: float, side_mm: float) -> list[tuple[float, float]]:
        far_x, far_y = width - inset - side_mm, height - inset - side_mm
        return [(inset, inset), (far_x, inset), (far_x, far_y), (inset, far_y)]

    assert [m.marker_id for m in SCREEN_MARKERS] == list(SCREEN_MARKER_IDS)
    assert [m.marker_id for m in PAPER_MARKERS] == list(PAPER_MARKER_IDS)
    screen_module = SCREEN_MARKER_MM / MARKER_MODULES
    assert [(m.rect.x_mm, m.rect.y_mm) for m in SCREEN_MARKERS] == corners(*SCREEN_TARGET_MM, screen_module,
                                                                        SCREEN_MARKER_MM)
    assert [(m.rect.x_mm, m.rect.y_mm) for m in PAPER_MARKERS] == corners(*PAPER_TARGET_MM, 0.0, PAPER_MARKER_MM)
    assert {(m.rect.width_mm, m.rect.height_mm) for m in SCREEN_MARKERS} == {(SCREEN_MARKER_MM,) * 2}
    assert {(m.rect.width_mm, m.rect.height_mm) for m in PAPER_MARKERS} == {(PAPER_MARKER_MM,) * 2}


def test_each_screen_patch_is_within_pair_mm_of_its_paper_twin():
    """A screen BLACK's twin is its group's paper BLACK; a screen WHITE's and GREY's the paper WHITE."""
    for group in PAIRED:
        for screen_role, paper_role in ((Role.BLACK, Role.BLACK), (Role.WHITE, Role.WHITE), (Role.GREY, Role.WHITE)):
            screen = one(group, Surface.SCREEN, screen_role).rect.centre()
            paper = in_screen_plane(one(group, Surface.PAPER, paper_role).rect).centre()
            assert math.dist(screen, paper) <= PAIR_MM, (group.index, screen_role)


def test_paper_words_are_free():
    words = TARGET.paper_words
    assert words.width_mm >= 120 and words.height_mm >= 30
    assert inside(words, Rect(0.0, 0.0, *PAPER_TARGET_MM))
    for rect in [p.rect for p in PAPER_PATCHES] + [m.rect for m in PAPER_MARKERS]:
        assert not overlap(words, rect), rect


# -- 3. Point lookups agree with the layout ----------------------------------------------------

def test_screen_point_at_every_patch_centre_is_its_signal():
    for patch in SCREEN_PATCHES:
        point = screen_point(*patch.rect.centre())
        assert (point.signal, point.raw, point.qr) == (patch.signal, False, False), patch


def test_screen_point_in_the_dark_box_is_raw_black():
    box = TARGET.dark_box
    for fx, fy in ((0.01, 0.01), (0.5, 0.5), (0.99, 0.99), (0.2, 0.8)):
        point = screen_point(box.x_mm + fx*box.width_mm, box.y_mm + fy*box.height_mm)
        assert (point.signal, point.raw, point.qr) == (0.0, True, False)


def test_screen_point_in_the_qr_square_is_the_qr():
    qr = TARGET.qr
    for fx, fy in ((0.01, 0.01), (0.5, 0.5), (0.99, 0.99)):
        point = screen_point(qr.x_mm + fx*qr.width_mm, qr.y_mm + fy*qr.height_mm)
        assert point.qr and not point.raw


def marker_modules(marker: Marker):
    """(module centre, white?) for every module of a marker, by its border and MARKER_BITS."""
    module = module_mm(marker)
    for row, column in itertools.product(range(MARKER_MODULES), repeat=2):
        centre = (marker.rect.x_mm + (column + .5)*module, marker.rect.y_mm + (row + .5)*module)
        border = row in (0, MARKER_MODULES - 1) or column in (0, MARKER_MODULES - 1)
        yield centre, (not border and MARKER_BITS[marker.marker_id][row - 1][column - 1] == "1")


def test_screen_point_draws_each_marker_and_its_quiet_zone():
    for marker in SCREEN_MARKERS:
        for centre, white in marker_modules(marker):
            assert screen_point(*centre).signal == (1.0 if white else 0.0), (marker.marker_id, centre)
        module, rect = module_mm(marker), marker.rect
        for x, y in ((rect.x_mm - module/2, rect.y_mm + rect.height_mm/2),
                     (rect.x_mm + rect.width_mm + module/2, rect.y_mm - module/2)):
            assert screen_point(x, y).signal == 1.0, (marker.marker_id, x, y)


def test_screen_point_outside_the_target_is_the_surround():
    width, height = SCREEN_TARGET_MM
    for x, y in ((-1.0, 10.0), (width, 10.0), (100.0, height + 5), (100.0, -0.01)):
        point = screen_point(x, y)
        assert (point.signal, point.raw, point.qr) == (0.0, False, False)
    # Between the groups in the ring: the surround.
    assert screen_point(TARGET.dark_box.x_mm - 1, TARGET.dark_box.y_mm + 1) == screen_point(-1.0, -1.0)


def test_paper_ink_is_the_blacks_and_the_markers_black_modules():
    for patch in PAPER_PATCHES:
        assert paper_ink(*patch.rect.centre()) is (patch.role is Role.BLACK), patch
    for marker in PAPER_MARKERS:
        for centre, white in marker_modules(marker):
            assert paper_ink(*centre) is (not white), (marker.marker_id, centre)
    width, height = PAPER_TARGET_MM
    assert not any(paper_ink(x, y) for x, y in ((-0.1, 1.0), (width, 1.0), (1.0, height), (1.0, -1.0)))
    assert not paper_ink(*TARGET.paper_words.centre())


# -- 4. Print fit ------------------------------------------------------------------------------

def test_the_paper_content_fits_letter_and_a4_inside_the_margins():
    for sheet in (LETTER_MM, A4_MM):
        assert PAPER_TARGET_MM[0] <= sheet[0] - 2*PRINT_MARGIN_MM
        assert PAPER_TARGET_MM[1] <= sheet[1] - 2*PRINT_MARGIN_MM


def test_the_dark_box_holds_either_sheet_with_slack():
    for axis in (0, 1):
        assert DARK_BOX_MM[axis] >= max(LETTER_MM[axis], A4_MM[axis]) + 2*BOX_SLACK_MM
    assert (TARGET.dark_box.width_mm, TARGET.dark_box.height_mm) == DARK_BOX_MM


def test_the_screen_target_holds_box_ring_markers_and_qr():
    plane = Rect(0.0, 0.0, *SCREEN_TARGET_MM)
    rects = ([TARGET.dark_box, TARGET.qr] + [p.rect for p in SCREEN_PATCHES]
             + [with_quiet_zone(m) for m in SCREEN_MARKERS])
    assert all(inside(rect, plane) for rect in rects)


# -- 5. The code -------------------------------------------------------------------------------

@pytest.mark.parametrize("output_id", OUTPUT_IDS)
@pytest.mark.parametrize("change", [1, 2**63 - 1])
def test_code_text_round_trips(output_id, change):
    code = TargetCode(PLAYER_ID, output_id, change)
    text = code_text(code)
    assert text == f"PWCT1 {PLAYER_ID} {output_id} {change}"
    assert parsed_code(text) == code


@pytest.mark.parametrize("text", [
    f"PWCT2 {PLAYER_ID} HDMI-A-1 3", f"PWCT1 {PLAYER_ID} HDMI-A-1", f"PWCT1 {PLAYER_ID} HDMI-A-1 3 4",
    f"PWCT1 {PLAYER_ID} HDMI-A-1 0", f"PWCT1 {PLAYER_ID} HDMI-A-1 01", f"PWCT1 {PLAYER_ID} HDMI-A-1 -1",
    "PWCT1 -bad HDMI-A-1 3", f"PWCT1 {PLAYER_ID} HDMI A-1 3", f"PWCT1  {PLAYER_ID} HDMI-A-1 3",
    f"PWCT1 {PLAYER_ID} HDMI-A-1 {2**63}", f"PWCT1 {PLAYER_ID} HDMI-A-1 3 ", "", "https://example.com",
])
def test_parsed_code_refuses_anything_else(text):
    assert parsed_code(text) is None


@pytest.mark.parametrize("fields", [
    (PLAYER_ID, "HDMI-A-1", 0), (PLAYER_ID, "HDMI-A-1", True), (PLAYER_ID, "HDMI-A-1", -1),
    (PLAYER_ID, "HDMI-A-1", 2**63), ("bad id", "HDMI-A-1", 1), (PLAYER_ID, "", 1), (PLAYER_ID, "HDMI-A-1", 1.0),
])
def test_target_code_refuses_a_bad_field(fields):
    with pytest.raises(ValueError, match="target_code"):
        TargetCode(*fields)
