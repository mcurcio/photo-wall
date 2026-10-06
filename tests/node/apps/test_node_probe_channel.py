"""The broker's probe thread (own selector and timer) and the app-link socket's probe path."""
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from support.packet_pair import packet_pair
from test_node_boot import environment

from appliance.apps.broker import RunningApp
from appliance.apps.probe import AppRunKey, OwedRelink, ProbeTiming
from appliance.apps.probe_channel import ProbeThread
from appliance.feed import Feed
from appliance.node import app_link
from appliance.node.app_link import BrokerLinkService
from contracts.node_app_link import (
    encode_node_app_link_result,
    encode_node_probe_answer,
    encode_node_probe_open,
    parse_node_probe_channel_message,
)
from contracts.node_protocol import NodeProcessIdentity

# Small timing so the thread's real timer is exercised in well under a second per check.
FAST = ProbeTiming(period_ms=50, miss_limit=2, startup_ms=150, kill_after_ms=400)
RUN = AppRunKey(uuid4(), 4242, 31751781, 1)
OTHER = AppRunKey(uuid4(), 4343, 31751999, 2)
OWED = OwedRelink(RUN, "a" * 32)  # one owed episode for RUN (one Central refusal)


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


def test_healthy_channel_is_answered_every_period(probes):
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        probes.publish_run(RUN, False)
        probes.adopt(ours, RUN)
        started = time.monotonic()
        time.sleep(1.0)
        elapsed_ms = (time.monotonic() - started) * 1000
        answered = facts(probes.feed, "probe_answered")
        assert len(answered) >= elapsed_ms / FAST.period_ms / 2, len(answered)
        assert all(type(e.value["rtt_ms"]) is int and e.value["rtt_ms"] >= 0 for e in answered)
        assert facts(probes.feed, "probe_unanswered") == []
        assert facts(probes.feed, "probe_kill_due") == [] and probes.take_kill_due() is None
        assert [e.value["state"] for e in facts(probes.feed, "probe_channel")] == ["open"]
        assert all(e.audience == "node" for e in facts(probes.feed, run=None))
    finally:
        player.stop()


def test_stale_nonces_are_never_answers(probes):
    ours, theirs = packet_pair()
    player = Player(theirs, answer=lambda nonce: "f" * 64)  # a well-formed answer to no probe
    try:
        probes.publish_run(RUN, False)
        probes.adopt(ours, RUN)
        assert until(lambda: facts(probes.feed, "probe_kill_due"))
        assert facts(probes.feed, "probe_answered") == []
        assert len([m for m in player.received if hasattr(m, "nonce")]) >= 3
    finally:
        player.stop()


def test_a_blocked_main_loop_does_not_stop_probe_timing(probes):
    """The main loop publishes once and then blocks (systemctl, HTTP): misses still count."""
    probes.publish_run(RUN, False)
    time.sleep(1.0)  # the main loop is blocked: no publish, no adopt, no take
    unanswered = facts(probes.feed, "probe_unanswered")
    assert unanswered and unanswered[-1].value["misses"] >= FAST.miss_limit
    [due] = facts(probes.feed, "probe_kill_due")
    assert due.value["unanswered_ms"] >= FAST.kill_after_ms
    due = probes.take_kill_due()
    assert due.run == RUN and due.unanswered_ms >= FAST.kill_after_ms


def test_kill_due_is_a_level_while_unanswered_and_the_fact_an_edge(probes):
    """A kill the main loop took but withheld (an armed recovery) is offered again on later
    turns while the run stays unanswered (E-AP2-1); `probe_kill_due` is still once."""
    probes.publish_run(RUN, False)
    assert until(lambda: probes.take_kill_due() is not None)
    retaken = []
    assert until(lambda: retaken.append(probes.take_kill_due()) or retaken[-1] is not None)
    assert retaken[-1].run == RUN and retaken[-1].unanswered_ms >= FAST.kill_after_ms
    time.sleep(3 * FAST.period_ms / 1000)
    assert len(facts(probes.feed, "probe_kill_due")) == 1


def test_an_answer_ends_the_kill_due_level(probes):
    ours, theirs = packet_pair()
    probes.publish_run(RUN, False)
    assert until(lambda: probes.take_kill_due() is not None)
    player = Player(theirs)  # the app answers again
    try:
        probes.adopt(ours, RUN)
        assert until(lambda: facts(probes.feed, "probe_answered"))
        time.sleep(2 * FAST.period_ms / 1000)
        probes.take_kill_due()  # at most one re-assertion raced the answer
        time.sleep(3 * FAST.period_ms / 1000)
        assert probes.take_kill_due() is None
    finally:
        player.stop()


def test_an_answer_clears_a_kill_due_latch_set_before_it():
    """E-B8-6: a latch set while overdue must not survive the answer that ends the episode,
    or the main loop kills an app that recovered. Driven by hand: no thread, no race."""
    now = [0]
    probes = ProbeThread(Feed(512), clock=lambda: now[0], timing=FAST)
    ours, theirs = packet_pair()
    theirs.setblocking(False)
    try:
        probes.publish_run(RUN, False)
        probes.adopt(ours, RUN)
        probes._apply(now[0])
        nonces = []
        while probes._kill_due is None:
            now[0] += FAST.period_ms
            probes._turn(now[0], late=False)
            while True:
                try:
                    nonces.append(parse_node_probe_channel_message(theirs.recv(9000)).nonce)
                except BlockingIOError:
                    break
        assert probes._kill_due.unanswered_ms >= FAST.kill_after_ms and nonces
        theirs.send(encode_node_probe_answer(nonces[-1]))
        probes._receive(now[0] + 1)
        assert facts(probes.feed, "probe_answered")
        assert probes.take_kill_due() is None
    finally:
        probes.close()
        theirs.close()


def test_a_run_change_drops_the_kill_due_latch(probes):
    probes.publish_run(RUN, False)
    assert until(lambda: facts(probes.feed, "probe_kill_due"))
    probes.publish_run(OTHER, False)
    time.sleep(2 * FAST.period_ms / 1000)  # OTHER is inside its startup budget
    assert probes.take_kill_due() is None


def test_a_late_thread_turn_is_not_counted():
    offset = [0]
    feed = Feed(512)
    timing = ProbeTiming(period_ms=50, miss_limit=2, startup_ms=1, kill_after_ms=5000)
    probes = ProbeThread(feed, clock=lambda: monotonic_ms() + offset[0], timing=timing)
    probes.start()
    try:
        probes.publish_run(RUN, False)
        assert until(lambda: facts(feed, "probe_unanswered"))
        offset[0] = 60_000  # the thread's clock jumps a minute: its own stall, not the app's
        time.sleep(0.4)
        assert facts(feed, "probe_unanswered")[-1].value["unanswered_ms"] < timing.kill_after_ms
        assert facts(feed, "probe_kill_due") == [] and probes.take_kill_due() is None
    finally:
        probes.close()


def test_channel_adopted_before_its_run_is_published_is_kept(probes):
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        probes.adopt(ours, RUN)  # serve_one runs before this turn's publish
        probes.publish_run(RUN, False)
        assert until(lambda: facts(probes.feed, "probe_answered"))
        assert [e.value["state"] for e in facts(probes.feed, "probe_channel")] == ["open"]
    finally:
        player.stop()


def test_a_channel_whose_run_is_not_published_is_closed(probes):
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        probes.publish_run(RUN, False)
        probes.adopt(ours, RUN)
        assert until(lambda: facts(probes.feed, "probe_answered"))
        probes.publish_run(OTHER, False)
        assert until(lambda: [e.value["state"] for e in facts(probes.feed, "probe_channel")]
                     == ["open", "closed"])
        # The new run has a fresh clock and no channel: it is probed by nobody, answered never.
        assert until(lambda: facts(probes.feed, "probe_unanswered", run=OTHER))
        assert facts(probes.feed, "probe_answered", run=OTHER) == []
    finally:
        player.stop()


def test_any_packet_but_an_answer_ends_the_channel(probes):
    ours, theirs = packet_pair()
    probes.publish_run(RUN, False)
    probes.adopt(ours, RUN)
    assert until(lambda: facts(probes.feed, "probe_channel"))
    theirs.send(encode_node_app_link_result("recorded"))
    assert until(lambda: [e.value["state"] for e in facts(probes.feed, "probe_channel")]
                 == ["open", "closed"])
    theirs.close()


def test_peer_eof_closes_the_channel(probes):
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    probes.publish_run(RUN, False)
    probes.adopt(ours, RUN)
    assert until(lambda: facts(probes.feed, "probe_channel"))
    theirs.close()
    assert until(lambda: [e.value["state"] for e in facts(probes.feed, "probe_channel")]
                 == ["open", "closed"])


def relinks(player):
    return [m for m in player.received if type(m).__name__ == "NodeRelinkV2"]


def test_relink_reaches_only_the_owed_runs_channel(probes):
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        probes.publish_run(RUN, False)
        probes.adopt(ours, RUN)
        assert until(lambda: facts(probes.feed, "probe_answered"))
        probes.owe_relink(OwedRelink(OTHER, "a" * 32))
        time.sleep(0.15)
        assert relinks(player) == []
        probes.owe_relink(OWED)
        assert until(lambda: relinks(player))
        assert [e.value for e in facts(probes.feed, "relink_sent")] == [{"run": RUN.document()}]
    finally:
        player.stop()


def test_a_relink_owed_while_no_channel_is_open_goes_to_the_next_channel_first(probes):
    """E-B7-3: the channel is down (broker restart, Player backoff) when the relink is owed."""
    probes.publish_run(RUN, False)
    probes.owe_relink(OWED)
    time.sleep(0.15)  # several thread passes with nothing to send it on
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        probes.adopt(ours, RUN)
        assert until(lambda: relinks(player))
        assert type(player.received[0]).__name__ == "NodeRelinkV2"  # before the first probe
        assert len(facts(probes.feed, "relink_sent")) == 1
    finally:
        player.stop()


def test_a_relink_is_sent_once_per_channel_instance(probes):
    probes.publish_run(RUN, False)
    first_ours, first_theirs = packet_pair()
    first = Player(first_theirs)
    try:
        probes.adopt(first_ours, RUN)
        for _ in range(15):  # the main loop restates the owed relink every turn
            probes.owe_relink(OWED)
            time.sleep(0.02)
        assert until(lambda: facts(probes.feed, "probe_answered"))
        assert len(relinks(first)) == 1
    finally:
        first.stop()
    assert until(lambda: [e.value["state"] for e in facts(probes.feed, "probe_channel")]
                 == ["open", "closed"])
    second_ours, second_theirs = packet_pair()
    second = Player(second_theirs)
    try:
        probes.adopt(second_ours, RUN)  # the Player reconnected: a new channel instance
        assert until(lambda: relinks(second))
        for _ in range(10):
            probes.owe_relink(OWED)
            time.sleep(0.02)
        assert len(relinks(second)) == 1 and len(facts(probes.feed, "relink_sent")) == 2
    finally:
        second.stop()


def test_each_owed_episode_reaches_a_long_lived_channel_once(probes):
    """B7c: Central refuses the same run's link again after the Player relinked on this
    channel; the new episode is sent on the same channel, once, and counted once."""
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        probes.publish_run(RUN, False)
        probes.adopt(ours, RUN)
        probes.owe_relink(OWED)
        assert until(lambda: len(relinks(player)) == 1)
        probes.owe_relink(None)  # the Player proved again: the slot holds its new link
        for episode in ("b" * 32, "c" * 32):  # two later refusals of that link, each restated
            for _ in range(5):
                probes.owe_relink(OwedRelink(RUN, episode))
                time.sleep(0.02)
        assert until(lambda: len(relinks(player)) == 3)
        time.sleep(0.15)  # restated every turn: still once per episode
        assert len(relinks(player)) == 3
        assert [e.value for e in facts(probes.feed, "relink_sent")] == [{"run": RUN.document()}] * 3
        assert [e.value["state"] for e in facts(probes.feed, "probe_channel")] == ["open"]
    finally:
        player.stop()


def test_nothing_owed_sends_no_relink(probes):
    probes.publish_run(RUN, False)
    probes.owe_relink(OWED)
    probes.owe_relink(None)  # the Player proved again: the slot holds its new link
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        probes.adopt(ours, RUN)
        assert until(lambda: facts(probes.feed, "probe_answered"))
        time.sleep(0.1)
        assert relinks(player) == [] and facts(probes.feed, "relink_sent") == []
    finally:
        player.stop()


def test_a_run_change_drops_the_owed_relink(probes):
    probes.publish_run(RUN, False)
    probes.owe_relink(OWED)
    probes.publish_run(OTHER, False)
    ours, theirs = packet_pair()
    player = Player(theirs)
    try:
        assert until(lambda: facts(probes.feed, "probe_unanswered", run=OTHER))
        probes.publish_run(RUN, False)  # the old run's key again: still nothing owed
        probes.adopt(ours, RUN)
        assert until(lambda: facts(probes.feed, "probe_answered"))
        assert relinks(player) == []
    finally:
        player.stop()


class Unwritable:
    """A real channel end whose first `blocked` sends report EAGAIN (the Player not reading)."""

    def __init__(self, connection, blocked):
        self.connection, self.blocked, self.refused = connection, blocked, 0

    def fileno(self):
        return self.connection.fileno()

    def setblocking(self, flag):
        self.connection.setblocking(flag)

    def recv(self, size):
        return self.connection.recv(size)

    def send(self, packet, flags=0):
        if self.refused < self.blocked:
            self.refused += 1
            raise BlockingIOError()
        return self.connection.send(packet, flags)

    def close(self):
        self.connection.close()


def test_an_unsent_relink_is_retried_on_a_later_pass(probes):
    ours, theirs = packet_pair()
    channel = Unwritable(ours, blocked=3)
    player = Player(theirs)
    try:
        probes.publish_run(RUN, False)
        probes.owe_relink(OWED)
        probes.adopt(channel, RUN)
        assert until(lambda: relinks(player))
        assert channel.refused == 3 and len(relinks(player)) == 1
        assert len(facts(probes.feed, "relink_sent")) == 1
    finally:
        player.stop()


def test_a_stopped_thread_is_loud():
    probes = ProbeThread(Feed(8), clock=monotonic_ms, timing=FAST)
    probes.start()
    probes.check()
    probes.close()
    with pytest.raises(RuntimeError, match="probe_thread_stopped"):
        probes.check()


# -- the app-link socket's first packet selects the path (appliance/node/app_link.py) --------


class Connection:
    def __init__(self):
        self.sent, self.closed = [], False

    def setsockopt(self, *args):
        pass

    def settimeout(self, value):
        pass

    def send(self, packet):
        self.sent.append(packet)
        return len(packet)

    def close(self):
        self.closed = True


def service(monkeypatch, first, *, pid=396, grant=None, probes=True):
    running = RunningApp(environment("a"), NodeProcessIdentity(pid, 31751781, uuid4()), 1, uuid4())
    connection = Connection()
    monkeypatch.setattr(socket, "SO_PASSCRED", 16, raising=False)  # Linux constant; absent on macOS
    monkeypatch.setattr(app_link, "receive_credential_packet", lambda *a, **k: first)
    adopted = []
    links = BrokerLinkService.__new__(BrokerLinkService)
    links.driver = SimpleNamespace(current=lambda: running)
    # No Central session and no retained grant: the probe path must not need either.
    links.session = SimpleNamespace(grant=grant, store=SimpleNamespace(read=lambda name: None))
    links.probes = SimpleNamespace(adopt=lambda c, run: adopted.append((c, run))) if probes else None
    links.listener = SimpleNamespace(accept=lambda: (connection, None))
    return links, connection, adopted, running


ORIGINAL_HANDLE = BrokerLinkService.handle
REFUSED = b'{"schema":2,"kind":"result","status":"refused"}'


def test_probe_open_admitted_by_uid_and_running_pid_without_a_grant(monkeypatch):
    links, connection, adopted, running = service(
        monkeypatch, ((396, 10004, 10004), encode_node_probe_open()))
    links.serve_one()
    assert adopted == [(connection, AppRunKey.of(running))]
    assert connection.sent == [] and not connection.closed  # no ack; the thread owns it now


@pytest.mark.parametrize("credentials, probes", [
    ((396, 10003, 10003), True),   # not the app uid
    ((397, 10004, 10004), True),   # the app uid, but not the running app's pid
    ((396, 10004, 10004), False),  # a broker with no probe thread refuses as before
])
def test_probe_open_from_anyone_else_is_refused(monkeypatch, credentials, probes):
    links, connection, adopted, _ = service(monkeypatch, (credentials, encode_node_probe_open()),
                                            probes=probes)
    links.serve_one()
    assert adopted == [] and connection.sent == [REFUSED] and connection.closed


def test_probe_open_with_no_running_app_is_refused(monkeypatch):
    links, connection, adopted, _ = service(monkeypatch, ((396, 10004, 10004), encode_node_probe_open()))
    links.driver = SimpleNamespace(current=lambda: None)
    links.serve_one()
    assert adopted == [] and connection.sent == [REFUSED] and connection.closed


def test_other_first_packets_take_the_proof_path(monkeypatch):
    first = ((396, 10004, 10004), b'{"schema":2,"kind":"begin"}')
    links, connection, adopted, _ = service(monkeypatch, first)
    handled = []
    monkeypatch.setattr(BrokerLinkService, "handle",
                        lambda self, c, *, first=None: handled.append((c, first)))
    links.serve_one()
    assert handled == [(connection, first)] and adopted == [] and connection.closed
    garbage = ((396, 10004, 10004), b"not json")
    links, connection, adopted, _ = service(monkeypatch, garbage)
    monkeypatch.setattr(BrokerLinkService, "handle", ORIGINAL_HANDLE)
    links.serve_one()  # today's refusal: no grant, so `handle` refuses
    assert adopted == [] and connection.sent == [REFUSED] and connection.closed
