"""The broker accepts an app-link proof locally and holds it in an outbox until Central acks.

`BrokerLinkService.handle` answers `accepted` with no Central round trip (and keeps the
`local-app-control` write the recovery obligation reads); `deliver_app_link` delivers the
latest link per app run from the main loop, keeps it on every transient failure, and turns
it into an owed relink (`{"relink": run}`) when Central can never accept it.
"""
import http.client
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_node_boot import environment
from test_node_linux_adapters import store as boot_store

from appliance.feed import Feed
from appliance.node import app_link
from appliance.node.app_link import OUTBOX, BrokerLinkService, deliver_app_link, owed_relink
from appliance.node.broker import RunningApp
from appliance.node.broker_runner import BrokerLoop
from appliance.node.probe import AppRunKey
from contracts.node_app_link import (
    NodeAppLinkV2,
    encode_node_app_link,
    encode_node_app_link_begin,
    encode_node_app_link_result,
    parse_node_app_link,
    parse_node_app_link_challenge,
)
from contracts.node_commands import NodeSessionGrant
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2

PLAYER = "p-" + "a" * 32
BLACKHOLE_SECONDS = 0.5  # the broker session's HTTP timeout (broker_runner.main)


def running_app(pid=396):
    return RunningApp(environment("a"), NodeProcessIdentity(pid, 31751781, uuid4()), 1, uuid4())


class Session:
    """A broker session whose Central answers `statuses` in turn (an exception is raised)."""

    def __init__(self, store, grant, statuses=()):
        self.store, self.grant, self.statuses, self.requests = store, grant, list(statuses), []

    def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        answer = self.statuses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer, b"{}"


class Blackhole(Session):
    """Central accepts the connection and never answers until the transport times out."""

    def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        time.sleep(BLACKHOLE_SECONDS)
        raise TimeoutError()


class Relinks:
    """The broker feed `deliver_app_link` appends to (the ProbeThread's)."""

    def __init__(self):
        self.feed = Feed(64)


class Connection:
    def __init__(self):
        self.sent = []

    def send(self, packet):
        self.sent.append((time.monotonic(), packet))
        return len(packet)


@pytest.fixture
def node(tmp_path, monkeypatch):
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    grant = NodeSessionGrant(producer, uuid4(), uuid4(), 500, "app_effect")
    store = boot_store(tmp_path / "broker", producer.kernel_boot_id)
    monkeypatch.setattr(app_link, "boottime_ms", lambda: 1000)
    yield SimpleNamespace(producer=producer, grant=grant, store=store, tmp_path=tmp_path)
    if store.fd >= 0:
        store.close()


def prove(monkeypatch, node, running, session, *, feed=None, receipt='{"authority_epoch":1}'):
    """One signed exchange through `handle`; returns (connection, time the link arrived)."""
    links = BrokerLinkService.__new__(BrokerLinkService)
    links.driver = SimpleNamespace(current=lambda: running)
    links.session, links.feed = session, feed
    connection, arrived = Connection(), []
    peer = (running.process.pid, 10004, 10004)

    def receive(*args, **kwargs):
        if not connection.sent:
            return peer, encode_node_app_link_begin(player_id=PLAYER, authority_epoch=1,
                                                    control_receipt=receipt)
        challenge = parse_node_app_link_challenge(connection.sent[0][1])
        arrived.append(time.monotonic())
        return peer, encode_node_app_link(NodeAppLinkV2(challenge, "c" * 64, "d" * 128))

    monkeypatch.setattr(app_link, "receive_credential_packet", receive)
    links.handle(connection)
    return connection, arrived[0]


# -- accepted locally ----------------------------------------------------------------------


def test_accepted_within_100_ms_with_central_blackholed(monkeypatch, node):
    running, feed = running_app(), Feed(64)
    session = Blackhole(node.store, node.grant)
    connection, arrived = prove(monkeypatch, node, running, session, feed=feed)
    replied, packet = connection.sent[-1]
    assert packet == encode_node_app_link_result("accepted")
    assert replied - arrived < 0.1
    assert session.requests == []  # no Central exchange inside the proof
    # The recovery obligation's local evidence is still written (online_broker.py service).
    assert node.store.read("local-app-control")["operation_id"] == str(running.operation_id)
    run = AppRunKey.of(running).document()
    slot = node.store.read(OUTBOX)
    assert slot["run"] == run and slot["player_id"] == PLAYER
    assert parse_node_app_link(slot["link"].encode()).challenge.command_session_id == node.grant.session_id
    events = feed.read(0, incarnation=None).events
    assert [(e.kind, e.value, e.audience) for e in events] == [
        ("app_link_accepted", {"run": run, "player_id": PLAYER}, "node")]


def test_a_newer_proof_replaces_the_slot(monkeypatch, node):
    running = running_app()
    session = Session(node.store, node.grant)
    prove(monkeypatch, node, running, session)
    first = node.store.read(OUTBOX)["link"]
    prove(monkeypatch, node, running, session, receipt='{"authority_epoch":1,"delivery":2}')
    second = node.store.read(OUTBOX)["link"]
    assert second != first and '\\"delivery\\":2' in second


# -- delivered until Central acknowledges ---------------------------------------------------


def test_recorded_by_central_clears_the_slot(monkeypatch, node):
    running, relinks = running_app(), Relinks()
    session = Session(node.store, node.grant, [200])
    prove(monkeypatch, node, running, session)
    link = node.store.read(OUTBOX)["link"]
    deliver_app_link(node.store, session, relinks, current=AppRunKey.of(running))
    assert session.requests == [("POST", "/v2/node/app-links", link.encode())]
    assert not node.store.read(OUTBOX)
    assert [(e.kind, e.value) for e in relinks.feed.read(0, incarnation=None).events] == [
        ("app_link_recorded", {"run": AppRunKey.of(running).document()})]
    deliver_app_link(node.store, session, relinks, current=AppRunKey.of(running))
    assert len(session.requests) == 1 and owed_relink(node.store, AppRunKey.of(running)) is None


@pytest.mark.parametrize("failure", [
    503, 500, 408, 429, 401, 403, OSError("unreachable"), TimeoutError(), http.client.HTTPException()])
def test_the_slot_survives_transient_failures_and_is_redelivered(monkeypatch, node, failure):
    running, relinks = running_app(), Relinks()
    session = Session(node.store, node.grant, [failure, failure, 200])
    prove(monkeypatch, node, running, session)
    link = node.store.read(OUTBOX)["link"]
    current = AppRunKey.of(running)
    for _ in range(2):
        deliver_app_link(node.store, session, relinks, current=current)
        assert node.store.read(OUTBOX)["link"] == link
    deliver_app_link(node.store, session, relinks, current=current)
    assert [body for _, _, body in session.requests] == [link.encode()] * 3
    assert not node.store.read(OUTBOX)


def test_the_slot_survives_a_broker_restart(monkeypatch, node):
    running, relinks = running_app(), Relinks()
    session = Session(node.store, node.grant, [503])
    prove(monkeypatch, node, running, session)
    link = node.store.read(OUTBOX)["link"]
    deliver_app_link(node.store, session, relinks, current=AppRunKey.of(running))
    node.store.close()  # the broker exits; the boot store (/run) keeps the slot
    node.store = boot_store(node.tmp_path / "broker", node.producer.kernel_boot_id)
    restarted = Session(node.store, node.grant, [200])
    deliver_app_link(node.store, restarted, Relinks(), current=AppRunKey.of(running))
    assert restarted.requests == [("POST", "/v2/node/app-links", link.encode())]
    assert not node.store.read(OUTBOX)


# -- refusals that Central repeats: an owed relink -----------------------------------------


@pytest.mark.parametrize("status", [409, 400, 422])
def test_a_permanent_refusal_owes_that_run_a_relink(monkeypatch, node, status):
    running, relinks = running_app(), Relinks()
    session = Session(node.store, node.grant, [status])
    prove(monkeypatch, node, running, session)
    deliver_app_link(node.store, session, relinks, current=AppRunKey.of(running))
    run = AppRunKey.of(running)
    assert node.store.read(OUTBOX) == {"relink": run.document()}
    assert owed_relink(node.store, run) == run
    assert [(e.kind, e.value) for e in relinks.feed.read(0, incarnation=None).events] == [
        ("app_link_refused", {"run": run.document(), "status": status, "reason": "central_refused"})]


def test_a_link_proved_under_another_session_relinks_without_posting(monkeypatch, node):
    running, relinks = running_app(), Relinks()
    session = Session(node.store, node.grant)
    prove(monkeypatch, node, running, session)
    # The broker re-enrolled (a 401/403 drops the grant): Central would refuse the old
    # session's link as a scope mismatch forever (central/fleet/node_app_links.py:47-49).
    session.grant = NodeSessionGrant(node.producer, uuid4(), node.grant.offer_id, 500, "app_effect")
    deliver_app_link(node.store, session, relinks, current=AppRunKey.of(running))
    run = AppRunKey.of(running)
    assert session.requests == [] and node.store.read(OUTBOX) == {"relink": run.document()}
    assert relinks.feed.read(0, incarnation=None).events[0].value == {
        "run": run.document(), "status": None, "reason": "session_changed"}


def test_a_slot_whose_run_is_not_current_is_cleared(monkeypatch, node):
    running, relinks = running_app(), Relinks()
    session = Session(node.store, node.grant)
    prove(monkeypatch, node, running, session)
    deliver_app_link(node.store, session, relinks, current=AppRunKey.of(running_app(pid=397)))
    assert session.requests == [] and not node.store.read(OUTBOX)
    prove(monkeypatch, node, running, session)
    deliver_app_link(node.store, session, relinks, current=None)
    assert session.requests == [] and not node.store.read(OUTBOX)


def owe(monkeypatch, node, running):
    """Central refuses the held link permanently: the slot now owes `running` a relink."""
    session = Session(node.store, node.grant, [409])
    prove(monkeypatch, node, running, session)
    deliver_app_link(node.store, session, Relinks(), current=AppRunKey.of(running))
    assert node.store.read(OUTBOX) == {"relink": AppRunKey.of(running).document()}
    return session


def test_an_owed_relink_is_never_posted(monkeypatch, node):
    running = running_app()
    session = owe(monkeypatch, node, running)
    session.statuses = [200]
    relinks = Relinks()
    for _ in range(3):
        deliver_app_link(node.store, session, relinks, current=AppRunKey.of(running))
    assert len(session.requests) == 1  # the refused link's POST only
    assert node.store.read(OUTBOX) == {"relink": AppRunKey.of(running).document()}
    assert relinks.feed.read(0, incarnation=None).events == ()


def test_an_owed_relink_for_a_run_that_is_not_current_is_cleared(monkeypatch, node):
    running, later = running_app(), running_app(pid=397)
    session = owe(monkeypatch, node, running)
    assert owed_relink(node.store, AppRunKey.of(later)) is None
    assert owed_relink(node.store, None) is None
    deliver_app_link(node.store, session, Relinks(), current=AppRunKey.of(later))
    assert node.store.read(OUTBOX) == {} and owed_relink(node.store, AppRunKey.of(running)) is None
    session = owe(monkeypatch, node, running)
    deliver_app_link(node.store, session, Relinks(), current=None)
    assert node.store.read(OUTBOX) == {}


def test_the_players_next_proof_settles_the_owed_relink(monkeypatch, node):
    running = running_app()
    session = owe(monkeypatch, node, running)
    prove(monkeypatch, node, running, session)  # the Player relinked: latest proof wins
    assert "link" in node.store.read(OUTBOX)
    assert owed_relink(node.store, AppRunKey.of(running)) is None


def test_no_grant_holds_the_slot(monkeypatch, node):
    running, relinks = running_app(), Relinks()
    session = Session(node.store, node.grant)
    prove(monkeypatch, node, running, session)
    session.grant = None
    deliver_app_link(node.store, session, relinks, current=AppRunKey.of(running))
    assert session.requests == [] and node.store.read(OUTBOX)


# -- the main loop delivers each turn with a grant ------------------------------------------


def loop_turn(node, running, session, *, granted):
    owed = []
    probes = SimpleNamespace(check=lambda: None, publish_run=lambda run, recovery_may_be_armed: None,
                             feed=Feed(64), owe_relink=owed.append, owed=owed,
                             take_kill_due=lambda: None)
    session.ensure = lambda: session.grant if granted else None
    online = SimpleNamespace(broker=SimpleNamespace(record={"phase": "running"}, service=lambda: None),
                             tick=lambda: None)
    loop = BrokerLoop(broker=SimpleNamespace(reconcile=lambda: None), online=online, store=node.store,
                      session=session, driver=SimpleNamespace(current=lambda: running),
                      links=SimpleNamespace(serve_one=lambda: None, remember_grant=lambda: None),
                      probes=probes, feeds=SimpleNamespace(serve=lambda: None))
    loop.turn()
    return probes


def test_a_turn_delivers_the_slot_only_with_a_grant(monkeypatch, node):
    running = running_app()
    session = Session(node.store, node.grant, [200, 200])  # process evidence, then the link
    prove(monkeypatch, node, running, session)
    link = node.store.read(OUTBOX)["link"]
    monkeypatch.setattr("appliance.node.broker_runner.boottime_ms", lambda: 1000)
    loop_turn(node, running, session, granted=False)
    assert session.requests == [] and node.store.read(OUTBOX)["link"] == link
    probes = loop_turn(node, running, session, granted=True)
    assert [path for _, path, _ in session.requests] == ["/v2/node/evidence", "/v2/node/app-links"]
    assert not node.store.read(OUTBOX)
    assert [e.kind for e in probes.feed.read(0, incarnation=None).events] == ["app_link_recorded"]


def test_every_known_turn_restates_the_owed_relink_with_or_without_a_grant(monkeypatch, node):
    running = running_app()
    session = owe(monkeypatch, node, running)
    session.statuses = [200, 200]  # process evidence on each granted turn
    monkeypatch.setattr("appliance.node.broker_runner.boottime_ms", lambda: 1000)
    for granted in (False, True):
        probes = loop_turn(node, running, session, granted=granted)
        assert probes.owed == [AppRunKey.of(running)]
    assert [path for _, path, _ in session.requests] == [
        "/v2/node/app-links", "/v2/node/evidence"]  # the owed relink itself is never POSTed
    prove(monkeypatch, node, running, session)
    assert loop_turn(node, running, session, granted=False).owed == [None]
