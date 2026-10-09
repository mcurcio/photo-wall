"""The overlay client's health layer: the pure per-Output rules (overlay/health.py) and the hook
that draws them (client.HealthLayer, JudgeLink) over fake Wayland globals and a fake judge.

Runs anywhere: neither cairo nor pywayland is imported. Real pixels and the real shell are proven
in the display harness (tests/native_display_smoke.py, the `production` block).
"""

from __future__ import annotations

import socket
import tempfile

import pytest
from support.packet_pair import LINUX, PACKET_TYPE, packet_pair

from appliance.display_host.overlay import client, health, render
from appliance.display_host.overlay.health import STALE_PAGE, HealthBoard, HealthOutput, HealthPage
from appliance.display_host.overlay.instruction import (
    INSTRUCTION_STALE_MS,
    UNAVAILABLE_LINES,
    OverlayInstruction,
    PresentedReport,
    encode_overlay_instruction,
    parse_presented_report,
)
from appliance.feed_socket import FEED_READERS
from appliance.health import runner

V = INSTRUCTION_STALE_MS
LINES = ("Photos paused - the player stopped responding", "app_unresponsive")


def instruction(serial, tint=True, output="Virtual-1", lines=LINES):
    return OverlayInstruction(output, serial, tint, lines if tint else ("", ""))


def drawn(state: HealthOutput, now: int) -> HealthPage | None:
    page = state.page(now)
    if page is not None:
        state.drawn(page)
    return page


# -- pure rules ---------------------------------------------------------------------------------

def test_the_socket_path_is_the_judges():
    assert health.DEFAULT_HEALTH_SOCKET == str(runner.HEALTH_SOCKET)
    assert health.health_socket_path({}) == health.DEFAULT_HEALTH_SOCKET
    assert health.health_socket_path({"PHOTO_WALL_HEALTH_SOCKET": "/tmp/x.sock"}) == "/tmp/x.sock"
    assert health.JUDGE_UIDS == FEED_READERS                   # root and pw-health


def test_nothing_to_draw_before_the_first_instruction():
    state = HealthOutput("Virtual-1", 0)
    assert state.page(V - 1) is None


def test_an_instruction_draws_once_and_only_a_change_redraws():
    state = HealthOutput("Virtual-1", 0)
    state.instruction(instruction(4, tint=False), 10)
    assert drawn(state, 10) == HealthPage(4, False, ("", ""))
    state.instruction(instruction(4, tint=False), 20)          # the judge's V/3 re-push
    assert state.page(20) is None
    state.instruction(instruction(5), 30)
    assert drawn(state, 30) == HealthPage(5, True, LINES)


def test_any_serial_is_taken_not_only_a_higher_one():
    state = HealthOutput("Virtual-1", 0)
    state.instruction(instruction(9), 10)
    drawn(state, 10)
    assert state.presented(9) == PresentedReport("Virtual-1", 9)
    state.instruction(instruction(1, tint=False), 20)          # a restarted judge counts from 1
    assert drawn(state, 20) == HealthPage(1, False, ("", ""))
    assert state.presented(1) == PresentedReport("Virtual-1", 1)


def test_a_serial_is_reported_once_per_connection_and_only_if_drawn_on_it():
    state = HealthOutput("Virtual-1", 0)
    assert state.presented(3) is None                          # never drawn
    state.instruction(instruction(3), 10)
    drawn(state, 10)
    assert state.presented(3) == PresentedReport("Virtual-1", 3)
    assert state.presented(3) is None


def test_a_discarded_commit_reports_nothing_and_a_later_presentation_still_does():
    state = HealthOutput("Virtual-1", 0)
    state.instruction(instruction(3), 10)
    drawn(state, 10)
    assert state.discarded(3) is None
    assert state.presented(3) == PresentedReport("Virtual-1", 3)


def test_a_reconnect_repaints_and_re_reports_on_the_first_new_instruction():
    state = HealthOutput("Virtual-1", 0)
    state.instruction(instruction(3), 10)
    drawn(state, 10)
    assert state.presented(3) is not None
    state.reconnected()
    assert state.page(20) is None                              # the screen keeps its drawing
    assert state.presented(3) is None                          # a late feedback of the old link
    state.instruction(instruction(3), 30)                      # the same content, re-pushed
    assert drawn(state, 30) == HealthPage(3, True, LINES)
    assert state.presented(3) == PresentedReport("Virtual-1", 3)


def test_stale_after_v_from_start_and_from_the_last_instruction():
    state = HealthOutput("Virtual-1", 100)
    assert state.page(100 + V - 1) is None
    assert state.deadline(100) == 100 + V
    assert drawn(state, 100 + V) == STALE_PAGE == HealthPage(None, True, UNAVAILABLE_LINES)
    state.instruction(instruction(2, tint=False), 100 + V + 5)
    assert drawn(state, 100 + V + 5) == HealthPage(2, False, ("", ""))
    assert state.page(100 + 2 * V + 4) is None
    assert drawn(state, 100 + 2 * V + 5) == STALE_PAGE
    assert state.deadline(100 + 2 * V + 5) is None
    assert state.presented(None) is None                       # a stale page is never reported
    state.instruction(instruction(2, tint=False), 100 + 2 * V + 6)
    assert drawn(state, 100 + 2 * V + 6) == HealthPage(2, False, ("", ""))


def test_a_repaint_draws_again_without_reporting_again():
    state = HealthOutput("Virtual-1", 0)
    state.instruction(instruction(3), 10)
    drawn(state, 10)
    state.presented(3)
    state.repaint()
    assert drawn(state, 20) == HealthPage(3, True, LINES)
    assert state.presented(3) is None


def test_an_instruction_for_another_output_is_refused():
    with pytest.raises(ValueError, match="health_output"):
        HealthOutput("Virtual-1", 0).instruction(instruction(1, output="HDMI-A-1"), 0)


def test_the_board_keeps_instructions_for_outputs_not_configured_yet_within_its_limit():
    board = HealthBoard(limit=2)
    assert board.instruction(instruction(1, output="A"), 5) is None
    assert board.instruction(instruction(2, output="B"), 6) is None
    assert board.instruction(instruction(3, output="C"), 7) is None    # over the limit: dropped
    assert board.instruction(instruction(4, output="A"), 8) is None    # newest per name kept
    assert board.configure("A", 10).page(10) == HealthPage(4, True, LINES)
    assert board.configure("C", 10).page(10) is None
    assert board.deadline(10) == 10 + V
    board.instruction(instruction(5, output="B"), 11)
    board.reconnected()                                         # pending belongs to the old link
    assert board.configure("B", 12).page(12) is None


# -- render -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("width", "height", "rect"), [
    (640, 480, (128, 384, 384, 72)),
    (1920, 1080, (384, 864, 1152, 162)),
    (320, 200, (64, 142, 192, 48)),                             # never under 48 px tall
])
def test_card_rect(width, height, rect):
    assert render.card_rect(width, height) == rect


def test_a_tint_on_page_darkens_the_output_then_draws_the_opaque_card_with_both_lines():
    ops = render.render_health(640, 480, HealthPage(3, True, LINES))
    assert ops[0] == render.Paint((0.0, 0.0, 0.0, 0.45), source=True)
    assert ops[1] == render.Rect(128, 384, 384, 72, (0.04, 0.06, 0.09, 1.0))
    assert [op.text for op in ops[2:]] == list(LINES)
    assert all(op.x > 128 + 2 and op.y - op.size > 384 + 2 for op in ops[2:])  # card corner clear


# -- the hook over fake Wayland -----------------------------------------------------------------

class _Proxy:
    def __init__(self, log, kind, args=()):
        self.dispatcher, self.destroyed, self.log, self.kind, self.args = {}, False, log, kind, args

    def destroy(self):
        self.destroyed = True

    def __getattr__(self, request):
        return lambda *args: self.log.append((self.kind, request, args))


class _Wayland:
    def __init__(self):
        self.log, self.buffers, self.feedbacks = [], [], []
        self.compositor = _Proxy(self.log, "compositor")
        self.compositor.create_surface = self._surface
        self.shm = _Proxy(self.log, "shm")
        self.shm.create_pool = lambda fd, size: self._pool()
        self.presentation = _Proxy(self.log, "presentation")
        self.presentation.feedback = self._feedback
        self.manager = _Proxy(self.log, "manager")
        self.manager.get_health_layer = (
            lambda surface, name: self.log.append(("manager", "get_health_layer", (surface.kind, name)))
            or _Proxy(self.log, "layer"))
        self.viewporter = _Proxy(self.log, "viewporter")
        self.viewporter.get_viewport = lambda surface: _Proxy(self.log, f"viewport-{surface.kind}")
        self.single_pixel = _Proxy(self.log, "single_pixel")
        self.solids = []
        self.single_pixel.create_u32_rgba_buffer = self._solid
        self.surfaces = 0

    def _surface(self):
        self.surfaces += 1
        return _Proxy(self.log, f"surface{self.surfaces}")

    def _pool(self):
        pool = _Proxy(self.log, "pool")

        def create_buffer(offset, width, height, stride, fmt):
            self.buffers.append(_Proxy(self.log, "buffer", (width, height)))
            return self.buffers[-1]
        pool.create_buffer = create_buffer
        return pool

    def _solid(self, *rgba):
        self.solids.append(_Proxy(self.log, "solid", ("solid", *rgba)))
        return self.solids[-1]

    def _feedback(self, surface):
        self.feedbacks.append(_Proxy(self.log, "feedback"))
        return self.feedbacks[-1]

    def health(self, surface="surface2"):
        """The health surface's requests, in order: ("viewport", (w, h)) for its viewport's
        destination, ("attach", (w, h) | ("solid", r, g, b, a) | None) and "commit"."""
        out = []
        for kind, request, args in self.log:
            if kind == f"viewport-{surface}" and request == "set_destination":
                out.append(("viewport", args))
            elif kind == surface and request == "attach":
                out.append(("attach", args[0].args if args[0] is not None else None))
            elif kind == surface and request in ("commit", "ack"):
                out.append(request)
        return out

    def shm_sizes(self):
        """The size of every wl_shm buffer created (slate and health alike)."""
        return [buffer.args for buffer in self.buffers]


def release(wayland):
    """The compositor is done with every buffer (pixman copies shm buffers at once)."""
    for buffer in wayland.buffers:
        if not buffer.destroyed:
            buffer.dispatcher["release"](buffer)


class _Link:
    def __init__(self):
        self.sent = []

    def send(self, report):
        self.sent.append(report)
        return True

    def attach(self, selector):
        pass

    def service(self, now_ms):
        return None


class _Surface:
    def __init__(self, pixels):
        self.pixels = pixels

    def finish(self):
        pass


@pytest.fixture
def layer(monkeypatch, tmp_path):
    """(HealthLayer over fake Wayland and a fake link, wayland, clock list, painted pages)."""
    counter = iter(range(1_000_000))
    monkeypatch.setattr(client.os, "MFD_CLOEXEC", 0, raising=False)
    monkeypatch.setattr(client.os, "memfd_create", lambda name, flags: client.os.open(
        tmp_path / f"buffer-{next(counter)}", client.os.O_RDWR | client.os.O_CREAT), raising=False)
    monkeypatch.setattr(client, "surface_for", lambda pixels, width, height: _Surface(pixels))
    painted = []
    wayland, link, now = _Wayland(), _Link(), [1000]
    hook = client.HealthLayer(clock=lambda: now[0], link=link,
                              painter=lambda ops, surface: painted.append(ops))
    overlay = client.OverlayClient(compositor=wayland.compositor, shm=wayland.shm,
                                   presentation=wayland.presentation, manager=wayland.manager,
                                   viewporter=wayland.viewporter,
                                   single_pixel=wayland.single_pixel,
                                   hooks=(hook,), painter=lambda ops, surface: None)
    configure = wayland.manager.dispatcher["output"]
    return hook, overlay, wayland, link, now, painted, configure


CLEAR = ("solid", 0, 0, 0, 0)    # the transparent single-pixel buffer


def cleared(width=640, height=480):
    """A tint-off commit: the solid buffer scaled to the whole Output."""
    return [("viewport", (width, height)), ("attach", CLEAR), "commit"]


def tinted(width=640, height=480):
    """A tint-on commit: a whole-Output ARGB buffer mapped 1:1."""
    return [("viewport", (width, height)), ("attach", (width, height)), "commit"]


def test_the_layer_is_mapped_transparent_before_any_instruction_and_never_acked(layer):
    hook, overlay, wayland, link, now, painted, configure = layer
    configure(wayland.manager, "Virtual-1", 640, 480, 1, "starting_new", 0)
    assert ("manager", "get_health_layer", ("surface2", "Virtual-1")) in wayland.log
    assert wayland.health() == cleared()       # a solid buffer, scaled to the Output
    assert wayland.shm_sizes() == [(640, 480)]                  # the slate's; no health shm
    assert not painted
    assert [(args[0].kind, args[1]) for kind, request, args in wayland.log
            if request == "ack"] == [("surface1", 1)]           # the slate's configure only


def test_tint_on_draws_the_whole_output_and_tint_off_the_scaled_solid_buffer(layer):
    hook, overlay, wayland, link, now, painted, configure = layer
    configure(wayland.manager, "Virtual-1", 640, 480, 1, "", 0)
    hook.instruction(instruction(7), 1001)
    assert wayland.health()[-3:] == tinted()
    assert painted[-1] == render.render_health(640, 480, HealthPage(7, True, LINES))
    release(wayland)
    hook.instruction(instruction(8, tint=False), 1002)
    assert wayland.health()[-3:] == cleared()
    hook.instruction(instruction(9), 1003)
    assert wayland.health()[-3:] == tinted()                   # the viewport maps it 1:1
    assert "ack" not in wayland.health()
    # Never an shm buffer smaller than the Output (Weston 14 DRM takes one for a cursor); one
    # solid buffer serves every clear commit and costs no cap.
    assert set(wayland.shm_sizes()) == {(640, 480)}
    assert [solid.args for solid in wayland.solids] == [CLEAR]
    assert (hook.surfaces["Virtual-1"].buffers, overlay.memory) == (1, 640 * 480 * 4)  # 9's


def test_a_presented_commit_reports_its_serial_and_a_discarded_one_nothing(layer):
    hook, overlay, wayland, link, now, painted, configure = layer
    configure(wayland.manager, "Virtual-1", 640, 480, 1, "", 0)
    base = wayland.feedbacks[-1]
    base.dispatcher["presented"](base, 0, 0, 0, 0, 0, 0, 0)     # the base page: no serial
    hook.instruction(instruction(7), 1001)
    first = wayland.feedbacks[-1]
    first.dispatcher["discarded"](first)
    assert link.sent == []
    release(wayland)
    hook.instruction(instruction(8, tint=False), 1002)
    second = wayland.feedbacks[-1]
    second.dispatcher["presented"](second, 0, 0, 0, 0, 0, 0, 0)
    assert link.sent == [PresentedReport("Virtual-1", 8)]
    assert first.destroyed and second.destroyed


def test_a_tint_on_page_over_the_size_caps_unmaps_the_layer_for_the_fallback_tint(layer):
    hook, overlay, wayland, link, now, painted, configure = layer
    configure(wayland.manager, "Virtual-1", 8192, 4096, 1, "", 0)   # over the area cap
    assert wayland.health() == cleared(8192, 4096)                  # tint off has no size cap
    hook.instruction(instruction(7), 1001)
    assert wayland.health()[-2:] == [("attach", None), "commit"]
    hook.service(1002)
    assert wayland.health()[-2:] == [("attach", None), "commit"]    # not repeated every pass
    assert len(wayland.health()) == 5
    hook.instruction(instruction(8, tint=False), 1003)
    assert wayland.health()[-3:] == cleared(8192, 4096)


def test_a_failed_paint_of_a_tint_on_page_also_shows_the_fallback(layer):
    hook, overlay, wayland, link, now, painted, configure = layer
    configure(wayland.manager, "Virtual-1", 640, 480, 1, "", 0)

    def broken(ops, surface):
        raise RuntimeError("cairo error")
    hook.painter = broken
    hook.instruction(instruction(7), 1001)
    assert wayland.health()[-2:] == [("attach", None), "commit"]
    assert (hook.surfaces["Virtual-1"].buffers, overlay.memory) == (0, 640 * 480 * 4)  # slate


def test_at_the_buffer_cap_a_tint_on_page_waits_for_a_release_and_tint_off_never_does(layer):
    hook, overlay, wayland, link, now, painted, configure = layer
    configure(wayland.manager, "Virtual-1", 640, 480, 1, "", 0)
    hook.instruction(instruction(7), 1001)
    hook.instruction(instruction(8), 1002)                      # two ARGB buffers held now
    hook.instruction(instruction(9), 1003)
    assert [page[2].text for page in painted] == [LINES[0]] * 2   # 9 waits at the cap
    held = wayland.buffers[1]                                   # [0] is the slate's
    held.dispatcher["release"](held)
    assert len(painted) == 3 and wayland.health()[-3:] == tinted()
    hook.instruction(instruction(10, tint=False), 1004)         # still two held: no wait
    assert wayland.health()[-3:] == cleared()


def test_a_resized_output_repaints_its_tint_on_page(layer):
    hook, overlay, wayland, link, now, painted, configure = layer
    configure(wayland.manager, "Virtual-1", 640, 480, 1, "", 0)
    hook.instruction(instruction(7), 1001)
    release(wayland)
    configure(wayland.manager, "Virtual-1", 800, 600, 2, "", 0)
    assert wayland.health()[-3:] == tinted(800, 600)


def test_the_stale_page_is_drawn_by_the_loop_after_v(layer):
    hook, overlay, wayland, link, now, painted, configure = layer
    configure(wayland.manager, "Virtual-1", 640, 480, 1, "", 0)
    assert hook.service(1000 + V - 1) == 1000 + V
    assert len(wayland.health()) == 3
    hook.service(1000 + V)
    assert wayland.health()[-3:] == tinted()
    assert painted[-1][2].text == UNAVAILABLE_LINES[0]
    wayland.feedbacks[-1].dispatcher["presented"](wayland.feedbacks[-1], 0, 0, 0, 0, 0, 0, 0)
    assert link.sent == []


def test_the_production_client_binds_the_manager_v3():
    assert client.MANAGER_VERSION == 3


# -- the judge link -----------------------------------------------------------------------------

class _Selector:
    def __init__(self):
        self.registered = {}

    def register(self, connection, events, data):
        self.registered[connection] = data

    def unregister(self, connection):
        del self.registered[connection]


def link_pair(peer_uid=10006):
    """A JudgeLink over `packet_pair` (production's SOCK_SEQPACKET on Linux, so a judge's close is
    a real EOF there; DGRAM on macOS, boundaries only): (link, judge ends, instructions taken,
    closes, selector)."""
    taken, closes, ends = [], [], []

    def connector(path):
        ours, theirs = packet_pair()
        ends.append(theirs)
        return ours
    link = client.JudgeLink("/run/x.sock", on_instruction=lambda i, now: taken.append(i),
                            on_closed=lambda: closes.append(True), clock=lambda: 5000,
                            connector=connector, peer=lambda connection: peer_uid)
    selector = _Selector()
    link.attach(selector)
    return link, ends, taken, closes, selector


def test_the_link_opens_with_one_overlay_request_and_takes_instructions():
    link, ends, taken, closes, selector = link_pair()
    assert link.service(0) is None
    assert ends[0].recv(64) == b'{"op":"overlay"}'
    ends[0].send(encode_overlay_instruction(instruction(1, tint=False)))
    selector.registered[link.connection]()
    assert taken == [instruction(1, tint=False)]
    assert link.send(PresentedReport("Virtual-1", 1))
    assert parse_presented_report(ends[0].recv(64)) == PresentedReport("Virtual-1", 1)


def test_a_peer_outside_root_and_pw_health_is_refused_and_retried_every_second():
    link, ends, taken, closes, selector = link_pair(peer_uid=10005)
    assert link.service(0) == client.RECONNECT_MS
    assert link.connection is None and not selector.registered
    assert link.service(client.RECONNECT_MS - 1) == client.RECONNECT_MS
    assert len(ends) == 1
    link.service(client.RECONNECT_MS)
    assert len(ends) == 2


def closed_reason(caplog):
    closed = [r.getMessage() for r in caplog.records if r.getMessage().startswith("judge link closed")]
    return closed[-1] if closed else None


def test_an_unparseable_packet_or_eof_closes_the_link_and_forgets_the_connection(caplog):
    link, ends, taken, closes, selector = link_pair()
    link.service(0)
    ends[0].send(b'{"output":"Virtual-1"}')
    selector.registered[link.connection]()
    assert link.connection is None and closes == [True] and not selector.registered
    assert closed_reason(caplog) == "judge link closed (not an overlay instruction)"
    assert link.next_attempt_ms == 5000 + client.RECONNECT_MS
    link.service(5000 + client.RECONNECT_MS)
    assert ends[1].recv(64) == b'{"op":"overlay"}'  # drained: the judge's close is a clean EOF
    ends[1].close()
    selector.registered[link.connection]()
    assert link.connection is None and closes == [True, True] and not selector.registered
    if LINUX:  # SEQPACKET EOF; a macOS DGRAM peer's close reads as a reset instead
        assert closed_reason(caplog) == "judge link closed (closed by the judge)"


def test_a_judge_that_closes_with_the_request_unread_resets_and_closes_the_link(caplog):
    link, ends, taken, closes, selector = link_pair()
    link.service(0)
    ends[0].close()                                 # {"op":"overlay"} never read: ECONNRESET
    selector.registered[link.connection]()
    assert link.connection is None and closes == [True] and not selector.registered
    reason = closed_reason(caplog)
    assert reason.startswith("judge link closed (receive: ") and "reset" in reason.lower()


@pytest.mark.skipif(not LINUX, reason="AF_UNIX SOCK_SEQPACKET is Linux-only")
def test_the_packet_pair_is_the_socket_type_the_link_connects_with():
    with (tempfile.TemporaryDirectory(dir="/tmp") as directory,  # AF_UNIX paths are short
          socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener):
        path = directory + "/health.sock"
        listener.bind(path)
        listener.listen(1)
        connection = client.connect_seqpacket(path)
        ours, theirs = packet_pair()
        with connection, ours, theirs:
            assert connection.type == ours.type == PACKET_TYPE == socket.SOCK_SEQPACKET


def test_a_failed_send_closes_the_link():
    link, ends, taken, closes, selector = link_pair()
    link.service(0)
    ends[0].close()
    assert not link.send(PresentedReport("Virtual-1", 1))
    assert closes == [True]
    assert not link.send(PresentedReport("Virtual-1", 1))      # closed: nothing to send on
