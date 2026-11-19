"""The device fence on real PostgreSQL, through Central's real services and routes.

A node session acts on its device's generation under a shared fence, so one slow transaction (a
commit waiting on the disk) never makes the next act on the same device, or the operator's device
page, wait out the lock timeout. Only a change to the generation (an offer, an enrolment,
retirement) is exclusive."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_node_boot import publish_deployment
from test_node_central import node_offer
from test_registry import ADMIN, enroll

from central.app import create_app
from central.fleet.device_fence import fence_in, retire_in
from central.fleet.models import FleetError
from central.fleet.node_boot import parse_node_deployment
from central.fleet.node_sessions import NodeControlConfig, NodeControlError, NodeSessions
from contracts.node_commands import NodeSessionClaim


def _enrolled(registry, owner="host_core"):
    """A host_core session under the default app's audience, enrolled through Central."""
    _, offer = node_offer(registry, NodeControlConfig())
    sessions = NodeSessions(registry.db, registry.clock, NodeControlConfig())
    claim = NodeSessionClaim(SERIAL, offer.offer_id, BOOT_ID, owner, uuid4(), uuid4(),
                             uuid4().hex + uuid4().hex)
    return sessions, claim, sessions.enroll(claim)


def _act(registry, sessions, claim) -> None:
    """One act of the session: its authentication under the device's shared fence, committed."""
    with registry.db.transaction() as conn:
        sessions.authenticate_in(conn, claim.session_id, claim.credential)


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


def test_a_slow_node_act_never_stalls_the_next_act_or_the_device_page(registry):
    sessions, claim, _ = _enrolled(registry)
    app = create_app(registry.db, registry.clock, ADMIN)
    admin = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app, raise_server_exceptions=False) as client, _Held(registry, sessions, claim):
        started = time.monotonic()
        _act(registry, sessions, claim)
        page = client.get(f"/v1/operator/node/devices/{DEVICE_ID}", headers=admin)
        assert page.status_code == 200, page.text
        assert time.monotonic() - started < 2


def test_retirement_waits_for_an_open_act_then_refuses_every_act(registry):
    sessions, claim, _ = _enrolled(registry)
    player, _, _ = enroll(registry, device_id=DEVICE_ID)
    with ThreadPoolExecutor(1) as pool:
        with _Held(registry, sessions, claim):
            retiring = pool.submit(registry.retire, player["player_id"], by="console")
            time.sleep(0.5)
            assert not retiring.done()
        retiring.result(10)
        with pytest.raises(NodeControlError) as refused:
            _act(registry, sessions, claim)
        assert (refused.value.code, refused.value.status) == ("node_device_unavailable", 403)
    registry.retire(player["player_id"], by="console")     # retiring a retired device changes nothing
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT generation FROM devices WHERE device_id=%s",
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
            retire_in(fence_in(conn, DEVICE_ID, "act"), 1000.0, by="console")
    for _ in range(2):
        with registry.db.transaction() as conn:
            retire_in(fence_in(conn, DEVICE_ID, "change"), 1000.0, by="console")
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT generation FROM devices WHERE device_id=%s",
                            (DEVICE_ID,)).fetchone()["generation"] == 2


def test_a_session_of_an_earlier_generation_is_fenced_out(registry):
    """The fence reads the device row's generation (decision 0034, S2e): once it moves on, the
    session enrolled at the earlier one is refused. Probe: read the generation as a constant 1 ->
    the stale session still authenticates."""
    sessions, claim, grant = _enrolled(registry)
    with registry.db.transaction() as conn:
        assert fence_in(conn, DEVICE_ID, "act").generation() == 1
        sessions.authenticate_in(conn, grant.session_id, claim.credential)
        conn.execute("UPDATE devices SET generation=3 WHERE device_id=%s", (DEVICE_ID,))
    with registry.db.transaction() as conn:
        assert fence_in(conn, DEVICE_ID, "act").generation() == 3
        with pytest.raises(NodeControlError, match="node_session_unavailable"):
            sessions.authenticate_in(conn, grant.session_id, claim.credential)


def test_the_release_writer_never_waits_on_a_device(registry):
    sessions, claim, _ = _enrolled(registry)
    with registry.db.transaction() as conn:
        deployment = parse_node_deployment(bytes(conn.execute(
            "SELECT deployment_document FROM node_releases").fetchone()["deployment_document"]))
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
