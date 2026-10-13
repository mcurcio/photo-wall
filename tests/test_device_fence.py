"""The device fence on real PostgreSQL, through Central's real routes.

A node request acts on its device's generation under a shared fence, so one slow transaction (a
commit waiting on the disk) never makes the next request from the same device, or the operator's
device page, wait out the lock timeout. Only a change to the generation (an offer, an enrolment,
retirement) is exclusive."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import BASE_TAG, BOOT_ID, DEVICE_ID, SERIAL
from test_node_boot import publish_deployment
from test_node_central import node_offer
from test_node_fleet_hosts import Host, _rig
from test_registry import ADMIN, enroll

from central.app import create_app
from central.fleet.device_fence import fence_in, retire_in
from central.fleet.models import FleetError
from central.fleet.node_boot import parse_node_deployment
from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from contracts.node_commands import NodeSessionClaim
from contracts.node_host_facts import HostFactsV2, encode_host_facts
from contracts.node_observation import HostMetricV2, HostObservationV2, encode_host_observation


def _enrolled(registry, owner="host_core"):
    """A host_core session under the default app's audience, enrolled through Central."""
    _, offer = node_offer(registry, NodeControlConfig())
    sessions = NodeSessions(registry.db, registry.clock, NodeControlConfig())
    claim = NodeSessionClaim(SERIAL, offer.offer_id, BOOT_ID, owner, uuid4(), uuid4(),
                             uuid4().hex + uuid4().hex)
    return sessions, claim, sessions.enroll(claim)


class _Held:
    """A transaction that authenticated `claim`'s session and stays open, as a node request
    whose COMMIT waits on a stalled WAL flush does."""

    def __init__(self, registry, sessions, claim):
        self.ready, self.release = threading.Event(), threading.Event()

        def hold():
            with registry.db.transaction() as conn:
                sessions.authenticate_in(conn, claim.session_id, claim.credential)
                self.ready.set()
                self.release.wait(30)

        self.thread = threading.Thread(target=hold)

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(10)
        return self

    def __exit__(self, *_):
        self.release.set()
        self.thread.join()


def test_a_slow_node_request_never_stalls_the_next_poll_or_the_device_page(registry):
    sessions, claim, grant = _enrolled(registry)
    app = create_app(registry.db, registry.clock, ADMIN)
    node = {"Authorization": "Bearer " + claim.credential, "X-Node-Session": str(grant.session_id)}
    admin = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app, raise_server_exceptions=False) as client, _Held(registry, sessions, claim):
        started = time.monotonic()
        polled = client.get("/v2/node/commands", headers=node)
        assert (polled.status_code, polled.json()) == (200, {"commands": []})
        page = client.get(f"/v1/operator/node/devices/{DEVICE_ID}", headers=admin)
        assert page.status_code == 200, page.text
        attempts = client.get(f"/v1/operator/node/devices/{DEVICE_ID}/app-attempts", headers=admin)
        assert attempts.status_code == 200, attempts.text
        assert time.monotonic() - started < 2


def test_retirement_waits_for_an_open_act_then_refuses_every_act(registry):
    sessions, claim, grant = _enrolled(registry)
    player, _, _ = enroll(registry, device_id=DEVICE_ID)
    app = create_app(registry.db, registry.clock, ADMIN)
    node = {"Authorization": "Bearer " + claim.credential, "X-Node-Session": str(grant.session_id)}
    with TestClient(app, raise_server_exceptions=False) as client, ThreadPoolExecutor(1) as pool:
        with _Held(registry, sessions, claim):
            retiring = pool.submit(registry.retire, player["player_id"])
            time.sleep(0.5)
            assert not retiring.done()
        retiring.result(10)
        polled = client.get("/v2/node/commands", headers=node)
        assert (polled.status_code, polled.json()) == (403, {"error": "node_device_unavailable"})
    registry.retire(player["player_id"])     # retiring a retired device changes nothing
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT generation FROM fleet_device_lifecycle WHERE device_id=%s",
                            (DEVICE_ID,)).fetchone()["generation"] == 2


def test_a_change_not_granted_in_time_is_busy_and_retryable(registry):
    sessions, claim, _ = _enrolled(registry)
    with _Held(registry, sessions, claim), registry.db.transaction() as conn:
        conn.execute("SET LOCAL lock_timeout='100ms'")
        with pytest.raises(FleetError) as busy:
            fence_in(conn, DEVICE_ID, "change")
    assert (busy.value.code, busy.value.status, busy.value.retry_after) == ("node_device_busy", 503, 1)


def test_retirement_needs_change_and_bumps_the_generation_once(registry):
    _enrolled(registry)
    with registry.db.transaction() as conn:
        with pytest.raises(RuntimeError, match="device_fence_change_required"):
            retire_in(fence_in(conn, DEVICE_ID, "act"), 1000.0)
    for _ in range(2):
        with registry.db.transaction() as conn:
            retire_in(fence_in(conn, DEVICE_ID, "change"), 1000.0)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT generation FROM fleet_device_lifecycle WHERE device_id=%s",
                            (DEVICE_ID,)).fetchone()["generation"] == 2


def test_the_release_writer_never_waits_on_a_device(registry):
    sessions, claim, _ = _enrolled(registry)
    with registry.db.transaction() as conn:
        deployment = parse_node_deployment(bytes(conn.execute(
            "SELECT document FROM node_deployments").fetchone()["document"]))
    with _Held(registry, sessions, claim):
        started = time.monotonic()
        assert publish_deployment(registry.db, deployment, registry.clock) is True
        assert time.monotonic() - started < 1


def test_a_transaction_fences_its_one_device_again_but_never_upgrades_or_fences_another(registry):
    """Taking the device's fence again is free (an operator read that resolves a session inside a
    fenced transaction); an upgrade or a second device could deadlock, so both are refused."""
    sessions, claim, grant = _enrolled(registry)
    with registry.db.transaction() as conn:
        sessions.authenticate_in(conn, claim.session_id, claim.credential)
        assert sessions.load_operator_target_in(conn, grant.session_id).grant.producer == grant.producer
        with pytest.raises(RuntimeError, match="device_fence_held"):
            fence_in(conn, DEVICE_ID, "change")
        with pytest.raises(RuntimeError, match="device_fence_held"):
            fence_in(conn, "another-device", "act")
        conn.commit()                                  # the fence ends with its transaction
        assert fence_in(conn, "another-device", "change").standing().kind == "unrecorded"
        with pytest.raises(RuntimeError, match="device_fence_held"):
            fence_in(conn, DEVICE_ID, "act")
        conn.commit()
        fence_in(conn, DEVICE_ID, "change")
        assert fence_in(conn, DEVICE_ID, "act").generation() == 1


class _StalledCommits:
    """A Database whose transactions wait for `release` before they commit, as a COMMIT behind a
    stalled WAL flush does."""

    def __init__(self, db):
        self.db, self.wrote, self.release = db, threading.Event(), threading.Event()

    @contextmanager
    def transaction(self):
        with self.db.transaction() as conn:
            yield conn
            self.wrote.set()
            assert self.release.wait(30)

    def __getattr__(self, name):
        return getattr(self.db, name)


@pytest.mark.parametrize("kind", ["observation", "facts"])
def test_a_producers_retry_during_its_own_slow_commit_is_a_duplicate(registry, kind):
    """A Node retries a post whose first COMMIT is stalled (its client timeout is 0.5 s): the
    retry waits for that commit and answers `duplicate`, never a 500."""
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    stalled = _StalledCommits(registry.db)
    slow = NodeObservations(NodeSessions(stalled, registry.clock, sessions.config))
    if kind == "observation":
        raw = encode_host_observation(HostObservationV2(
            host.grant.producer, 1, 5000, (HostMetricV2("soc_temperature", 50.0, "celsius"),)))
        record = "record"
    else:
        raw = encode_host_facts(HostFactsV2(
            host.grant.producer, 1, 1000, kernel_release="6.6.51+rpt-rpi-v8", interface="eth0",
            link_state="up", address="192.168.1.40", base_tag=BASE_TAG))
        record = "record_facts"
    session = (host.claim.session_id, host.claim.credential, raw)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(getattr(slow, record), *session)
        assert stalled.wrote.wait(10)
        retry = pool.submit(getattr(host.observations, record), *session)
        time.sleep(0.3)
        stalled.release.set()
        assert first.result(10)["disposition"] == "recorded"
        assert retry.result(10)["disposition"] == "duplicate"
