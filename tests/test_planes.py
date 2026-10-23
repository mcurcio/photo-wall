"""A screen's planes and their strengths: the display controller mixes stored sRGB values, so a
fade over a picture keeps its alpha and a dip to black takes its sRGB encoding (bench T3). Each
picture is finished at the smaller of its cropped source and its placement, and the controller
enlarges it to the placement (owner, 2026-10-18)."""
from __future__ import annotations

import pytest
from test_executor import layer, media

from contracts.frame_fit import Placement
from contracts.models import PhotoCrop
from contracts.photo_crop import crop_window
from player.geometry import encode_srgb
from player.planes import MIN_STRENGTH, FrameLayers, Screen, compose, finish_size, plane_strength
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


def test_a_dissolve_keeps_the_outgoing_on_top_fading_out_over_the_incoming_at_full():
    """A shown picture keeps its plane: the incoming enters beneath at full, the outgoing above it
    at 1 - alpha, which the controller's stored-value mix makes the same picture as the incoming at
    alpha over the outgoing."""
    assert strengths(photo("out", 1.0, start=100), photo("in", 0.3, start=101)) == [("in", 1.0), ("out", 0.7)]


def overlays(planes) -> list[tuple[str, float]]:
    """Weston 14's DRM backend gives a screen's views KMS overlay planes from the top down, each
    the first free overlay of its plane list (libweston/backend-drm/state-propose.c
    drm_output_find_plane_for_view, zpos zpos_max then current_lowest - 1, tracer runs on the test
    Pi: 107, then 118): overlay i carries the plane i-th from the top. Each overlay's assignment
    and its share of what the viewer sees: its strength times (1 - strength) of every plane above
    it, the controller's stored-value mix."""
    shares, above = [], 1.0
    for plane in reversed(planes):
        shares.append((plane.layer.layer.assignment_id, plane.strength * above))
        above *= 1 - plane.strength
    return shares


DISSOLVE_SHARES = (0.0, 0.0005, 0.01, 0.02, 0.05, 0.5, 0.98, 0.999, 1.0)


def dissolve(share: float, incoming: LocalLayer | None = None):
    incoming = incoming or photo("in", share, start=101)
    return compose(SCREEN, [FrameLayers("frame1", FULL, (photo("out", 1.0, start=100),
                                                         LocalLayer(incoming.layer, None, 0.0, share)))]).planes


def test_no_commit_of_a_dissolve_switches_on_an_overlay_the_viewer_sees():
    """The owner, 2026-10-19: "it shows a black frame at the start of the transition". The test
    Pi's display controller shows nothing from an overlay a commit switches on for its first
    refresh (EVIDENCE: at the start the outgoing photo moved from overlay 107 to 118, switched on
    by that commit). Walked from one photo alone through a dissolve and back to one, every overlay
    a commit switches on carries under 5 % of the picture, and the picture on screen before keeps
    its overlay."""
    before = [("out", 1.0)]                             # the outgoing alone, on the first overlay
    for share in DISSOLVE_SHARES:
        shown = overlays(dissolve(share))
        assert all(seen < 0.05 for _, seen in shown[len(before):]), (share, shown)
        names = [name for name, _ in shown]
        assert before[0][0] not in names or names[0] == before[0][0], (share, shown)    # it keeps its overlay
        before = shown
    assert shown == [("in", pytest.approx(1.0))]        # after it, the incoming alone at full


def test_a_dissolve_shows_the_stored_value_mix_of_its_share():
    for share in DISSOLVE_SHARES:
        mix = dict(overlays(dissolve(share)))
        assert mix.get("in", 0.0) == pytest.approx(share, abs=MIN_STRENGTH), (share, mix)
        assert mix.get("out", 0.0) == pytest.approx(1 - share, abs=MIN_STRENGTH), (share, mix)


def test_a_png_incoming_may_be_transparent_so_it_fades_in_over_the_outgoing():
    png = LocalLayer(layer("in", start=101), None, 0.0, 0.3)
    assert png.layer.variant.media_type == "image/png"
    assert [(p.layer.layer.assignment_id, pytest.approx(p.strength, abs=1e-12)) for p in dissolve(0.3, png)] == [
        ("out", 1.0), ("in", 0.3)]


def test_a_fade_in_from_a_black_layer_keeps_the_black_on_top_at_the_same_mix():
    dip = encode_srgb(0.3)
    assert strengths(black("black", 1.0, start=100), photo("over", 0.3, start=101)) == [
        ("over", 1.0), ("black", pytest.approx(1 - dip))]


def test_a_dissolve_over_a_fading_picture_keeps_its_order():
    """Neither is opaque, so no order gives the same mix: the incoming fades in over the outgoing."""
    assert strengths(photo("out", 0.6, start=100), photo("in", 0.3, start=101)) == [
        ("out", encode_srgb(0.6)), ("in", 0.3)]


def test_a_dip_to_black_takes_the_alpha_s_srgb_encoding():
    dip = encode_srgb(0.3)
    assert strengths(photo("alone", 0.3)) == [("alone", dip)]


def test_a_layer_under_an_opaque_one_has_no_plane():
    assert strengths(photo("under", 1.0, start=100), photo("over", 1.0, start=101)) == [("over", 1.0)]


def test_a_plane_too_weak_to_show_is_removed_never_left_transparent():
    """An outgoing dissolve at 1 - 0.999 is under MIN_STRENGTH; a dip at 0.001 encodes to about 0.0129."""
    assert strengths(photo("out", 1.0, start=100), photo("in", 0.999, start=101)) == [("in", 1.0)]
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


def sized(key: str, width: int, height: int, *, video: bool = False, **fields) -> LocalLayer:
    """A photo (or a video) whose prepared copy is `width` x `height`."""
    variant = media(key.encode(), video).model_copy(update={"width": width, "height": height, **(
        {} if video else {"media_type": "image/jpeg"})})
    return LocalLayer(layer(key, variant=variant, video=video, **fields), None, 0.0, 1.0)


HD = Placement(960, 540, 1920, 1080)


@pytest.mark.parametrize(("local", "at", "size", "enlarged"), [
    (sized("video", 1920, 1080, video=True), FULL, (1920, 1080), True),     # a 1080p video in a 4K Frame
    (sized("large", 6000, 4000), HD, (1920, 1080), False),                  # a large photo, shown 1:1
    (sized("small", 1280, 720), HD, (1280, 720), True),                     # a small photo, enlarged
    (sized("tall", 1080, 1920), FULL, (1080, 608), True),                   # its crop is 1080 x 607.5
    (sized("exact", 3840, 2160), FULL, (3840, 2160), False),
])
def test_a_picture_is_finished_at_the_smaller_of_its_cropped_source_and_its_placement(local, at, size, enlarged):
    plane, = compose(SCREEN, [FrameLayers("frame1", at, (local,))]).planes
    assert plane.size == finish_size(local.layer, at) == size
    assert plane.at == at and plane.enlarged is enlarged


def test_the_finish_size_keeps_the_frames_aspect_whatever_the_crop():
    for offset in (0, 0.3, 1):
        local = sized("wide", 4000, 1000, crop=PhotoCrop(offset=offset))       # its crop: 1778 x 1000
        assert finish_size(local.layer, FULL) == (1778, 1000)
        assert finish_size(local.layer, HD) == (1778, 1000)


@pytest.mark.parametrize("at", [Placement(0, 0, 720, 1080), Placement(0, 0, 1080, 1080),
                                Placement(0, 0, 1280, 1600)])
def test_the_finish_size_is_the_crop_window_taken_at_the_placements_own_aspect(at):
    """A 1,500 x 1,000 photo is smaller than each of these Frames' placements, so its crop window
    limits the scale: the picture is finished at the window's own pixels, and the window is the one
    taken at the placement's width : height, not the screen's.
    Mutation probe: take the window at a fixed 16 : 9 -> each size here changes."""
    local = sized("small", 1500, 1000, crop=PhotoCrop(offset=0.3))
    left, top, right, bottom = crop_window(PhotoCrop(offset=0.3), 1500, 1000, at.width / at.height)
    scale = min((right - left) * 1500 / at.width, (bottom - top) * 1000 / at.height)
    assert scale < 1
    assert finish_size(local.layer, at) == (round(at.width * scale), round(at.height * scale))
    assert finish_size(local.layer, at) == {720: (667, 1000), 1080: (1000, 1000), 1280: (800, 1000)}[at.width]


def test_a_black_layer_is_finished_at_its_placement():
    assert finish_size(black("dark", 1.0).layer, HD) == (1920, 1080)
