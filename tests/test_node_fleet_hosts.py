"""Host-health tracer, Central side (console DDD §60, §63-§64, bead T1), on real PostgreSQL: G12
(`GET /v1/operator/node/hosts`): the metrics and facts each Pi's host line states as Central's link
store records them (decision 0034, S4a), the numbers-only thresholds, the Node API link with the
hub's state, its access rules and its lock-free snapshot."""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
from support.devices import seed_device
from test_fleet_attempts import DEVICE_ID, SERIAL
from test_node_boot import cold_setup
from test_registry import ADMIN, enroll

from central.app import create_app
from central.db import ProcessTransactionClock
from central.fleet.device_fence import fence_in
from central.fleet.host_thresholds import HOST_SILENT_AFTER_SECONDS, thresholds_document
from central.fleet.node_bus_presence import (
    HUB_REACHABILITY,
    record_hub_reachable_in,
    record_presence_in,
)
from central.fleet.node_observations import NodeObservations, host_facts_in
from central.infra.node_link_store import PgLinkStores
from contracts.node_boot import NodeBootRequestV2
from contracts.node_host_facts import HostFactsState, encode_host_facts_state
from contracts.node_link import Pipe
from contracts.node_observation import HostMetricsState, HostMetricV2, encode_host_metrics_state
from contracts.node_state import BIRTH_KEY, HOST_FACTS_KEY, HOST_LINE, HOST_METRICS_KEY
from nodeapi.buffers import state_prefix, state_stream
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read


class Host:
    """Host Management on one boot of the fixture box: Central's boot offer."""

    def __init__(self, registry, boots, boot_id=None):
        self.registry = registry
        self.offer = boots.offer(NodeBootRequestV2(SERIAL, boot_id or uuid4(), uuid4().hex + uuid4().hex))


def on_line(registry, serial, key, value, seq):
    """`serial`'s host line states `value` under `key`, recorded by Central's real link store as the
    worker's fleet-pipe NodeLink records it."""
    read = Read(Token("epoch", seq), state_prefix(HOST_LINE) + key, {}, value)
    commit = PgLinkStores(registry.db, {}).store(serial, Pipe.FLEET).commit(
        state_stream(HOST_LINE), Batch((read,), Token("epoch", seq), False))
    # Its own thread, so a caller already inside an event loop (Playwright's) can call it.
    with ThreadPoolExecutor(1) as worker:
        worker.submit(asyncio.run, commit).result()


def metrics_on_line(registry, serial, temperature, seq):
    on_line(registry, serial, HOST_METRICS_KEY, encode_host_metrics_state(
        HostMetricsState((HostMetricV2("soc_temperature", temperature, "celsius"),))), seq)


FACTS = HostFactsState(kernel_release="6.6.51+rpt-rpi-v8", interface="eth0", link_state="up",
                       address="192.168.1.40", base_tag="v2.1.0", reset_cause=None,
                       wall_clock="synchronised", boot=None)


def _rig(registry):
    """Central's boot offer, the fixture box's version kept (cold_setup)."""
    boots, _ = cold_setup(registry)
    return boots


def _device(read, device_id=DEVICE_ID):
    return next((item for item in read["devices"] if item["device_id"] == device_id), None)


def test_g12_serves_what_the_host_line_states(registry):
    """Criterion 4. Mutation probe: serve no metrics from the line -> `host` stays None, fails."""
    boots = _rig(registry)
    observations = NodeObservations(registry.db)
    Host(registry, boots)
    served = _device(observations.fleet_hosts())
    assert served["host"] is None and served["facts"] is None
    assert "previous_boot_received_at" not in served
    metrics_on_line(registry, SERIAL, 81.2, 1)
    on_line(registry, SERIAL, HOST_FACTS_KEY, encode_host_facts_state(FACTS), 2)
    read = observations.fleet_hosts()
    served = _device(read)
    with registry.db.transaction() as conn:
        recorded = {row["subject"]: row["recorded_at"] for row in conn.execute(
            "SELECT subject, recorded_at FROM node_states WHERE device_id=%s", (DEVICE_ID,)).fetchall()}
    assert served["host"] == {"received_at": recorded[state_prefix(HOST_LINE) + HOST_METRICS_KEY],
                              "fault_code": None,
                              "metrics": [{"name": "soc_temperature", "value": 81.2,
                                           "unit": "celsius", "source": "host_sampler"}]}
    assert served["facts"] == {"first_received_at": recorded[state_prefix(HOST_LINE) + HOST_FACTS_KEY],
                               "kernel_release": "6.6.51+rpt-rpi-v8", "interface": "eth0",
                               "link_state": "up", "address": "192.168.1.40", "base_tag": "v2.1.0",
                               "reset_cause": None, "wall_clock": "synchronised", "boot": None}
    # Central's offer's base, from the device's newest boot offer.
    assert served["boot"]["base_tag"]
    # read_at is the database's clock, the one every receipt here was stamped by.
    assert 0 <= read["read_at"] - served["host"]["received_at"] < 60
    assert read["thresholds"] == thresholds_document()
    assert read["thresholds"]["host_silent_after_seconds"] == HOST_SILENT_AFTER_SECONDS
    assert read["thresholds"]["metrics"][0] == {"name": "soc_temperature", "unit": "celsius",
                                                "notice_at": 75, "alarm_at": 80}
    # A newer value replaces it; the Releases page and Central's state read the same facts.
    metrics_on_line(registry, SERIAL, 40.0, 3)
    assert _device(observations.fleet_hosts())["host"]["metrics"][0]["value"] == 40.0
    with registry.db.transaction() as conn:
        assert host_facts_in(conn)[DEVICE_ID]["address"] == "192.168.1.40"


def test_the_device_read_states_the_host_line_and_nothing_of_the_old_path(registry):
    """S4c-2a-iii and 2a-iv: the device read's host part is the host line's (its birth's stream
    creation and when Central recorded the birth and the newest metrics or facts); no session,
    producer, admission, host sample, App Manager preparation or display exchange is served.
    Mutation probe: serve `sessions` again -> the key set differs, fails."""
    boots = _rig(registry)
    observations = NodeObservations(registry.db)
    Host(registry, boots)
    assert observations.status(DEVICE_ID)["host"] is None   # no birth on the line yet
    on_line(registry, SERIAL, BIRTH_KEY, b"{}", 1)
    metrics_on_line(registry, SERIAL, 50.0, 2)
    read = observations.status(DEVICE_ID)
    assert set(read) == {"device_id", "device_generation", "read_at", "host", "boot_claims",
                         "reboot_commands", "physical_output"}
    with registry.db.transaction() as conn:
        recorded = {row["subject"]: row["recorded_at"] for row in conn.execute(
            "SELECT subject, recorded_at FROM node_states WHERE device_id=%s", (DEVICE_ID,)).fetchall()}
    assert read["host"] == {"epoch": "epoch", "born_at": recorded[state_prefix(HOST_LINE) + BIRTH_KEY],
                            "received_at": recorded[state_prefix(HOST_LINE) + HOST_METRICS_KEY]}
    assert read["boot_claims"][0]["kernel_boot_id"]


def test_a_pi_heard_only_on_its_line_is_served(registry):
    """A Pi with no old-path session at all (enrolled, never posted): its line alone is its host."""
    boots = _rig(registry)
    Host(registry, boots)
    metrics_on_line(registry, SERIAL, 50.0, 1)
    served = _device(NodeObservations(registry.db).fleet_hosts())
    assert served["host"]["metrics"][0]["value"] == 50.0


def test_g12_omits_retired_boxes_and_lists_active_ones_without_a_boot(registry):
    boots = _rig(registry)
    Host(registry, boots)
    metrics_on_line(registry, SERIAL, 50.0, 1)
    spare_id = "device-" + "e" * 64
    with registry.db.transaction() as conn:
        seed_device(conn, spare_id, None)
    retired_id = "device-" + "f" * 64
    retired = enroll(registry, count=1, device_id=retired_id)[0]["player_id"]
    registry.retire(retired, by="console")
    read = NodeObservations(registry.db).fleet_hosts()
    assert _device(read, retired_id) is None
    assert _device(read)["serial"] == SERIAL  # the box's claim at boot, for the console's names
    assert _device(read, spare_id) == {"device_id": spare_id, "serial": None, "host": None,
                                       "boot": None, "facts": None,
                                       "bus_link": {"linked": None, "changed_at": None,
                                                    "hub": "no_hub"}}


def test_g12_serves_each_boxs_own_bus_link_linked_unlinked_and_never_seen(registry):
    boots = _rig(registry)
    Host(registry, boots)
    unlinked_id, unseen_id = "device-" + "d" * 64, "device-" + "e" * 64
    with registry.db.transaction() as conn:
        for device_id in (unlinked_id, unseen_id):
            seed_device(conn, device_id, None)
        record_presence_in(conn, {DEVICE_ID: True}, 100.0)
        record_presence_in(conn, {unlinked_id: True}, 110.0)
        record_presence_in(conn, {unlinked_id: False}, 120.0)
        record_presence_in(conn, {unlinked_id: False}, 130.0)   # unchanged: not rewritten
        record_hub_reachable_in(conn, True, times=ProcessTransactionClock(registry.clock))
        conn.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now())")
    read = NodeObservations(registry.db).fleet_hosts()
    assert _device(read)["bus_link"] == {"linked": True, "changed_at": 100.0, "hub": "reachable"}
    assert _device(read, unlinked_id)["bus_link"] == {"linked": False, "changed_at": 120.0,
                                                      "hub": "reachable"}
    assert _device(read, unseen_id)["bus_link"] == {"linked": None, "changed_at": None,
                                                    "hub": "reachable"}


def test_the_hubs_reachability_is_written_only_when_it_changes(registry):
    """Mutation probe (H1): make `Singleton.write_in` skip its equality check -> revision 2."""
    times = ProcessTransactionClock(registry.clock)
    with registry.db.transaction() as conn:
        record_hub_reachable_in(conn, True, times=times)
    registry.clock.advance(5)
    with registry.db.transaction() as conn:
        record_hub_reachable_in(conn, True, times=times)
        assert conn.execute("SELECT pg_current_xact_id_if_assigned() AS x").fetchone()["x"] is None
        stored = HUB_REACHABILITY.read_in(conn)
    assert (stored.revision, stored.changed_at, stored.changed_by) == (1, 1000.0, "central")
    with registry.db.transaction() as conn:
        record_hub_reachable_in(conn, False, times=times)
        conn.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now())")
        assert HUB_REACHABILITY.read_in(conn).revision == 2
    assert _device_hub(registry) == "unreachable"


def _device_hub(registry) -> str:
    from central.fleet.node_bus_presence import bus_links_in
    with registry.db.transaction() as conn:
        return bus_links_in(conn, [DEVICE_ID])[DEVICE_ID]["hub"]


def test_a_held_device_fence_does_not_block_g12(registry):
    boots = _rig(registry)
    Host(registry, boots)
    metrics_on_line(registry, SERIAL, 50.0, 1)
    held, release = threading.Event(), threading.Event()

    def hold():
        with registry.db.transaction() as conn:
            fence_in(conn, DEVICE_ID, "change")
            conn.execute("SELECT 1 FROM devices WHERE device_id=%s FOR UPDATE", (DEVICE_ID,))
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        started = time.monotonic()
        read = NodeObservations(registry.db).fleet_hosts()
        assert time.monotonic() - started < 2
        assert _device(read)["host"] is not None
    finally:
        release.set()
        holder.join()


def test_g12_route_access(registry):
    boots = _rig(registry)
    Host(registry, boots)
    metrics_on_line(registry, SERIAL, 50.0, 1)
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        assert client.get("/v1/operator/node/hosts").status_code == 401
        player = {"Authorization": "Bearer " + uuid4().hex + uuid4().hex}   # not the operator's token
        assert client.get("/v1/operator/node/hosts", headers=player).status_code == 401
        response = client.get("/v1/operator/node/hosts", headers={"Authorization": "Bearer " + ADMIN})
        assert response.status_code == 200, response.text
        assert _device(response.json())["host"]["metrics"][0]["name"] == "soc_temperature"
