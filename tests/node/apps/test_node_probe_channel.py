"""The broker's probe thread (own selector and timer). Its listener: test_node_app_link_local.py."""
import socket
import threading
import time
from uuid import uuid4

import pytest
from support.packet_pair import packet_pair

from appliance.apps.probe import AppRunKey, ProbeTiming
from appliance.apps.probe_channel import ProbeThread
from appliance.feed import Feed
from contracts.node_app_link import (
    FrameBlack,
    FrameBlackCause,
    encode_node_app_link_result,
    encode_node_frames_black,
    encode_node_probe_answer,
    encode_node_renderer,
    parse_node_probe_channel_message,
)

# Small timing so the thread's real timer is exercised in well under a second per check.
FAST = ProbeTiming(period_ms=50, miss_limit=2, startup_ms=150)
# Passes until the run is long unanswered (the first is at 0): well past the misses' report.
DUE = 9
RUN = AppRunKey(uuid4(), 4242, 31751781, 1)
OTHER = AppRunKey(uuid4(), 4343, 31751999, 2)


def monotonic_ms():
    """The node uses CLOCK_BOOTTIME (Linux only); any monotonic ms clock drives the thread."""
    return time.monotonic_ns() // 1_000_000


def facts(feed, kind=None, run=RUN):
    taken, after = [], 0
    while True:
        page = feed.read(after, incarnation=None, limit=8)
        if not page.events:
            break
        taken += page.events
        after = page.events[-1].sequence
    return [e for e in taken if (kind is None or e.kind == kind)
            and (run is None or e.value.get("run") == run.document())]


def passes(probes, count, now=0):
    """`count` passes of an unstarted thread, a period apart from `now`; the next `now`."""
    for _ in range(count):
        probes.step(now)
        now += FAST.period_ms
    return now


def until(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class Player:
    """A fake Player on the far end: answers each probe with `answer(nonce)`."""

    def __init__(self, end, answer=lambda nonce: nonce):
        self.end, self.answer, self.received = end, answer, []
        self.end.settimeout(0.2)
        self.stopped = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopped:
            try:
                raw = self.end.recv(9000)
            except TimeoutError:
                continue
            except OSError:
                return
            if not raw:
                self.received.append("eof")
                return
            message = parse_node_probe_channel_message(raw)
            self.received.append(message)
            nonce = getattr(message, "nonce", None)
            if nonce is not None:
                self.end.send(encode_node_probe_answer(self.answer(nonce)))

    def stop(self):
        self.stopped = True
        self.thread.join(2)
        self.end.close()


@pytest.fixture
def probes():
    feed = Feed(512)
    thread = ProbeThread(feed, clock=monotonic_ms, timing=FAST)
    thread.start()
    yield thread
    thread.close()


def test_the_players_renderer_is_a_fact_and_keeps_the_channel_open(stepped):
    ours, theirs = packet_pair()
    theirs.settimeout(2)
    try:
        stepped.publish_run(RUN)
        stepped.adopt(ours, RUN)
        now = passes(stepped, 2)
        theirs.send(encode_node_probe_answer(parse_node_probe_channel_message(theirs.recv(9000)).nonce))
        theirs.send(encode_node_renderer("V3D 7.1.10.2"))
        now = passes(stepped, 2, now)  # one packet read per pass
        assert [e.value["renderer"] for e in facts(stepped.feed, "app_renderer")] == ["V3D 7.1.10.2"]
        assert len(facts(stepped.feed, "probe_answered")) == 1
        for _ in range(2):  # the channel still probes
            parse_node_probe_channel_message(theirs.recv(9000))
        assert [e.value["state"] for e in facts(stepped.feed, "probe_channel")] == ["open"]
    finally:
        theirs.close()


def test_healthy_channel_is_answered_every_period(stepped):
    ours, theirs = packet_pair()
    theirs.settimeout(2)
    try:
        stepped.publish_run(RUN)
        stepped.adopt(ours, RUN)
        now = passes(stepped, 2)  # the channel opens, then the first probe goes out
        for _ in range(2 * DUE):  # the Player answers each probe within the period
            theirs.send(encode_node_probe_answer(parse_node_probe_channel_message(
                theirs.recv(9000)).nonce))
            now = passes(stepped, 1, now)
        answered = facts(stepped.feed, "probe_answered")
        assert [e.value["rtt_ms"] for e in answered] == [FAST.period_ms] * 2 * DUE
        assert facts(stepped.feed, "probe_unanswered") == []
        assert [e.value["state"] for e in facts(stepped.feed, "probe_channel")] == ["open"]
        assert all(e.audience == "node" for e in facts(stepped.feed, run=None))
    finally:
        theirs.close()


def test_stale_nonces_are_never_answers(probes):
    ours, theirs = packet_pair()
    player = Player(theirs, answer=lambda nonce: "f" * 64)  # a well-formed answer to no probe
    try:
        probes.publish_run(RUN)
        probes.adopt(ours, RUN)
        assert until(lambda: facts(probes.feed, "probe_unanswered"))
        assert until(lambda: len([m for m in player.received if hasattr(m, "nonce")]) >= 3)
        assert facts(probes.feed, "probe_answered") == []
    finally:
        player.stop()


def test_a_blocked_main_loop_does_not_stop_probe_timing(probes):
    """The main loop publishes once and then blocks (systemctl, HTTP): misses still count on
    the thread's own timer, and the unanswered time keeps growing; nothing is handed back."""
    probes.publish_run(RUN)  # then nothing: no publish, no adopt
    assert until(lambda: any(e.value["unanswered_ms"] >= 8 * FAST.period_ms
                             for e in facts(probes.feed, "probe_unanswered")))
    unanswered = facts(probes.feed, "probe_unanswered")
    assert unanswered[-1].value["misses"] >= FAST.miss_limit
    assert {e.kind for e in facts(probes.feed, run=None)} <= {"probe_unanswered"}


@pytest.fixture
def stepped():
    """A real probe thread, not started: the test runs its passes on its own clock."""
    probes = ProbeThread(Feed(512), timing=FAST)
    yield probes
    probes.close()


def test_a_late_thread_turn_is_not_counted():
    offset = [0]
    feed = Feed(512)
    timing = ProbeTiming(period_ms=50, miss_limit=2, startup_ms=1)
    probes = ProbeThread(feed, clock=lambda: monotonic_ms() + offset[0], timing=timing)
    probes.start()
    try:
        probes.publish_run(RUN)
        assert until(lambda: facts(feed, "probe_unanswered"))
        offset[0] = 60_000  # the thread's clock jumps a minute: its own stall, not the app's
        time.sleep(0.4)
        assert facts(feed, "probe_unanswered")[-1].value["unanswered_ms"] < 5000
    finally:
        probes.close()


def test_channel_adopted_before_its_run_is_published_is_kept(probes):
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        probes.adopt(ours, RUN)  # serve_one runs before this turn's publish
        probes.publish_run(RUN)
        assert until(lambda: facts(probes.feed, "probe_answered"))
        assert [e.value["state"] for e in facts(probes.feed, "probe_channel")] == ["open"]
    finally:
        player.stop()


def test_a_channel_whose_run_is_not_published_is_closed(probes):
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        probes.publish_run(RUN)
        probes.adopt(ours, RUN)
        assert until(lambda: facts(probes.feed, "probe_answered"))
        probes.publish_run(OTHER)
        assert until(lambda: [e.value["state"] for e in facts(probes.feed, "probe_channel")]
                     == ["open", "closed"])
        # The new run has a fresh clock and no channel: it is probed by nobody, answered never.
        assert until(lambda: facts(probes.feed, "probe_unanswered", run=OTHER))
        assert facts(probes.feed, "probe_answered", run=OTHER) == []
    finally:
        player.stop()


def test_any_packet_but_an_answer_ends_the_channel(probes):
    ours, theirs = packet_pair()
    probes.publish_run(RUN)
    probes.adopt(ours, RUN)
    assert until(lambda: facts(probes.feed, "probe_channel"))
    theirs.send(encode_node_app_link_result("recorded"))
    assert until(lambda: [e.value["state"] for e in facts(probes.feed, "probe_channel")]
                 == ["open", "closed"])
    theirs.close()


def test_peer_eof_closes_the_channel(probes):
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    probes.publish_run(RUN)
    probes.adopt(ours, RUN)
    assert until(lambda: facts(probes.feed, "probe_channel"))
    theirs.close()
    assert until(lambda: [e.value["state"] for e in facts(probes.feed, "probe_channel")]
                 == ["open", "closed"])


def test_a_stopped_thread_is_loud():
    probes = ProbeThread(Feed(8), clock=monotonic_ms, timing=FAST)
    probes.start()
    probes.check()
    probes.close()
    with pytest.raises(RuntimeError, match="probe_thread_stopped"):
        probes.check()


def test_the_players_frames_black_report_is_a_fact_for_the_channels_run_and_keeps_the_channel_open(stepped):
    ours, theirs = packet_pair()
    try:
        stepped.publish_run(RUN)
        stepped.adopt(ours, RUN)
        stepped.step(0)
        theirs.send(encode_node_frames_black((FrameBlack("HDMI-A-1", FrameBlackCause.NO_PLAN),)))
        stepped.step(1)
        assert [e.value["frames"] for e in facts(stepped.feed, "app_frames_black")] == [
            [{"output": "HDMI-A-1", "cause": "no_plan"}]]
        assert [e.value["run"] for e in facts(stepped.feed, "app_frames_black")] == [RUN.document()]
        assert stepped._channel is not None
    finally:
        theirs.close()


def test_a_malformed_frames_black_report_is_dropped_and_the_channel_stays_open(stepped):
    ours, theirs = packet_pair()
    try:
        stepped.publish_run(RUN)
        stepped.adopt(ours, RUN)
        stepped.step(0)
        theirs.send(b'{"schema": 2, "kind": "frames_black", "frames": [{"output": "a", "cause": "new"}]}')
        stepped.step(1)
        assert facts(stepped.feed, "app_frames_black") == []
        assert stepped._channel is not None
    finally:
        theirs.close()


def test_an_unknown_packet_still_ends_the_channel(stepped):
    ours, theirs = packet_pair()
    try:
        stepped.publish_run(RUN)
        stepped.adopt(ours, RUN)
        stepped.step(0)
        theirs.send(b'{"schema": 2, "kind": "something_else"}')
        stepped.step(1)
        assert stepped._channel is None
    finally:
        theirs.close()
