"""The device fence on real PostgreSQL, through Central's real services and routes.

An act on a device (an operator's reboot, a Position trial, a Player's enrolment on its device)
holds its generation under a shared fence, so one slow transaction (a commit waiting on the disk)
never makes the next act on the same device, or the operator's device page, wait out the lock
timeout. Only a change to the generation (a boot offer, retirement) is exclusive."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_node_boot import publish_deployment
from test_registry import ADMIN, enroll

from central.app import create_app
from central.fleet.device_fence import fence_in, retire_in
from central.fleet.models import FleetError, NodeControlError
from central.fleet.node_boot import parse_node_deployment
from contracts.node_boot import NodeBootRequestV2


def _offered(registry):
    """The Pi's device, at generation 1, as its first boot offer through Central claims it."""
    from test_node_boot import cold_setup

    boots, _ = cold_setup(registry)
    return boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))


def _act(registry) -> None:
    """One act on the device: its generation read under the shared fence, committed."""
    with registry.db.transaction() as conn:
        fence_in(conn, DEVICE_ID, "act").generation()


class _Held:
    """A transaction that acted on the device and stays open, as a request whose COMMIT waits on a
    stalled WAL flush does."""

    def __init__(self, registry):
        self.ready, self.release = threading.Event(), threading.Event()

        def hold():
            with registry.db.transaction() as conn:
                fence_in(conn, DEVICE_ID, "act").generation()
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
    _offered(registry)
    app = create_app(registry.db, registry.clock, ADMIN)
    admin = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app, raise_server_exceptions=False) as client, _Held(registry):
        started = time.monotonic()
        _act(registry)
        page = client.get(f"/v1/operator/node/devices/{DEVICE_ID}", headers=admin)
        assert page.status_code == 200, page.text
        assert time.monotonic() - started < 2


def test_retirement_waits_for_an_open_act_then_refuses_every_act(registry):
    _offered(registry)
    player, _, _ = enroll(registry, device_id=DEVICE_ID)
    with ThreadPoolExecutor(1) as pool:
        with _Held(registry):
            retiring = pool.submit(registry.retire, player["player_id"], by="console")
            time.sleep(0.5)
            assert not retiring.done()
        retiring.result(10)
        with pytest.raises(NodeControlError) as refused:
            _act(registry)
        assert (refused.value.code, refused.value.status) == ("node_device_unavailable", 403)
    registry.retire(player["player_id"], by="console")     # retiring a retired device changes nothing
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT generation FROM devices WHERE device_id=%s",
                            (DEVICE_ID,)).fetchone()["generation"] == 2


def test_a_change_not_granted_in_time_is_busy_and_retryable(registry):
    _offered(registry)
    with _Held(registry), registry.db.transaction() as conn:
        conn.execute("SET LOCAL lock_timeout='100ms'")
        with pytest.raises(FleetError) as busy:
            fence_in(conn, DEVICE_ID, "change")
    assert (busy.value.code, busy.value.status, busy.value.retry_after) == ("node_device_busy", 503, 1)


def test_retirement_needs_change_and_bumps_the_generation_once(registry):
    _offered(registry)
    with registry.db.transaction() as conn:
        with pytest.raises(RuntimeError, match="device_fence_change_required"):
            retire_in(fence_in(conn, DEVICE_ID, "act"), 1000.0, by="console")
    for _ in range(2):
        with registry.db.transaction() as conn:
            retire_in(fence_in(conn, DEVICE_ID, "change"), 1000.0, by="console")
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT generation FROM devices WHERE device_id=%s",
                            (DEVICE_ID,)).fetchone()["generation"] == 2


def test_an_act_reads_the_devices_current_generation(registry):
    """The fence reads the device row's generation (decision 0034, S2e). Probe: read the
    generation as a constant 1 -> the second read is 1, fails."""
    _offered(registry)
    with registry.db.transaction() as conn:
        assert fence_in(conn, DEVICE_ID, "act").generation() == 1
        conn.execute("UPDATE devices SET generation=3 WHERE device_id=%s", (DEVICE_ID,))
    with registry.db.transaction() as conn:
        assert fence_in(conn, DEVICE_ID, "act").generation() == 3


def test_the_release_writer_never_waits_on_a_device(registry):
    _offered(registry)
    with registry.db.transaction() as conn:
        deployment = parse_node_deployment(bytes(conn.execute(
            "SELECT deployment_document FROM node_releases").fetchone()["deployment_document"]))
    with _Held(registry):
        started = time.monotonic()
        assert publish_deployment(registry.db, deployment, registry.clock) is True
        assert time.monotonic() - started < 1


def test_a_transaction_fences_its_one_device_again_but_never_upgrades_or_fences_another(registry):
    """Taking the device's fence again is free (a fenced transaction that reads the device again);
    an upgrade or a second device could deadlock, so both are refused."""
    _offered(registry)
    with registry.db.transaction() as conn:
        fence_in(conn, DEVICE_ID, "act")
        assert fence_in(conn, DEVICE_ID, "act").generation() == 1
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
