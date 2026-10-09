"""Progress probes are answered from the Player's GLib control queue, never a helper thread."""

import asyncio
import json
import socket
import threading
from queue import Empty, Queue

import pytest
from test_player_service import close, rig

from contracts.node_app_link import (
    NodeProbeV2,
    NodeRendererV2,
    encode_node_probe,
    encode_node_relink,
    parse_node_probe_answer,
    parse_node_probe_open,
    parse_node_probe_reply,
)
from contracts.player_control import ControlAppliedReceipt
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

    def settimeout(self, _timeout):
        pass

    def send(self, raw, flags=0):
        if self.closed:
            raise OSError("closed")
        if self.full:
            raise BlockingIOError("EAGAIN")
        self.flags.append(flags)
        self.peer.incoming.put(bytes(raw))
        return len(raw)

    def recv(self, size):
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
    def __init__(self, dispatcher=None):
        self.glib = FakeGLib()
        self.dispatcher = dispatcher or MainLoopDispatcher(self.glib)
        self.relinks = 0
        self.player, self.broker = channel_pair()
        self.responder = ProbeResponder(self.dispatcher, on_relink=self.relink,
                                        connector=lambda _path: self.player)

    def relink(self):
        self.relinks += 1

    def open(self):
        self.thread = threading.Thread(target=self.responder._channel, daemon=True)
        self.thread.start()
        parse_node_probe_open(self.broker.recv(8193))    # the Player speaks first

    def probe(self, nonce):
        self.broker.send(encode_node_probe(nonce))

    def settle(self):
        """Wait until the responder thread handled every packet sent so far.

        Packets are handled in order, so a trailing `relink` marks the point."""
        expected = self.relinks + 1
        self.broker.send(encode_node_relink())
        for _ in range(2500):
            if self.relinks >= expected:
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


def test_relink_calls_back_and_unknown_packets_end_the_channel():
    rig_ = Rig()
    rig_.open()
    rig_.settle()
    assert rig_.relinks == 1 and rig_.glib.callbacks == []
    rig_.broker.send(json.dumps({"schema": 2, "kind": "probe", "nonce": "x"}).encode())
    rig_.thread.join(timeout=3)
    assert not rig_.thread.is_alive() and rig_.player.closed


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

    responder = ProbeResponder(MainLoopDispatcher(FakeGLib()), on_relink=lambda: None,
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

    responder = ProbeResponder(MainLoopDispatcher(FakeGLib()), on_relink=lambda: None,
                               connector=connector, sleep=sleep)
    with pytest.raises(Stop):
        responder._run()
    assert delays == [0.5, 1.0, 0.5, 0.5]


def test_start_runs_one_daemon_thread_named_player_probe():
    started = threading.Event()

    def connector(_path):
        started.set()
        raise OSError("socket_unavailable")

    responder = ProbeResponder(MainLoopDispatcher(FakeGLib()), on_relink=lambda: None,
                               connector=connector, sleep=lambda _s: threading.Event().wait(60))
    responder.start()
    assert started.wait(2)
    assert responder._thread.name == "player-probe" and responder._thread.daemon
    with pytest.raises(RuntimeError):
        responder.start()


def test_relink_request_clears_the_recorded_link_on_the_next_turn(tmp_path, monkeypatch):
    receipt_base = ControlAppliedReceipt(authority_epoch=7, delivery_id="e" * 32,
                                         delivery_sequence=5, state_digest="f" * 64,
                                         ack_nonce="b" * 64)

    class Proof:
        def exchange(self, **_kwargs):
            return "recorded"

        def exchange_applied(self, **_kwargs):
            return "recorded"

    class Link:
        def __init__(self):
            self.calls = 0

        def exchange_applied(self, **_kwargs):
            self.calls += 1
            return "recorded"

    async def run():
        service, server = await rig(tmp_path)
        link = Link()
        service.app_proof_client, service.node_link_client = Proof(), link
        service.boot_id = BOOT_ID
        service._proof_active = True
        registration = service.registration
        receipt = receipt_base.model_copy(update={"authority_epoch": registration.authority_epoch})
        service._highest_control_sequence = 5
        service._highest_control_delivery_id = "e" * 32
        service._applied_proof_receipt = (service._session, receipt)
        monkeypatch.setattr("player.service.LOCAL_PROOF_RETRY", .01)
        task = asyncio.create_task(service._local_app_proof_loop())
        try:
            await asyncio.sleep(.08)
            assert link.calls == 1          # recorded once, then held
            await asyncio.to_thread(service.request_relink)    # from another thread
            await asyncio.sleep(.08)
            assert link.calls == 2
        finally:
            service._proof_active = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close(service)

    asyncio.run(run())


def test_real_seqpacket_channel_round_trip():
    try:
        player, broker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        pytest.skip("SOCK_SEQPACKET is Linux-only")
    glib = FakeGLib()
    responder = ProbeResponder(MainLoopDispatcher(glib), on_relink=lambda: None,
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
