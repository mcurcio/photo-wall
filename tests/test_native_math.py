from __future__ import annotations

import math
import random
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

import player.native as native_module
from contracts.frame_fit import cover_rectangle
from contracts.models import Calibration, CornersFit, CoverFit
from contracts.node_frame import PictureOperations
from player.geometry import (
    compose_pixel,
    decode_channel,
    encode_srgb,
    final_pixel,
    homography,
    inverse,
    output_pixel,
    output_to_picture,
    photo_rect,
    picture_source_uv,
    transform,
)
from player.native import NativeOutput, NativeRenderer, SampleMailbox, diagnostic_text, packed_rgba
from player.rendering import REDRAW_RENEWAL, PrepareResult


def test_local_diagnostic_distinguishes_startup_from_enrolled_unbound_output():
    startup = diagnostic_text("HDMI-A-1", serial="0123456789abcdef")
    assert "OS and Player running" in startup
    assert "Central: connecting" in startup
    assert "enrolled" not in startup
    enrolled = diagnostic_text("HDMI-A-1", "p-" + "a" * 32,
                               serial="0123456789abcdef", central_link_state="reachable",
                               configuration_received=True)
    assert "Central: reachable; configuration received" in enrolled
    assert "No Frame assigned" in enrolled
    assert "Output HDMI-A-1" in enrolled
    assert "Player p-…aaaaaaaa" in enrolled
    assert "Serial 0123456789abcdef" in enrolled
    retrying = diagnostic_text("HDMI-A-1", "p-" + "a" * 32,
        central_link_state="retrying", configuration_received=True)
    assert "Central: retrying; last configuration received" in retrying
    before_first_config = diagnostic_text("HDMI-A-1", "p-" + "a" * 32,
        central_link_state="retrying", configuration_received=False)
    assert "Central: retrying; no configuration received" in before_first_config


def test_native_diagnostic_is_hidden_for_bound_outputs():
    class Widget:
        def __init__(self):
            self.visible = True
            self.text = ""

        def show(self):
            self.visible = True

        def hide(self):
            self.visible = False

        def set_text(self, value):
            self.text = value

    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._owner = threading.get_ident()
    renderer._closed = False
    renderer._serial_label = "0123456789abcdef"
    first, second = Widget(), Widget()
    renderer._surfaces = {
        "HDMI-A-1": SimpleNamespace(diagnostic=first, diagnostic_label=Widget()),
        "HDMI-A-2": SimpleNamespace(diagnostic=second, diagnostic_label=Widget()),
    }
    renderer.set_unbound_outputs(("HDMI-A-1",), "p-" + "a" * 32, "reachable", True)
    assert first.visible
    assert not second.visible
    assert "No Frame assigned" in renderer._surfaces["HDMI-A-1"].diagnostic_label.text
    renderer.set_unbound_outputs((), "p-" + "a" * 32, "reachable", True)
    assert not first.visible and not second.visible


def test_native_identify_banner_is_high_contrast_and_target_only():
    class Widget:
        def __init__(self):
            self.visible = False
            self.text = ""
        def show_all(self): self.visible = True
        def hide(self): self.visible = False
        def get_child(self): return self
        def set_text(self, value): self.text = value

    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._owner = threading.get_ident()
    renderer._closed = False
    first, second = Widget(), Widget()
    renderer._surfaces = {
        "HDMI-A-1": SimpleNamespace(identify_banner=first),
        "HDMI-A-2": SimpleNamespace(identify_banner=second),
    }
    renderer.set_identify_output("HDMI-A-1")
    assert first.visible and not second.visible
    assert "IDENTIFY THIS OUTPUT" in first.text
    renderer.set_identify_output(None)
    assert not first.visible and not second.visible


def test_projective_corners_and_interior_roundtrip():
    corners = ((.15, .1), (.9, .2), (.8, .95), (.05, .7))
    forward = homography(corners)
    backward = inverse(forward)
    for logical, output in zip(((0, 0), (1, 0), (1, 1), (0, 1)), corners, strict=True):
        assert transform(forward, *logical) == pytest.approx(output)
        assert transform(backward, *output) == pytest.approx(logical, abs=1e-10)
    for x, y in ((.1, .8), (.4, .2), (.7, .65)):
        assert transform(backward, *transform(forward, x, y)) == pytest.approx((x, y))
    assert transform(forward, .5, .5) != pytest.approx((.475, .5))


def test_singular_or_nonfinite_homography_rejected():
    with pytest.raises(ValueError):
        homography(((0, 0), (1, 0), (2, 0), (3, 0)))
    with pytest.raises(ValueError):
        homography(((math.inf, 0), (1, 0), (1, 1), (0, 1)))
    with pytest.raises(ValueError):
        inverse((0,)*9)


@pytest.mark.parametrize("rotation, expected", [(0, (.2, .3)), (90, (.3, .8)),
                                                 (180, (.8, .7)), (270, (.7, .2))])
def test_corners_undoes_the_quarter_turn(rotation, expected):
    fit = CornersFit(rotation=rotation)
    assert picture_source_uv(.2, .3, fit, (100, 100), 1, 100, 100) == pytest.approx(expected)


def test_the_opening_clips_before_the_quarter_turn():
    fit = CornersFit(corners=((.2, .2), (.8, .2), (.8, .8), (.2, .8)), rotation=90)
    assert picture_source_uv(.1, .4, fit, (100, 100), 1, 100, 100) is None
    assert picture_source_uv(.5, .5, fit, (100, 100), 1, 100, 100) == pytest.approx((.5, .5))


# -- roadmap 1c R1: one matrix per Frame fit, masks per Frame and per Output, the final pass ------

HD = (1920, 1080)
CORNER_SETS = (
    ((0, 0), (1, 0), (1, 1), (0, 1)),
    ((.2, .2), (.8, .2), (.8, .8), (.2, .8)),
    ((.15, .1), (.9, .2), (.8, .95), (.05, .7)),
    ((.05, .05), (.95, .02), (.98, .97), (.03, .93)),
    ((.3, .1), (.7, .1), (.95, .9), (.05, .9)),
    ((.1, .4), (.6, .05), (.9, .6), (.4, .95)),
)
OPENING = ((.1, .12), (.85, .08), (.9, .9), (.12, .82))


def _operations(rgb=(1.0, 1.0, 1.0), light=1.0, slope=1.0, exponent=1.0):
    return PictureOperations("HDMI-A-1", 1, rgb, light, slope, exponent)


# Where a clockwise quarter turn puts picture point (s, t) on the unit square the corners span.
TURNED = {0: lambda s, t: (s, t), 90: lambda s, t: (1 - t, s),
          180: lambda s, t: (1 - s, 1 - t), 270: lambda s, t: (t, 1 - s)}


@pytest.mark.parametrize("corners", CORNER_SETS)
@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_corners_fit_stretches_the_picture_onto_the_corners(corners, rotation):
    """R1 case 1: Corners samples, at the output point where the forward
    perspective puts a turned picture point, exactly that picture point."""
    rng = random.Random(f"{corners}{rotation}")
    fit = CornersFit(corners=corners, rotation=rotation)
    forward = homography(corners)
    for _ in range(200):
        s, t = rng.uniform(.01, .99), rng.uniform(.01, .99)
        x, y = transform(forward, *TURNED[rotation](s, t))
        assert picture_source_uv(x, y, fit, HD, 1.6, 320, 200) == pytest.approx((s, t), abs=1e-9)


def _picture_edges(matrix) -> tuple[tuple[float, float], tuple[float, float]]:
    """The linear part of `matrix` from output PIXELS to the picture, inverted: the picture's u
    and v edges in output pixels."""
    a, b = matrix[0] / HD[0], matrix[1] / HD[1]
    c, d = matrix[3] / HD[0], matrix[4] / HD[1]
    det = a*d - b*c
    return (d/det, -c/det), (-b/det, a/det)


@pytest.mark.parametrize("aspect", (16 / 9, 9 / 16))
@pytest.mark.parametrize("rotation", (0, 7, 45, 90, 135))
def test_cover_is_the_smallest_unskewed_rectangle_of_the_profiles_aspect(rotation, aspect):
    """R1 case 2, with the owner's rule of 2026-10-11 (""landscape" and "portrait" are
    descriptions of the aspect ratio. whereas the angle of rotation is a quality of the way frames
    are hung on the wall"): in Cover every opening corner lands in the picture; the picture is
    turned and scaled evenly in output pixels (no skew); its width-to-height is the Frame
    profile's at every angle; and it is the smallest such rectangle: shrunk by 1 % about its
    centre, a corner falls outside. Mutation probes: centre on the corners' average, or take the
    aspect from the corners' bounding box -> this fails."""
    fit = CoverFit(corners=OPENING, rotation=rotation)
    matrix = output_to_picture(fit, HD, aspect)
    mapped = [transform(matrix, x, y) for x, y in OPENING]
    for u, v in mapped:
        assert -1e-9 <= u <= 1 + 1e-9 and -1e-9 <= v <= 1 + 1e-9
    shrunk = [((u - .5) / .99 + .5, (v - .5) / .99 + .5) for u, v in mapped]
    assert any(not (0 <= u <= 1 and 0 <= v <= 1) for u, v in shrunk)
    u_edge, v_edge = _picture_edges(matrix)
    assert u_edge[0]*v_edge[0] + u_edge[1]*v_edge[1] == pytest.approx(0, abs=1e-6)
    assert math.hypot(*u_edge) / math.hypot(*v_edge) == pytest.approx(aspect, rel=1e-9)
    turn = math.radians(rotation)
    assert u_edge[0] / math.hypot(*u_edge) == pytest.approx(math.cos(turn), abs=1e-9)
    assert u_edge[1] / math.hypot(*u_edge) == pytest.approx(math.sin(turn), abs=1e-9)


@pytest.mark.parametrize("rotation", (0, 7, 20, 45, 90))
def test_cover_crops_the_photo_once_to_the_profiles_aspect(rotation):
    """One fit: the photo is cropped to the Frame profile's aspect (16:9 here) and the rectangle
    Cover draws has that aspect too, so one output pixel along the picture's width and one along
    its height cover the same number of photo pixels at any angle: circles stay circles."""
    fit = CoverFit(corners=OPENING, rotation=rotation)
    rectangle = cover_rectangle(OPENING, rotation, HD, 16 / 9)
    turn, step, source = math.radians(rotation), 20, (400, 300)
    x, y = rectangle.centre_x / HD[0], rectangle.centre_y / HD[1]

    def photo_step(dx: float, dy: float) -> float:
        here = picture_source_uv(x, y, fit, HD, 16 / 9, *source)
        there = picture_source_uv(x + dx / HD[0], y + dy / HD[1], fit, HD, 16 / 9, *source)
        return math.hypot((there[0] - here[0]) * source[0], (there[1] - here[1]) * source[1])
    along_width = photo_step(step * math.cos(turn), step * math.sin(turn))
    along_height = photo_step(-step * math.sin(turn), step * math.cos(turn))
    assert along_width == pytest.approx(along_height, rel=1e-9)


def test_cover_at_a_quarter_turn_puts_the_picture_top_on_the_right():
    """A clockwise quarter turn on the full screen of a Frame hung on its side (a 9:16 profile):
    the picture's top-left is the output's top-right, as Corners' quarter turn has it."""
    matrix = output_to_picture(CoverFit(rotation=90), HD, 9 / 16)
    assert transform(matrix, 1, 0) == pytest.approx((0, 0))
    assert transform(matrix, 1, 1) == pytest.approx((1, 0))
    assert transform(matrix, 0, 0) == pytest.approx((0, 1))


def test_a_frame_draws_nothing_outside_its_opening():
    """R1 case 3: inside the cover rectangle but outside the opening, the Frame samples nothing."""
    fit = CoverFit(corners=((.3, .3), (.7, .3), (.7, .7), (.3, .7)), rotation=30)
    backward = inverse(output_to_picture(fit, HD, 16 / 9))
    x, y = transform(backward, .1, .1)          # near the picture's top-left, outside the opening
    assert 0 <= x <= 1 and 0 <= y <= 1
    assert 0 < transform(output_to_picture(fit, HD, 16 / 9), x, y)[0] < 1
    assert picture_source_uv(x, y, fit, HD, 16 / 9, 100, 100) is None
    assert picture_source_uv(.5, .5, fit, HD, 16 / 9, 100, 100) is not None


def test_two_frames_on_one_output_keep_to_their_openings():
    """R1 case 4 (owner 2026-10-10 q2: outside a Frame is "someone else's territory"): Frame A red,
    Frame B green on one output, contrast slope 0.5. Each paints only its own opening; between
    and around them is exactly opaque black. B is turned 30 degrees, so its cover rectangle
    reaches over A's opening and only the per-Frame mask keeps it out."""
    a = ((.04, .1), (.46, .1), (.46, .9), (.04, .9))
    b = ((.54, .15), (.95, .1), (.92, .9), (.58, .85))
    frames = ((CoverFit(corners=a), 9 / 16, (1.0, 0.0, 0.0)),
              (CoverFit(corners=b, rotation=30), 9 / 16, (0.0, 1.0, 0.0)))
    operations = _operations(slope=.5)
    red, green, black = (.75, .25, .25, 1), (.25, .75, .25, 1), (0, 0, 0, 1)   # worked by hand
    seen = set()
    for i in range(97):
        for j in range(61):
            x, y = (i + .5) / 97, (j + .5) / 61
            pixel = output_pixel(x, y, frames, operations, HD)
            in_a = .04 < x < .46 and .1 < y < .9
            in_b = transform(output_to_picture(CornersFit(corners=b), HD, 1), x, y)
            in_b = 0 < in_b[0] < 1 and 0 < in_b[1] < 1
            expected = red if in_a else green if in_b else black
            assert pixel == pytest.approx(expected, abs=1e-12), (x, y)
            seen.add(expected)
    assert seen == {red, green, black}


# Linear grey inputs through operations (0.9, 0.95, 1), light 1.2, slope 0.8, exponent 24/22,
# worked by hand from the sRGB encoding (IEC 61966-2-1): a = min(1, L*m*1.2); e = sRGB(a);
# c = (e - 0.5)*0.8 + 0.5; out = c^(24/22). E.g. L = 0.5, m = 0.9: a = 0.54, e = 0.76111,
# c = 0.70889, out = 0.68706.
FINAL_PASS_TABLE = (
    (0.0, (0.0811131, 0.0811131, 0.0811131)),
    (0.18, (0.4516162, 0.4615475, 0.4711953)),
    (0.5, (0.6870604, 0.7028032, 0.7180977)),
    (1.0, (0.8914207, 0.8914207, 0.8914207)),
)


@pytest.mark.parametrize("grey, expected", FINAL_PASS_TABLE)
def test_final_pass_order_matches_the_hand_worked_table(grey, expected):
    """R1 case 5: white balance, light, clamp, encode, contrast, gamma, clamp, in that order."""
    operations = _operations((.9, .95, 1.0), 1.2, .8, 24/22)
    assert final_pixel((grey,)*3, operations, inside=True) == pytest.approx((*expected, 1), abs=1e-6)


def test_the_output_mask_comes_after_the_contrast_lift():
    """Black outside every opening stays black at low contrast: the mask is the last step."""
    assert final_pixel((0, 0, 0), _operations(slope=.5), inside=False) == (0, 0, 0, 1)
    assert final_pixel((0, 0, 0), _operations(slope=.5), inside=True)[:3] == pytest.approx((.25,)*3)


@pytest.mark.parametrize("encoded", ((0, 0, 0), (.2, .5, .9), (1, 1, 1), (.04, .7, .3)))
def test_neutral_operations_change_nothing(encoded):
    linear = tuple(decode_channel(v) for v in encoded)
    assert final_pixel(linear, _operations(), inside=True) == pytest.approx(
        compose_pixel([((*encoded, 1), 1, False)]), abs=1e-12)


def test_photo_rect_is_centred_cover_without_trim():
    assert photo_rect(1, 200, 100) == pytest.approx((.25, 0, .75, 1))
    assert photo_rect(2, 200, 100) == pytest.approx((0, 0, 1, 1))
    assert photo_rect(4, 200, 100) == pytest.approx((0, .25, 1, .75))
    assert photo_rect(.5, 200, 100) == pytest.approx((.375, 0, .625, 1))
    with pytest.raises(ValueError):
        photo_rect(0, 200, 100)
    with pytest.raises(ValueError):
        photo_rect(1, 0, 100)


def test_srgb_and_bt709_transfer():
    assert decode_channel(.04045) == pytest.approx(.00313080495)
    assert decode_channel(.081-1e-9, video=True) == pytest.approx(.018)
    assert decode_channel(.5, video=True) > decode_channel(.5)
    for value in (0, .002, .03, .3, .8, 1):
        assert encode_srgb(decode_channel(value)) == pytest.approx(value, abs=1e-7)
    assert compose_pixel([((.5, .5, .5, 1), 1, False)])[:3] == pytest.approx((.5,)*3)


def test_linear_premultiplied_alpha_black_and_transparency():
    white = ((1, 1, 1, 1), 1, False)
    assert compose_pixel([white, ((0, 0, 0, 1), 0, False)]) == pytest.approx((1, 1, 1, 1))
    assert compose_pixel([white, ((0, 0, 0, 1), 1, False)]) == (0, 0, 0, 1)
    assert compose_pixel([white, ((0, 0, 0, 1), .5, False)]) == pytest.approx(
        (encode_srgb(.5),)*3 + (1,)
    )
    # Straight source alpha and layer fade both apply exactly once.
    assert compose_pixel([((1, 0, 0, .5), .5, False)]) == pytest.approx(
        (encode_srgb(.25), 0, 0, 1)
    )
    assert compose_pixel([]) == (0, 0, 0, 1)


def test_rgba_padding_offset_and_invalid_stride():
    data = b"xx" + b"abcdefgh" + b"PAD!" + b"ijklmnop"
    assert packed_rgba(data, 2, 2, 12, 2) == b"abcdefghijklmnop"
    assert packed_rgba(b"abcdefghijklmnop", 2, 2, 8) == b"abcdefghijklmnop"
    for stride, offset in ((7, 0), (-8, 0), (12, -1), (12, 100)):
        with pytest.raises(ValueError):
            packed_rgba(data, 2, 2, stride, offset)
    with pytest.raises(ValueError):
        packed_rgba(b"short", 2, 2, 8)


def test_mailbox_bound_generation_and_terminal_close():
    mailbox = SampleMailbox()
    for value in range(100):
        mailbox.publish(value)
    assert len(mailbox) == 2
    assert mailbox.latest(0) == 99
    assert len(mailbox) == 0
    mailbox.publish("old")
    mailbox.segment(1)
    mailbox.publish("new")
    assert mailbox.latest(0) is None
    mailbox.publish("newer")
    assert mailbox.latest(1) == "newer"
    mailbox.close()
    mailbox.publish("late callback")
    assert len(mailbox) == 0


def test_output_dimensions_are_bounded():
    with pytest.raises(ValueError):
        NativeOutput("hdmi", "app", 16384, 1080)


def test_decoder_recovery_cannot_reuse_texture_from_previous_incarnation():
    from types import SimpleNamespace

    from player.native import NativeRenderer

    uploads = []
    unmapped = []
    buffer = SimpleNamespace(map=lambda _flags: (True, SimpleNamespace(data=b"new!")),
                             unmap=lambda _mapping: unmapped.append(True))
    sample = SimpleNamespace(get_caps=lambda: "caps", get_buffer=lambda: buffer)
    local = SimpleNamespace(layer=SimpleNamespace(assignment_id="clip",
                                                  variant=SimpleNamespace(width=1, height=1)))
    decoder = SimpleNamespace(failure=None, prepared=True, sample=sample,
                              sample_serial=1, incarnation=2)
    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._decoders = {"clip": decoder}
    renderer._read_sample = lambda _decoder: None
    renderer.Gst = SimpleNamespace(MapFlags=SimpleNamespace(READ=1))
    renderer.GstVideo = SimpleNamespace(
        VideoInfo=SimpleNamespace(new_from_caps=lambda _caps:
                                  SimpleNamespace(stride=(4,), offset=(0,))),
        buffer_get_video_meta=lambda _buffer: None)
    renderer.GL = SimpleNamespace(
        GL_TEXTURE_2D=1, GL_UNPACK_ALIGNMENT=2, GL_RGBA=3, GL_UNSIGNED_BYTE=4,
        glBindTexture=lambda *_args: None, glPixelStorei=lambda *_args: None,
        glTexSubImage2D=lambda *args: uploads.append(args[-1]))
    surface = SimpleNamespace(textures={"clip": (77, 1, 1)})
    assert renderer._upload(surface, local) == 77
    assert uploads == [b"new!"]
    assert unmapped == [True]
    assert surface.textures["clip"] == (77, 2, 1)
    renderer._upload(surface, local)
    assert uploads == [b"new!"]


def test_resize_bounds_peak_memory_before_allocating_replacement_targets():
    from types import SimpleNamespace

    from player.native import NativeRenderer

    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer.GL = object()  # No allocation calls are permitted before this gate.
    renderer.texture_budget = 1100
    renderer._decoders = {}
    surface = SimpleNamespace(programs=(1, 2), output=NativeOutput("out", "app", 4, 4),
                              size=(4, 4), targets=[(1, 11), (2, 12)])
    renderer._surfaces = {"out": surface}
    with pytest.raises(RuntimeError, match="texture budget"):
        renderer._targets(surface, 8, 8)
    assert surface.size == (4, 4)
    assert surface.targets == [(1, 11), (2, 12)]


def test_failed_gl_texture_allocation_does_not_leak_untracked_object():
    from types import SimpleNamespace

    from player.native import NativeRenderer

    deleted = []
    def fail_upload(*_args):
        raise RuntimeError("allocation fault")
    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer.GL = SimpleNamespace(
        GL_TEXTURE_2D=1, GL_TEXTURE_MIN_FILTER=2, GL_TEXTURE_MAG_FILTER=3, GL_LINEAR=4,
        GL_TEXTURE_WRAP_S=5, GL_TEXTURE_WRAP_T=6, GL_CLAMP_TO_EDGE=7, GL_UNPACK_ALIGNMENT=8,
        GL_RGBA8=9, GL_RGBA=10, GL_UNSIGNED_BYTE=11,
        glGenTextures=lambda _count: 19, glBindTexture=lambda *_args: None,
        glTexParameteri=lambda *_args: None, glPixelStorei=lambda *_args: None,
        glTexImage2D=fail_upload, glDeleteTextures=lambda ids: deleted.extend(ids))
    with pytest.raises(RuntimeError, match="allocation fault"):
        renderer._texture(1, 1)
    assert deleted == [19]


def test_failed_fragment_compile_releases_both_shader_objects():
    from types import SimpleNamespace

    from player.native import NativeRenderer

    deleted = []
    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer.GL = SimpleNamespace(
        GL_VERTEX_SHADER=1, GL_FRAGMENT_SHADER=2, GL_COMPILE_STATUS=3,
        glCreateShader=lambda kind: kind, glShaderSource=lambda *_args: None,
        glCompileShader=lambda *_args: None, glGetShaderiv=lambda shader, _status: shader == 1,
        glDeleteShader=lambda shader: deleted.append(shader))
    with pytest.raises(RuntimeError, match="shader compilation"):
        renderer._program("invalid fragment")
    assert deleted == [1, 2]


def test_invalid_negotiated_stride_never_becomes_prepared():
    from types import SimpleNamespace

    from player.native import NativeRenderer

    buffer = SimpleNamespace(get_size=lambda: 100)
    sample = SimpleNamespace(get_caps=lambda: "caps", get_buffer=lambda: buffer)
    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer.GstVideo = SimpleNamespace(
        VideoInfo=SimpleNamespace(new_from_caps=lambda _caps:
                                  SimpleNamespace(width=2, height=2, stride=(7,), offset=(0,))),
        buffer_get_video_meta=lambda _buffer: None)
    decoder = SimpleNamespace(
        mailbox=SimpleNamespace(latest=lambda _generation: sample), generation=1,
        local=SimpleNamespace(layer=SimpleNamespace(variant=SimpleNamespace(width=2, height=2))),
        failure=None, prepared=False, sample=None)
    renderer._read_sample(decoder)
    assert decoder.failure == "decode"
    assert not decoder.prepared and decoder.sample is None


def test_capacity_waits_for_real_surface_initialization():
    import threading

    from player.native import NativeRenderer, _Surface

    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._owner, renderer._closed = threading.get_ident(), False
    renderer._decoders = {}
    renderer.decoder_limit, renderer.texture_budget = 4, 512*1024**2
    surface = _Surface(output=NativeOutput("out", "app", 16, 16), window=None, area=None)
    renderer._surfaces = {"out": surface}
    assert not renderer.capacity(()).available
    surface.programs = (1, 2)
    assert renderer.capacity(()).available
    assert not renderer.capacity(()).qualified


def test_the_frame_renewer_paints_only_surfaces_not_renewed_within_a_renewal():
    """G8 E-G8-4: present() renews a shown draw every REDRAW_RENEWAL, so the display-host
    renewer paints only surfaces with no acknowledgment or one at least that old."""
    class Area:
        def __init__(self):
            self.renders = 0

        def queue_render(self):
            self.renders += 1

    now = time.monotonic()
    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._closed = False
    renderer._surfaces = {
        name: SimpleNamespace(area=Area(), acknowledged=None if completed is None
                              else SimpleNamespace(completed_at=completed))
        for name, completed in (("none", None), ("fresh", now + 60),
                                ("old", now - REDRAW_RENEWAL - .1))}
    assert renderer._renew_display_frames() is True
    assert {name: surface.area.renders for name, surface in renderer._surfaces.items()} == {
        "none": 1, "fresh": 0, "old": 1}


def test_a_crossfade_step_a_trial_edit_or_a_picture_change_is_a_change_to_redraw():
    """G8 render on change, E-G8-3: alpha, the calibration trial and (1c) the Output's picture
    operations change pixels without changing the composition's fingerprint, so each is part of
    what a surface shows."""
    class Frames:
        def __init__(self, candidate, operations=None):
            self.candidate, self.operations = candidate, operations

        def trial(self, composition, grant):
            if self.candidate == "unreadable":
                raise ValueError("unreadable")
            return None if self.candidate is None else SimpleNamespace(candidate=self.candidate)

        def picture(self, output_id):
            assert output_id == "hdmi1"
            if self.operations == "unreadable":
                raise RuntimeError("display_picture_protocol_lost")
            return self.operations

    def trial(sequence):
        return SimpleNamespace(trial_id="t", generation=1, sequence=sequence,
                               candidate_sha256="0" * 64)

    def shown(alpha=1.0, frames=None):
        local = SimpleNamespace(layer="picture", path=None, alpha=alpha)
        composition = SimpleNamespace(binding=SimpleNamespace(output_id="hdmi1"),
                                      calibration=Calibration(), fallback=False, layers=(local,))
        return native_module._shown(composition, (), (), frames, object())

    assert shown() == shown()
    assert shown(.5) != shown(1.0)
    assert shown(frames=Frames(None)) == shown()
    assert shown(frames=Frames(trial(1))) != shown()
    assert shown(frames=Frames(trial(1))) == shown(frames=Frames(trial(1)))
    assert shown(frames=Frames(trial(2))) != shown(frames=Frames(trial(1)))
    unreadable = Frames("unreadable")
    assert shown(frames=unreadable) != shown(frames=unreadable)
    dimmer = _operations(light=.5)
    assert shown(frames=Frames(None, dimmer)) != shown(frames=Frames(None))
    assert shown(frames=Frames(None, dimmer)) == shown(frames=Frames(None, _operations(light=.5)))
    lost = Frames(None, "unreadable")
    assert shown(frames=lost) != shown(frames=lost)


def _presenting_renderer(decoders=None):
    """A NativeRenderer reduced to present(): one surface, every layer prepared, and _render
    played by the test (acknowledge or drop the pending draw)."""
    class Area:
        renders = 0

        def queue_render(self):
            self.renders += 1

    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._owner, renderer._closed, renderer._serial = threading.get_ident(), False, 0
    renderer._decoders = decoders or {}
    renderer._surfaces = {"hdmi1": native_module._Surface(output=None, window=None, area=Area())}
    renderer.capacity = lambda compositions: SimpleNamespace(available=True)
    renderer.prepare = lambda local: PrepareResult("prepared")
    return renderer, renderer._surfaces["hdmi1"]


def _composition(*layers):
    return SimpleNamespace(binding=SimpleNamespace(output_id="hdmi1"), calibration=Calibration(),
                           fallback=False, layers=layers)


def _acknowledge(surface):
    """What _render does with a pending draw it paints."""
    surface.acknowledged, surface.pending = surface.pending, None
    surface.acknowledged.completed_at = time.monotonic()


def test_a_draw_render_dropped_is_drawn_again_on_the_next_present():
    """Render on change keeps one record of what is owed: the newest draw (pending, else
    acknowledged). When _render drops a pending draw (its grant lapsed, its decoder sought, its
    candidate failed), the next present() queues it again at once, not after REDRAW_RENEWAL."""
    renderer, surface = _presenting_renderer()
    photo = _composition(SimpleNamespace(layer=SimpleNamespace(assignment_id="a", variant=None),
                                         path=None, alpha=1.0))
    other = _composition(SimpleNamespace(layer=SimpleNamespace(assignment_id="b", variant=None),
                                         path=None, alpha=1.0))
    assert renderer.present(photo).status == "pending" and surface.area.renders == 1
    _acknowledge(surface)
    assert renderer.present(photo).status == "presented" and surface.area.renders == 1
    assert renderer.present(other).status == "pending" and surface.area.renders == 2
    surface.pending = None                  # _render dropped it
    assert renderer.present(other).status == "pending" and surface.area.renders == 3
    assert surface.pending is not None
    _acknowledge(surface)
    assert renderer.present(other).status == "presented" and surface.area.renders == 3


def test_a_playing_video_is_redrawn_on_each_new_sample_only():
    """Video: a new decoded sample is a change to draw (its serial is in the redraw key); with
    no new sample nothing is redrawn before REDRAW_RENEWAL."""
    layer = SimpleNamespace(assignment_id="v", output_id="hdmi1",
                            variant=SimpleNamespace(duration=10.0, media_type="video/mp4"))
    decoder = SimpleNamespace(incarnation=0, generation=0, sample_serial=1, playing=True,
                              local=SimpleNamespace(layer=layer))
    renderer, surface = _presenting_renderer({"v": decoder})
    video = _composition(SimpleNamespace(layer=layer, path=None, alpha=1.0))
    assert renderer.present(video).status == "pending" and surface.area.renders == 1
    _acknowledge(surface)
    assert renderer.present(video).status == "presented" and surface.area.renders == 1
    decoder.sample_serial += 1
    assert renderer.present(video).status == "presented" and surface.area.renders == 2
    _acknowledge(surface)
    assert renderer.present(video).status == "presented" and surface.area.renders == 2


# -- roadmap 1c R2: picture operations from the shell, the picture change in the tag ------------

class _FrameClient:
    """libphoto-wall-frame-client's two v4 calls, faked: the newest `picture` text per Output,
    and every tag_picture_next_commit's arguments."""

    def __init__(self):
        self.texts: dict[bytes, bytes] = {}
        self.tags: list[tuple[bytes, bytes, bytes]] = []

    def pw_frame_client_picture(self, _client, output_id, buffer, capacity):
        if output_id == b"lost":
            return -1
        if output_id not in self.texts:
            return 0
        assert capacity > len(self.texts[output_id])
        buffer.value = self.texts[output_id]
        return 1

    def pw_frame_client_tag_picture_next_commit(self, _client, _surface, grant_id, tag, change):
        self.tags.append((grant_id, tag, change))
        return 0


def _wayland_frames():
    from player.wayland_frames import WaylandFrames

    frames = WaylandFrames.__new__(WaylandFrames)
    frames.owner, frames.client, frames.trials = threading.get_ident(), object(), {}
    frames.picture_texts, frames.operations = {}, {}
    frames.lib = _FrameClient()
    frames.gdk = SimpleNamespace(gdk_wayland_window_get_wl_surface=lambda _pointer: 1)
    frames._pointer = lambda _window: 2
    return frames


def test_the_player_reads_the_outputs_newest_picture_operations_and_keeps_the_last_good():
    """R2: `picture` decodes the shell's v4 event with contracts.node_frame; an undecodable text,
    or one for another Output, is logged and ignored (the last decoded value stands)."""
    from contracts.node_frame import encode_picture_operations

    frames = _wayland_frames()
    assert frames.picture("HDMI-A-1") is None
    first = _operations(light=.5)
    frames.lib.texts[b"HDMI-A-1"] = encode_picture_operations(first).encode()
    assert frames.picture("HDMI-A-1") == first
    frames.lib.texts[b"HDMI-A-1"] = b'{"not":"operations"}'
    assert frames.picture("HDMI-A-1") == first
    other = PictureOperations("HDMI-A-2", 3, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0)
    frames.lib.texts[b"HDMI-A-1"] = encode_picture_operations(other).encode()
    assert frames.picture("HDMI-A-1") == first
    second = PictureOperations("HDMI-A-1", 7, (.9, .95, 1.0), 1.2, .8, 1.1)
    frames.lib.texts[b"HDMI-A-1"] = encode_picture_operations(second).encode()
    assert frames.picture("HDMI-A-1") == second
    with pytest.raises(RuntimeError, match="display_picture_protocol_lost"):
        frames.picture("lost")


def test_every_tag_names_the_picture_change_its_buffer_was_drawn_with():
    """R2: the tag always goes through tag_picture (frame protocol v4): "0" before any picture
    operations, else the decimal change."""
    from player.wayland_frames import FrameGrant

    frames = _wayland_frames()
    grant = FrameGrant(uuid.uuid4(), 1, 1, "frame", True)
    frames.tag_rendered_buffer(object(), grant, "tag-a")
    frames.tag_rendered_buffer(object(), grant, "tag-b", picture_change=7)
    assert frames.lib.tags == [(str(grant.grant_id).encode(), b"tag-a", b"0"),
                               (str(grant.grant_id).encode(), b"tag-b", b"7")]


def test_a_new_picture_change_is_drawn_again_at_once():
    """R2: present() queues a draw when the Output's picture operations change, though the
    composition and its acknowledgment are unchanged."""
    renderer, surface = _presenting_renderer()

    class Frames:
        operations = None

        def grant(self, _output_id, _composition=None):
            return SimpleNamespace(grant_id=uuid.UUID(int=1), binding_generation=1,
                                   config_revision=1, admitted=True, matches=lambda _c: True)

        def trial(self, _composition, _grant):
            return None

        def picture(self, _output_id):
            return self.operations

    renderer._frames = frames = Frames()
    photo = _composition(SimpleNamespace(layer=SimpleNamespace(assignment_id="a", variant=None),
                                         path=None, alpha=1.0))
    assert renderer.present(photo).status == "pending" and surface.area.renders == 1
    _acknowledge(surface)
    assert renderer.present(photo).status == "presented" and surface.area.renders == 1
    frames.operations = PictureOperations("hdmi1", 7, (1.0, 1.0, 1.0), .5, 1.0, 1.0)
    assert renderer.present(photo).status == "presented" and surface.area.renders == 2
    _acknowledge(surface)
    assert renderer.present(photo).status == "presented" and surface.area.renders == 2
