from __future__ import annotations

import math
import threading
import time
from types import SimpleNamespace

import pytest

import player.native as native_module
from contracts.models import Calibration
from player.geometry import (
    compose_pixel,
    cover_rect,
    decode_channel,
    encode_srgb,
    homography,
    inverse,
    inverse_rotation,
    source_uv,
    transform,
)
from player.native import NativeOutput, NativeRenderer, SampleMailbox, diagnostic_text, packed_rgba
from player.rendering import REDRAW_RENEWAL


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


def test_crop_once_then_cover_uses_frame_aspect():
    assert cover_rect((.25, 0, .75, 1), 1, 200, 100) == pytest.approx((.25, 0, .75, 1))
    assert cover_rect((.25, 0, .75, 1), 2, 200, 100) == pytest.approx((.25, .25, .75, .75))
    assert cover_rect((0, 0, 1, 1), .5, 200, 100) == pytest.approx((.375, 0, .625, 1))
    calibration = Calibration(crop=(.25, 0, .75, 1))
    assert source_uv(.2, .3, calibration, 1, 200, 100) == pytest.approx((.35, .3))


@pytest.mark.parametrize("rotation, expected", [(0, (.2, .3)), (90, (.3, .8)),
                                                 (180, (.8, .7)), (270, (.7, .2))])
def test_inverse_quarter_turn_before_cover(rotation, expected):
    assert inverse_rotation(.2, .3, rotation) == pytest.approx(expected)
    calibration = Calibration(rotation=rotation, crop=(.25, 0, .75, 1))
    result = source_uv(.2, .3, calibration, 1, 200, 100)
    assert result == pytest.approx((.25+expected[0]*.5, expected[1]))


def test_aperture_clips_before_rotation():
    calibration = Calibration(corners=((.2, .2), (.8, .2), (.8, .8), (.2, .8)), rotation=90)
    assert source_uv(.1, .4, calibration, 1, 100, 100) is None
    assert source_uv(.5, .5, calibration, 1, 100, 100) == pytest.approx((.5, .5))


def test_srgb_and_bt709_transfer_and_gain():
    assert decode_channel(.04045) == pytest.approx(.00313080495)
    assert decode_channel(.081-1e-9, video=True) == pytest.approx(.018)
    assert decode_channel(.5, video=True) > decode_channel(.5)
    for value in (0, .002, .03, .3, .8, 1):
        assert encode_srgb(decode_channel(value)) == pytest.approx(value, abs=1e-7)
    assert compose_pixel([((.5, .5, .5, 1), 1, False)], gain=2)[:3] == pytest.approx(
        (encode_srgb(decode_channel(.5)*2),)*3
    )
    assert compose_pixel([((1, 1, 1, 1), 1, False)], gain=2) == pytest.approx((1, 1, 1, 1))


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
    from types import SimpleNamespace

    from player.native import NativeRenderer

    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer._owner, renderer._closed = threading.get_ident(), False
    renderer._decoders = {}
    renderer.decoder_limit, renderer.texture_budget = 4, 512*1024**2
    surface = SimpleNamespace(output=NativeOutput("out", "app", 16, 16),
                              size=(0, 0), programs=None, failure=None)
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


def test_a_crossfade_step_or_a_trial_edit_is_a_change_to_redraw():
    """G8 render on change, E-G8-3: alpha and the calibration trial change pixels without
    changing the composition's fingerprint, so each is part of what a surface shows."""
    class Frames:
        def __init__(self, candidate):
            self.candidate = candidate

        def trial(self, composition, grant):
            if self.candidate == "unreadable":
                raise ValueError("unreadable")
            return None if self.candidate is None else SimpleNamespace(candidate=self.candidate)

    def trial(sequence):
        return SimpleNamespace(trial_id="t", generation=1, sequence=sequence,
                               candidate_sha256="0" * 64)

    def shown(alpha=1.0, frames=None):
        local = SimpleNamespace(layer="picture", path=None, alpha=alpha)
        composition = SimpleNamespace(binding="hdmi1", calibration=Calibration(),
                                      fallback=False, layers=(local,))
        return native_module._shown(composition, (), (), frames, object())

    assert shown() == shown()
    assert shown(.5) != shown(1.0)
    assert shown(frames=Frames(None)) == shown()
    assert shown(frames=Frames(trial(1))) != shown()
    assert shown(frames=Frames(trial(1))) == shown(frames=Frames(trial(1)))
    assert shown(frames=Frames(trial(2))) != shown(frames=Frames(trial(1)))
    unreadable = Frames("unreadable")
    assert shown(frames=unreadable) != shown(frames=unreadable)
