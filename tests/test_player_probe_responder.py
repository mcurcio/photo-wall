"""Progress probes are answered from the Player's GLib control queue, never a helper thread."""

import json
import logging
import socket
import threading
from queue import Empty, Queue

import pytest

from contracts.node_app_link import (
    MAX_FRAMES_BLACK,
    FrameBlack,
    FrameBlackCause,
    NodeFramesBlackV2,
    NodeProbeV2,
    NodeRendererV2,
    encode_node_probe,
    parse_node_probe_answer,
    parse_node_probe_open,
    parse_node_probe_reply,
)
from player.mainloop import CONTROL, DispatchRefused, MainLoopDispatcher
from player.probe_responder import RETRY_DELAYS, ProbeResponder

NONCES = [format(index, "x") * 64 for index in range(1, 6)]
BOOT_ID = "12345678-1234-1234-1234-123456789abc"


class FakeGLib:
    """An idle queue the test runs by hand; never running it is a starved control queue."""

    def __init__(self):
        self.callbacks = []

    def idle_add(self, callback, *, priority):
        # Control and its probe lane share one priority, above the tick and GDK paint.
        assert priority == CONTROL
        self.callbacks.append(callback)

    def run_idle(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


class Channel:
    """Portable SOCK_SEQPACKET end (macOS has none); the broker side is `broker`."""

    def __init__(self):
        self.incoming = Queue()
        self.peer = None
        self.closed = False
        self.flags = []
        self.full = False
        self.delivered = 0      # packets the peer put on this end
        self.receives = 0       # recv calls on this end

    def settimeout(self, _timeout):
        pass

    def send(self, raw, flags=0):
        if self.closed:
            raise OSError("closed")
        if self.full:
            raise BlockingIOError("EAGAIN")
        self.flags.append(flags)
        self.peer.delivered += 1
        self.peer.incoming.put(bytes(raw))
        return len(raw)

    def recv(self, size):
        self.receives += 1
        raw = self.incoming.get(timeout=5)
        return raw[:size]

    def close(self):
        if not self.closed:
            self.closed = True
            self.peer.incoming.put(b"")


def channel_pair():
    player, broker = Channel(), Channel()
    player.peer, broker.peer = broker, player
    return player, broker


class Rig:
    def __init__(self, dispatcher=None, frames_black=None):
        self.glib = FakeGLib()
        self.dispatcher = dispatcher or MainLoopDispatcher(self.glib)
        self.player, self.broker = channel_pair()
        self.report = None      # no executor unless a test says otherwise: nothing is sent
        self.responder = ProbeResponder(self.dispatcher,
                                        frames_black=frames_black or (lambda: self.report),
                                        connector=lambda _path: self.player)

    def open(self):
        self.thread = threading.Thread(target=self.responder._channel, daemon=True)
        self.thread.start()
        parse_node_probe_open(self.broker.recv(8193))    # the Player speaks first

    def probe(self, nonce):
        self.broker.send(encode_node_probe(nonce))

    def settle(self):
        """Wait until the responder thread handled every packet sent so far: it is back in
        `recv` once for each packet it took, plus the one it waits in."""
        for _ in range(2500):
            if self.player.receives > self.player.delivered and self.player.incoming.empty():
                return
            threading.Event().wait(.002)
        raise AssertionError("responder did not settle")

    def answers(self):
        found = []
        while True:
            try:
                raw = self.broker.incoming.get(timeout=.05)
            except Empty:
                return found
            if raw:
                found.append(parse_node_probe_answer(raw))

    def shut(self):
        self.broker.close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()


def test_starved_control_queue_answers_nothing_and_queues_exactly_one_callback():
    rig_ = Rig()
    rig_.open()
    for nonce in NONCES:
        rig_.probe(nonce)
    rig_.settle()
    assert len(rig_.glib.callbacks) == 1
    assert rig_.answers() == []
    rig_.shut()


def test_unstarved_queue_answers_only_the_latest_nonce_once_from_the_glib_queue():
    rig_ = Rig()
    rig_.open()
    for nonce in NONCES[:3]:
        rig_.probe(nonce)
    rig_.settle()
    assert len(rig_.glib.callbacks) == 1
    rig_.glib.run_idle()
    assert rig_.answers() == [NONCES[2]]
    assert rig_.player.flags == [0, socket.MSG_DONTWAIT]    # probe_open, then the answer
    rig_.glib.run_idle()
    assert rig_.answers() == []
    rig_.probe(NONCES[3])          # the slot is free again after the answer
    rig_.settle()
    assert len(rig_.glib.callbacks) == 1
    rig_.glib.run_idle()
    assert rig_.answers() == [NONCES[3]]
    rig_.shut()


def test_the_gl_renderer_follows_the_first_answer_once_per_channel():
    """The renderer is reported on the main thread, after an answer, once it is known."""
    known = {"name": None}
    rig_ = Rig()
    rig_.responder.gl_renderer = lambda: known["name"]

    def packets():
        found = []
        while True:
            try:
                raw = rig_.broker.incoming.get(timeout=.05)
            except Empty:
                return found
            if raw:
                found.append(parse_node_probe_reply(raw))

    rig_.open()
    rig_.probe(NONCES[0])
    rig_.settle()
    rig_.glib.run_idle()
    assert packets() == [NodeProbeV2(NONCES[0])]  # no GL context yet: nothing to report
    known["name"] = "V3D 7.1.10.2"
    for nonce in NONCES[1:3]:
        rig_.probe(nonce)
        rig_.settle()
        rig_.glib.run_idle()
    assert packets() == [NodeProbeV2(NONCES[1]), NodeRendererV2("V3D 7.1.10.2"), NodeProbeV2(NONCES[2])]
    rig_.shut()
    rig_.player, rig_.broker = channel_pair()  # a new channel (a restarted broker) hears it again
    rig_.responder.connector = lambda _path: rig_.player
    rig_.open()
    rig_.probe(NONCES[3])
    rig_.settle()
    rig_.glib.run_idle()
    assert packets() == [NodeProbeV2(NONCES[3]), NodeRendererV2("V3D 7.1.10.2")]
    rig_.shut()


def test_queued_probe_never_takes_a_control_slot():
    """A probe queued behind a held idle queue leaves all four control slots to control work."""
    rig_ = Rig()
    rig_.open()
    rig_.probe(NONCES[0])
    rig_.settle()
    assert len(rig_.glib.callbacks) == 1
    # Control, time, websocket and observation loops each dispatch while the queue is held.
    control = [rig_.dispatcher(lambda: None) for _ in range(4)]
    assert not any(future.done() for future in control)       # all queued, none refused
    assert len(rig_.glib.callbacks) == 5
    assert isinstance(rig_.dispatcher(lambda: None).exception(timeout=0), DispatchRefused)  # still four
    rig_.glib.run_idle()           # same queue: the probe and the control work drain together
    assert all(future.done() and future.exception() is None for future in control)
    assert rig_.answers() == [NONCES[0]]
    rig_.shut()


def test_full_control_queue_still_queues_the_probe_on_the_same_glib_queue():
    rig_ = Rig()
    control = [rig_.dispatcher(lambda: None) for _ in range(4)]   # control work fills its slots
    assert isinstance(rig_.dispatcher(lambda: None).exception(timeout=0), DispatchRefused)
    rig_.open()
    rig_.probe(NONCES[0])
    rig_.settle()
    assert len(rig_.glib.callbacks) == 5 and rig_.answers() == []
    rig_.glib.run_idle()
    assert all(future.done() for future in control)
    assert rig_.answers() == [NONCES[0]]
    rig_.shut()


def test_busy_probe_lane_refusal_answers_nothing_and_frees_the_gate():
    rig_ = Rig()
    held = rig_.responder.dispatcher(lambda: None)              # the probe lane's one slot
    assert isinstance(rig_.responder.dispatcher(lambda: None).exception(timeout=0), DispatchRefused)
    rig_.open()
    rig_.probe(NONCES[0])
    rig_.settle()
    assert len(rig_.glib.callbacks) == 1 and rig_.answers() == []
    rig_.glib.run_idle()           # the lane drains; the refused probe stays unanswered
    assert held.done() and rig_.answers() == []
    rig_.probe(NONCES[1])
    rig_.settle()
    rig_.glib.run_idle()
    assert rig_.answers() == [NONCES[1]]
    rig_.shut()


def test_full_socket_drops_the_answer_without_blocking_glib():
    rig_ = Rig()
    rig_.open()
    rig_.probe(NONCES[0])
    rig_.settle()
    rig_.player.full = True
    rig_.glib.run_idle()
    rig_.player.full = False
    assert rig_.answers() == []
    rig_.probe(NONCES[1])
    rig_.settle()
    rig_.glib.run_idle()
    assert rig_.answers() == [NONCES[1]]
    rig_.shut()


def test_callback_queued_across_a_closed_channel_sends_nothing_and_stays_single():
    rig_ = Rig()
    rig_.open()
    rig_.probe(NONCES[0])
    rig_.settle()
    rig_.shut()                    # channel ends with the callback still queued
    rig_.player, rig_.broker = channel_pair()
    rig_.open()
    rig_.probe(NONCES[1])
    rig_.settle()
    assert len(rig_.glib.callbacks) == 1     # the old callback still holds the one slot
    rig_.glib.run_idle()
    assert rig_.answers() == [NONCES[1]]
    rig_.shut()


@pytest.mark.parametrize("packet", [
    {"schema": 2, "kind": "relink"},                    # retired with the app link (0034, S4c)
    {"schema": 2, "kind": "probe", "nonce": "x"},
])
def test_anything_but_a_probe_ends_the_channel(packet):
    rig_ = Rig()
    rig_.open()
    rig_.broker.send(json.dumps(packet).encode())
    rig_.thread.join(timeout=3)
    assert not rig_.thread.is_alive() and rig_.player.closed and rig_.glib.callbacks == []


def test_the_player_opens_its_probe_channel_with_no_link():
    """The Player's first and only opening packet is `probe_open`: no app link's `begin` before it
    (decision 0034, S4c). Mutation probe: send a `begin` packet before `probe_open` -> the
    broker's listener refuses the channel (tests/test_node_app_link_local.py) and this fails."""
    rig_ = Rig()
    rig_.open()            # the first packet parses as `probe_open` (Rig.open)
    rig_.probe(NONCES[0])
    rig_.settle()
    rig_.glib.run_idle()
    assert rig_.answers() == [NONCES[0]]
    assert not hasattr(rig_.responder, "on_relink")
    rig_.shut()


def test_todays_broker_refusal_retries_with_slow_capped_backoff():
    """Today's broker answers `probe_open` like any non-begin packet: `refused`, then close."""
    opened, delays = [], []

    class Stop(Exception):
        pass

    def connector(_path):
        player, broker = channel_pair()

        def refuse():
            opened.append(broker.recv(8193))
            broker.send(b'{"schema":2,"kind":"result","status":"refused"}')
            broker.close()

        threading.Thread(target=refuse, daemon=True).start()
        return player

    def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 8:
            raise Stop

    responder = ProbeResponder(MainLoopDispatcher(FakeGLib()), frames_black=lambda: None,
                               connector=connector, sleep=sleep)
    with pytest.raises(Stop):
        responder._run()
    assert delays == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0]
    assert max(RETRY_DELAYS) == 5.0
    assert len(opened) == 8
    for raw in opened:
        parse_node_probe_open(raw)


def test_unavailable_socket_retries_and_a_probed_channel_resets_the_backoff():
    delays, attempts = [], []

    class Stop(Exception):
        pass

    def connector(_path):
        attempts.append(1)
        if len(attempts) in (1, 2):
            raise OSError("socket_unavailable")
        player, broker = channel_pair()

        def serve():
            broker.recv(8193)
            broker.send(encode_node_probe(NONCES[0]))
            broker.close()

        threading.Thread(target=serve, daemon=True).start()
        return player

    def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 4:
            raise Stop

    responder = ProbeResponder(MainLoopDispatcher(FakeGLib()), frames_black=lambda: None,
                               connector=connector, sleep=sleep)
    with pytest.raises(Stop):
        responder._run()
    assert delays == [0.5, 1.0, 0.5, 0.5]


def test_start_runs_one_daemon_thread_named_player_probe():
    started = threading.Event()

    def connector(_path):
        started.set()
        raise OSError("socket_unavailable")

    responder = ProbeResponder(MainLoopDispatcher(FakeGLib()), frames_black=lambda: None,
                               connector=connector, sleep=lambda _s: threading.Event().wait(60))
    responder.start()
    assert started.wait(2)
    assert responder._thread.name == "player-probe" and responder._thread.daemon
    with pytest.raises(RuntimeError):
        responder.start()


def test_real_seqpacket_channel_round_trip():
    try:
        player, broker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        pytest.skip("SOCK_SEQPACKET is Linux-only")
    glib = FakeGLib()
    responder = ProbeResponder(MainLoopDispatcher(glib), frames_black=lambda: None,
                               connector=lambda _path: player)
    thread = threading.Thread(target=responder._channel, daemon=True)
    thread.start()
    broker.settimeout(2)
    with broker:
        parse_node_probe_open(broker.recv(8193))
        broker.send(encode_node_probe(NONCES[0]))
        for _ in range(1000):
            if glib.callbacks:
                break
            threading.Event().wait(.002)
        glib.run_idle()
        assert parse_node_probe_answer(broker.recv(8193)) == NONCES[0]
    thread.join(timeout=3)
    assert not thread.is_alive()


def replies(rig):
    """Every packet the Player sent since the last call, parsed."""
    found = []
    while True:
        try:
            raw = rig.broker.incoming.get(timeout=.05)
        except Empty:
            return found
        if raw:
            found.append(parse_node_probe_reply(raw))


def reports_of(rig):
    return [reply.frames for reply in replies(rig) if isinstance(reply, NodeFramesBlackV2)]


def answered(rig):
    rig.probe(NONCES[0])
    rig.settle()
    rig.glib.run_idle()


NO_PLAN = (FrameBlack("HDMI-A-1", FrameBlackCause.NO_PLAN),)


def test_the_frames_black_report_goes_once_until_it_changes():
    """Sixty probes with no change send one packet; a change sends another. Mutation probe: send
    on every answer."""
    rig = Rig()
    rig.report = NO_PLAN
    rig.open()
    for _ in range(60):
        answered(rig)
    assert reports_of(rig) == [NO_PLAN]
    rig.report = ()
    answered(rig)
    assert reports_of(rig) == [()]                  # an empty report: nothing is black
    rig.shut()


def test_a_new_channel_restates_the_report():
    """Mutation probe: do not reset on channel open."""
    rig = Rig()
    rig.report = NO_PLAN
    rig.open()
    answered(rig)
    assert reports_of(rig) == [NO_PLAN]
    rig.shut()
    rig.player, rig.broker = channel_pair()
    rig.responder.connector = lambda _path: rig.player
    rig.open()
    answered(rig)
    assert reports_of(rig) == [NO_PLAN]
    rig.shut()


def test_no_executor_sends_nothing():
    """"Cannot tell" is not "nothing is black". Mutation probe: no executor reported as ()."""
    rig = Rig(frames_black=lambda: None)
    rig.open()
    answered(rig)
    assert reports_of(rig) == []
    rig.shut()


def test_no_executor_after_a_report_sends_nothing_and_the_report_stands():
    """Mutation probe: treat a later "cannot tell" as a report to send."""
    rig = Rig()
    rig.report = NO_PLAN
    rig.open()
    answered(rig)
    assert reports_of(rig) == [NO_PLAN]
    rig.report = None
    answered(rig)
    assert reports_of(rig) == []
    rig.report = NO_PLAN                            # unchanged since it last got through
    answered(rig)
    assert reports_of(rig) == []
    rig.shut()


def test_a_failed_send_is_retried_at_the_next_answer():
    """The answer goes (EAGAIN is the socket's, the report stays owed)."""
    rig = Rig()
    rig.report = NO_PLAN
    rig.open()
    sent = []
    send = rig.player.send

    def refuse_a_report(raw, flags=0):
        if "frames_black" in bytes(raw).decode():
            if not sent:
                sent.append(raw)
                raise BlockingIOError("EAGAIN")
        return send(raw, flags)

    rig.player.send = refuse_a_report
    answered(rig)
    assert reports_of(rig) == []
    answered(rig)
    assert reports_of(rig) == [NO_PLAN]
    rig.shut()


def test_a_report_that_cannot_be_encoded_is_skipped_and_the_answer_stands():
    too_long = tuple(FrameBlack(f"o{i:02}", FrameBlackCause.NO_PLAN) for i in range(MAX_FRAMES_BLACK + 1))
    rig = Rig()
    rig.report = too_long
    rig.open()
    rig.probe(NONCES[1])
    rig.settle()
    rig.glib.run_idle()
    assert replies(rig) == [NodeProbeV2(NONCES[1])]
    rig.shut()


def test_an_unreportable_renderer_does_not_stop_the_frames_black_report():
    """Mutation probe: report inside the renderer send's try -> an over-long name loses the card."""
    rig = Rig()
    rig.responder.gl_renderer = lambda: "x" * 500
    rig.report = NO_PLAN
    rig.open()
    answered(rig)
    assert reports_of(rig) == [NO_PLAN]
    rig.shut()


@pytest.mark.parametrize("error", [ValueError("a report that cannot be made"), KeyError("picture")])
def test_a_report_that_raises_is_logged_and_skipped(error, caplog):
    """Mutation probe: catch only ValueError -> a KeyError escapes unlogged, into the dispatcher."""
    def broken():
        raise error

    rig = Rig(frames_black=broken)
    rig.open()
    rig.probe(NONCES[2])
    rig.settle()
    with caplog.at_level(logging.WARNING, logger="photo_wall.player.probe"):
        rig.glib.run_idle()
    assert replies(rig) == [NodeProbeV2(NONCES[2])]
    assert "frames black report skipped" in caplog.text
    rig.shut()
