"""A screen's planes and their strengths: the display controller mixes stored sRGB values, so a
fade over a picture keeps its alpha and a dip to black takes its sRGB encoding (bench T3)."""
from __future__ import annotations

import pytest
from test_executor import layer, media

from contracts.frame_fit import Placement
from player.geometry import encode_srgb
from player.planes import MIN_STRENGTH, FrameLayers, Screen, compose, plane_strength
from player.rendering import LocalLayer

SCREEN = Screen("hdmi1", (3840, 2160), 0)
FULL = Placement(0, 0, 3840, 2160)


def photo(key: str, alpha: float, **fields) -> LocalLayer:
    """A JPEG (a PNG may carry transparency, so it never hides what lies beneath)."""
    jpeg = media(key.encode()).model_copy(update={"media_type": "image/jpeg"})
    return LocalLayer(layer(key, variant=jpeg, **fields), None, 0.0, alpha)


def black(key: str, alpha: float, **fields) -> LocalLayer:
    return LocalLayer(layer(key, presentation="black", variant=None, **fields), None, 0.0, alpha)


def strengths(*layers: LocalLayer, frame: str = "frame1") -> list[tuple[str, float]]:
    """Each plane's assignment and strength; full strength over black is encode_srgb(1), which is
    1 within a float's last bit."""
    planes = compose(SCREEN, [FrameLayers(frame, FULL, layers)]).planes
    return [(plane.layer.layer.assignment_id, pytest.approx(plane.strength, abs=1e-12)) for plane in planes]


def test_a_dissolve_keeps_the_outgoing_opaque_and_the_incoming_at_its_alpha():
    assert strengths(photo("out", 1.0, start=100), photo("in", 0.3, start=101)) == [("out", 1.0), ("in", 0.3)]


def test_a_dip_to_black_takes_the_alpha_s_srgb_encoding():
    dip = encode_srgb(0.3)
    assert strengths(photo("alone", 0.3)) == [("alone", dip)]
    assert strengths(black("black", 1.0, start=100), photo("over", 0.3, start=101)) == [
        ("black", 1.0), ("over", dip)]


def test_a_layer_under_an_opaque_one_has_no_plane():
    assert strengths(photo("under", 1.0, start=100), photo("over", 1.0, start=101)) == [("over", 1.0)]


def test_a_plane_too_weak_to_show_is_removed_never_left_transparent():
    """An incoming dissolve at 0.001 is under MIN_STRENGTH; a dip at 0.001 encodes to about 0.0129."""
    assert strengths(photo("out", 1.0, start=100), photo("in", 0.001, start=101)) == [("out", 1.0)]
    assert strengths(photo("dip", 0.001)) == [("dip", pytest.approx(0.0129, abs=1e-4))]
    assert encode_srgb(0.0001) < MIN_STRENGTH and strengths(photo("dip", 0.0001)) == []


def test_frames_keep_the_order_given_each_at_its_placement():
    left, right = Placement(0, 0, 1920, 1080), Placement(1920, 1080, 1920, 1080)
    composition = compose(SCREEN, [
        FrameLayers("b", right, (photo("b1", 1.0, frame="b"),)),
        FrameLayers("a", left, (photo("a1", 1.0, frame="a"),)),
    ])
    assert [(p.frame_id, p.at, p.layer.layer.assignment_id) for p in composition.planes] == [
        ("b", right, "b1"), ("a", left, "a1")]
    assert composition.screen == SCREEN


def test_plane_strength_over_a_picture_and_over_black():
    assert plane_strength(0.3, True) == 0.3
    assert plane_strength(0.3, False) == encode_srgb(0.3)
    assert plane_strength(1, False) == pytest.approx(1.0)


@pytest.mark.parametrize("alpha", [-0.1, 1.1, float("nan"), float("inf"), True, "0.5", None])
def test_an_alpha_outside_0_to_1_has_no_strength(alpha):
    with pytest.raises(ValueError, match="^plane_alpha$"):
        plane_strength(alpha, True)


def test_two_frames_of_one_id_are_refused():
    with pytest.raises(ValueError, match="^compose_frames$"):
        compose(SCREEN, [FrameLayers("frame1", FULL, ()), FrameLayers("frame1", FULL, ())])


@pytest.mark.parametrize("at", [Placement(-1, 0, 3840, 2160), Placement(1, 0, 3840, 2160),
                                Placement(0, -1, 100, 100), Placement(0, 2100, 100, 100)])
def test_a_placement_off_the_screen_is_refused(at):
    with pytest.raises(ValueError, match="^compose_outside$"):
        compose(SCREEN, [FrameLayers("frame1", at, (photo("p", 1.0),))])


def test_a_layer_for_another_output_is_refused():
    with pytest.raises(ValueError, match="^compose_output$"):
        compose(SCREEN, [FrameLayers("frame1", FULL, (photo("p", 1.0, output="hdmi2"),))])
