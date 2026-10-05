"""The overlay client's pure render path: parity with the retired C client (diagnostic-client.c).

Runs anywhere: render.py needs neither cairo nor pywayland, and neither is imported to draw a page.
Real pixels are proven in the display harness (tests/native_display_smoke.py).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from appliance.display_host.overlay import client, render
from appliance.display_host.overlay.render import (
    REASON_DEFAULT,
    REASON_TEXT,
    Arc,
    Line,
    Paint,
    Text,
    parse_trial_points,
    reason_text,
    render_slate,
    render_trial,
)

WHITE = (0.94, 0.96, 1.0, 1.0)
YELLOW = (1.0, 1.0, 0.0, 1.0)


def test_render_path_imports_neither_cairo_nor_pywayland():
    code = ("import sys\n"
            "from appliance.display_host.overlay import render, client\n"
            "render.render_slate('HDMI-A-1', 1920, 1080, 'starting_new', True)\n"
            "render.render_trial(640, 480, ((0, 0), (1, 0), (1, 1), (0, 1)))\n"
            "assert not {'cairo', 'pywayland', '_cffi_backend'} & set(sys.modules), sys.modules\n")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parents[1])


def test_reason_text_is_the_c_clients_map_with_its_default():
    assert dict(REASON_TEXT) == {
        "starting_new": "Starting Player - waiting for authorized handoff",
        "output_mode_changed": "Display mode changed - checking Player output",
        "surface_lease_or_process_lost": "Player output unavailable - management remains separate",
        "authorized_withdrawal": "Player restarting - waiting for new output",
    }
    assert REASON_DEFAULT == "Player output unavailable"
    assert reason_text("app_absent") == REASON_DEFAULT
    assert reason_text("") == REASON_DEFAULT
    with pytest.raises(TypeError):
        REASON_TEXT["app_absent"] = "x"  # type: ignore[index]


@pytest.mark.parametrize(("testing", "alpha"), [(False, 1.0), (True, 0.96)])
def test_slate_background_is_opaque_or_the_testing_alpha(testing, alpha):
    page = render_slate("Virtual-1", 640, 480, "starting_new", testing)
    assert page[0] == Paint((0.04, 0.06, 0.09, alpha), source=True)


def test_slate_base_page_text_and_geometry_at_1280():
    page = render_slate("HDMI-A-1", 1280, 720, "output_mode_changed", False)
    assert page[1:] == (
        Text(48, 90, 36, "Photo Wall", WHITE, "sans"),
        Text(48, 150, 24, "Display mode changed - checking Player output", WHITE, "sans"),
        Text(48, 215, 20, "Output HDMI-A-1  |  1280 x 720 pixels", WHITE, "sans"),
        Text(48, 260, 20, "Frame binding unconfirmed", WHITE, "sans"),
        Text(48, 305, 18, "Base display service - app content is independently supervised",
             WHITE, "sans"),
        Text(48, 345, 18, "Panel brightness unavailable", WHITE, "sans"),
    )


@pytest.mark.parametrize(("width", "scale"), [(1920, 1.5), (640, 0.5), (320, 0.35), (100, 0.35)])
def test_slate_scales_with_the_output_width_never_below_035(width, scale):
    title = render_slate("Virtual-1", width, 480, "app_absent", False)[1]
    assert (title.x, title.y, title.size) == pytest.approx((48 * scale, 90 * scale, 36 * scale))
    assert render_slate("Virtual-1", width, 480, "app_absent", False)[2].text == REASON_DEFAULT


def test_trial_overlay_is_transparent_with_a_closed_quad_numbered_rings_and_title():
    points = ((0.1, 0.2), (0.9, 0.2), (0.9, 0.8), (0.1, 0.8))
    page = render_trial(640, 480, points)
    corners = ((64.0, 96.0), (576.0, 96.0), (576.0, 384.0), (64.0, 384.0))
    assert page[0] == Paint((0.04, 0.06, 0.09, 0.0), source=True)
    assert page[1] == Line(corners, YELLOW, 3.0, closed=True)
    marks = page[2:-1]
    assert marks == tuple(op for number, (x, y) in enumerate(corners, start=1)
                          for op in (Arc(x, y, 9.0, YELLOW, 3.0),
                                     Text(x + 12, y + 18, 20, str(number), YELLOW)))
    assert page[-1] == Text(24, 32, 20, "Live calibration - output-space overlay", YELLOW)


@pytest.mark.parametrize(("primitives", "expected"), [
    ("[[0,0],[1,0],[1,1],[0,1]]", ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))),
    ('[[0.5,"x"],[true,1],[],{"a":1}]', ((0.5, 0.0), (0.0, 1.0), (0.0, 0.0), (0.0, 0.0))),
    ("[[0,0],[1,0],[1,1]]", None),
    ('{"points":[]}', None),
    ("[[NaN,0],[1,0],[1,1],[0,1]]", None),
    ("[[1e999,0],[1,0],[1,1],[0,1]]", None),
    ("[[99999999999999999999,0],[1,0],[1,1],[0,1]]", None),
    ('[{"a":1,"a":2},[1,0],[1,1],[0,1]]', None),
    ("not json", None),
    ("[" + " " * 512 + "]", None),
])
def test_trial_primitives_parse_as_the_c_client(primitives, expected):
    assert parse_trial_points(primitives) == expected


@pytest.mark.parametrize(("width", "height", "buffers", "memory", "admitted"), [
    (1920, 1080, 0, 0, True),
    (4096, 2160, 1, 0, True),
    (4096, 2161, 0, 0, False),        # area cap 4096 x 2160
    (8193, 1, 0, 0, False),           # side cap 8192
    (8192, 1, 0, 0, True),
    (0, 480, 0, 0, False),
    (640, 480, 2, 0, False),          # at most 2 buffers per Output
    (640, 480, 0, client.MAX_MEMORY - 640 * 480 * 4, True),
    (640, 480, 0, client.MAX_MEMORY - 640 * 480 * 4 + 1, False),   # 128 MB in all
])
def test_buffer_caps_are_the_c_clients(width, height, buffers, memory, admitted):
    assert client.buffer_admissible(width, height, buffers, memory) is admitted


def test_main_refuses_without_the_shells_private_socket(monkeypatch):
    monkeypatch.delenv("WAYLAND_SOCKET", raising=False)
    assert client.main() == 2


def test_oom_score_raise_writes_900_and_tolerates_failure(tmp_path, caplog):
    target = tmp_path / "oom_score_adj"
    client.raise_oom_score(str(target))
    assert target.read_text() == "900"
    client.raise_oom_score(str(tmp_path / "missing" / "oom_score_adj"))
    assert "oom_score_adj not raised" in caplog.text


def test_render_module_constants():
    assert (render.OPAQUE_ALPHA, render.TESTING_ALPHA, render.TRIAL_ALPHA) == (1.0, 0.96, 0.0)


class _Proxy:
    def __init__(self, log=None, kind=""):
        self.dispatcher, self.destroyed, self.log, self.kind = {}, False, log, kind

    def destroy(self):
        self.destroyed = True

    def __getattr__(self, request):                   # attach / damage / commit / surface ...
        return lambda *args: self.log.append((self.kind, request, args))


class _Wayland:
    """Fakes for the four globals; `log` records every request in order, `buffers` every wl_buffer."""

    def __init__(self, fail_ack=False):
        self.log, self.buffers, self.fail_ack = [], [], fail_ack
        self.compositor = _Proxy(self.log, "compositor")
        self.compositor.create_surface = lambda: _Proxy(self.log, "surface")
        self.shm = _Proxy(self.log, "shm")
        self.shm.create_pool = lambda fd, size: self._pool()
        self.presentation = _Proxy(self.log, "presentation")
        self.presentation.feedback = lambda surface: _Proxy(self.log, "feedback")
        self.manager = _Proxy(self.log, "manager")
        self.manager.ack = self._ack

    def _pool(self):
        pool = _Proxy(self.log, "pool")
        pool.create_buffer = lambda *args: self.buffers.append(_Proxy(self.log, "buffer")) or self.buffers[-1]
        return pool

    def _ack(self, surface, serial):
        if self.fail_ack:
            raise RuntimeError("connection lost")
        self.log.append(("manager", "ack", (serial,)))

    def acks(self):
        return [args[0] for kind, request, args in self.log if (kind, request) == ("manager", "ack")]


class _Surface:
    def __init__(self, pixels):
        self.pixels = pixels

    def finish(self):
        pass


@pytest.fixture
def overlay_client(monkeypatch, tmp_path):
    """An OverlayClient over fake Wayland globals and file-backed (not memfd) buffers, painting
    through a fake cairo surface: `make(painter, **wayland)` -> (client, wayland, surfaces)."""
    counter = iter(range(1_000_000))
    monkeypatch.setattr(client.os, "MFD_CLOEXEC", 0, raising=False)
    monkeypatch.setattr(client.os, "memfd_create", lambda name, flags: client.os.open(
        tmp_path / f"buffer-{next(counter)}", client.os.O_RDWR | client.os.O_CREAT), raising=False)
    surfaces = []
    monkeypatch.setattr(client, "surface_for",
                        lambda pixels, width, height: surfaces.append(_Surface(pixels)) or surfaces[-1])

    def make(painter, **wayland_options):
        wayland = _Wayland(**wayland_options)
        return (client.OverlayClient(compositor=wayland.compositor, shm=wayland.shm,
                                     presentation=wayland.presentation, manager=wayland.manager,
                                     painter=painter), wayland, surfaces)
    return make


def test_a_paint_failure_still_commits_cleared_and_acks_so_an_output_never_sticks(overlay_client):
    calls = []

    def painter(draw_list, surface):
        calls.append(surface)
        surface.pixels[:4] = b"\xff" * 4               # half-drawn when it fails
        if len(calls) <= 2:
            raise RuntimeError("cairo error")

    overlay, wayland, surfaces = overlay_client(painter)
    configure = wayland.manager.dispatcher["output"]
    configure(wayland.manager, "HDMI-A-1", 64, 32, 1, "starting_new", 0)
    configure(wayland.manager, "HDMI-A-1", 64, 32, 2, "starting_new", 0)
    output = overlay.outputs["HDMI-A-1"]
    assert wayland.acks() == [1, 2]                    # both failed paints still acked ...
    assert sum(request == "commit" for _, request, _ in wayland.log) == 2   # ... and committed
    assert all(bytes(surface.pixels[:4]) == bytes(4) for surface in surfaces)  # cleared, not half
    assert (output.buffers, overlay.memory) == (2, 2 * 64 * 32 * 4)

    configure(wayland.manager, "HDMI-A-1", 64, 32, 3, "starting_new", 0)
    assert (output.dirty, wayland.acks()) == (True, [1, 2])   # at the cap: waits for a release
    first, second = wayland.buffers[:2]
    first.dispatcher["release"](first)
    assert wayland.acks() == [1, 2, 3] and not output.dirty and len(calls) == 3
    second.dispatcher["release"](second)
    third = wayland.buffers[2]
    third.dispatcher["release"](third)
    assert (output.buffers, overlay.memory) == (0, 0)
    assert all(buffer.destroyed for buffer in wayland.buffers)


def test_a_buffer_counts_only_once_committed(overlay_client):
    overlay, wayland, _ = overlay_client(lambda draw_list, surface: None, fail_ack=True)
    with pytest.raises(RuntimeError, match="connection lost"):
        wayland.manager.dispatcher["output"](wayland.manager, "HDMI-A-1", 64, 32, 1, "", 0)
    output = overlay.outputs["HDMI-A-1"]
    assert (output.buffers, overlay.memory, output.dirty) == (0, 0, True)


def test_a_release_whose_pixels_cannot_close_still_frees_its_count(overlay_client, caplog):
    exports = []

    def painter(draw_list, surface):
        exports.append(memoryview(surface.pixels))     # an export outliving the paint
    overlay, wayland, _ = overlay_client(painter)
    wayland.manager.dispatcher["output"](wayland.manager, "HDMI-A-1", 64, 32, 1, "", 0)
    buffer = wayland.buffers[0]
    buffer.dispatcher["release"](buffer)
    output = overlay.outputs["HDMI-A-1"]
    assert (output.buffers, overlay.memory, buffer.destroyed) == (0, 0, True)
    assert "buffer pixels not closed on HDMI-A-1" in caplog.text
    exports[0].release()
