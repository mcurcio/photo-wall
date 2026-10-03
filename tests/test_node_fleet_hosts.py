"""Host-health tracer, Central side (console DDD §60, §63-§64, bead T1), on real PostgreSQL:
observation coalescing on the producer's own boot clock (never a Central receipt clock, so a
stepped or skewed Central replica changes nothing), the derived daily cap, and G12
(`GET /v1/operator/node/hosts`): the current boot's sample only, the previous boot's receipt,
`intake_full`, the numbers-only thresholds, its access rules and its lock-free snapshot."""
import math
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import DEVICE_ID, SERIAL
from test_node_boot import claim_for, cold_setup
from test_registry import ADMIN, enroll

from central.app import create_app
from central.fleet.host_thresholds import HOST_SILENT_AFTER_SECONDS, thresholds_document
from central.fleet.locks import lock_fleet_assets_in
from central.fleet.node_observations import _FLEET_HOSTS_SQL, NodeObservations
from central.fleet.node_sessions import (
    OBSERVATION_DAILY_CAP,
    NodeControlConfig,
    NodeControlError,
    NodeSessions,
)
from contracts.node_boot import NodeBootRequestV2
from contracts.node_observation import (
    HOST_OBSERVATION_INTERVAL_SECONDS,
    HostMetricV2,
    HostObservationV2,
    encode_host_observation,
)

INTERVAL = HOST_OBSERVATION_INTERVAL_SECONDS
NODE = NodeControlConfig("node-test")


class Host:
    """Host Management on one boot of the fixture box, posting through Central's owner."""

    def __init__(self, registry, sessions, boots, boot_id=None):
        self.registry, self.sessions = registry, sessions
        self.offer = boots.offer(NodeBootRequestV2(SERIAL, boot_id or uuid4(), uuid4().hex + uuid4().hex))
        self.claim = claim_for(self.offer)
        self.grant = sessions.enroll(self.claim)
        self.observations = NodeObservations(sessions)
        self.sequence = 0

    def post(self, temperature=50.0, *, boottime_ms=None, observations=None):
        """One post. The node's boot clock follows the test clock's monotonic reading (in ms)
        unless `boottime_ms` says otherwise; `observations` posts through another Central
        replica."""
        self.sequence += 1
        if boottime_ms is None:
            boottime_ms = int(self.registry.clock.monotonic() * 1000)
        sample = HostObservationV2(self.grant.producer, self.sequence, boottime_ms,
                                   (HostMetricV2("soc_temperature", temperature, "celsius"),))
        return (observations or self.observations).record(
            self.claim.session_id, self.claim.credential, encode_host_observation(sample))


def _rig(registry, *, session_seconds=3600):
    boots, _, _ = cold_setup(registry)
    sessions = NodeSessions(registry.db, registry.clock,
                            NodeControlConfig("node-test", session_seconds=session_seconds))
    return boots, sessions


def _stored(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT count(*) n FROM node_host_observations").fetchone()["n"]


def _intake(registry):
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT coalesce(sum(used),0) n FROM node_intake_quotas "
                           "WHERE kind='observation'").fetchone()
        return row["n"]


def _device(read, device_id=DEVICE_ID):
    return next((item for item in read["devices"] if item["device_id"] == device_id), None)


def test_a_producer_posting_every_2_s_for_a_day_stores_one_per_interval_and_never_429(registry):
    # Start at a UTC day boundary so the whole day lands in one quota row.
    registry.clock.advance(86400 - registry.clock.utc() % 86400)
    boots, sessions = _rig(registry, session_seconds=86400)
    host = Host(registry, sessions, boots)
    dispositions = {}
    for _ in range(86400 // 2 - 1):
        answer = host.post()
        dispositions[answer["disposition"]] = dispositions.get(answer["disposition"], 0) + 1
        if answer["disposition"] == "coalesced":
            assert answer["stored"] is False and answer["authority_granted"] is False
        registry.clock.advance(2)
    stored = _stored(registry)
    assert stored <= math.ceil(86400 / INTERVAL) + 1
    assert stored == dispositions["recorded"] and dispositions["coalesced"] > 0
    # A coalesced post claims no intake: the day's quota counts stored samples only.
    assert _intake(registry) == stored < OBSERVATION_DAILY_CAP


def test_a_post_inside_the_interval_is_coalesced_and_one_after_it_stores(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    assert host.post()["disposition"] == "recorded"
    registry.clock.advance(INTERVAL - 0.5)
    assert host.post() == {"stored": False, "disposition": "coalesced",
                           "received_at": registry.clock.utc(), "authority_granted": False}
    registry.clock.advance(0.5)
    assert host.post()["disposition"] == "recorded"
    assert _stored(registry) == 2 and _intake(registry) == 2


def test_central_clock_steps_neither_coalesce_nor_store_a_post(registry):
    # Coalescing reads the producer's boot clock only. Central's wall clock stepping back 30 s
    # with the node's clock a whole interval on stores; stepping forward ten minutes (inside
    # the session) with the node's clock 1 s on coalesces. A receipt-clock judgement gets both backwards.
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    registry.clock.advance(60)
    assert host.post()["disposition"] == "recorded"
    registry.clock.advance(INTERVAL)
    registry.clock.step_utc(-30)
    assert host.post()["disposition"] == "recorded"
    registry.clock.advance(1)
    registry.clock.step_utc(600)
    assert host.post()["disposition"] == "coalesced"
    assert _stored(registry) == 2


def test_a_skewed_replica_judges_coalescing_by_the_producers_boot_clock(registry):
    # Two Central replicas whose clocks disagree by 40 s, one in each direction, share the
    # database. Whichever replica ingests, a post 2 s after the stored one on the node's clock
    # is coalesced and one an interval after it stores.
    from contracts.time import ManualClock
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    registry.clock.advance(120)
    assert host.post()["disposition"] == "recorded"

    def replica(skew):
        clock = ManualClock(wall=registry.clock.utc() + skew, mono=registry.clock.monotonic())
        return NodeObservations(NodeSessions(registry.db, clock, NODE)), clock

    for skew in (40, -40):
        other, clock = replica(skew)
        registry.clock.advance(2)
        clock.advance(2)
        answer = host.post(observations=other)
        assert answer["disposition"] == "coalesced", skew
        registry.clock.advance(INTERVAL)
        clock.advance(INTERVAL)
        assert host.post(observations=other)["disposition"] == "recorded", skew
    assert _stored(registry) == 3


def test_a_sample_older_than_the_stored_one_on_the_boot_clock_stores(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    assert host.post(boottime_ms=50_000)["disposition"] == "recorded"
    # A higher sequence carrying an earlier boot-clock reading: a negative difference stores.
    assert host.post(boottime_ms=49_000)["disposition"] == "recorded"
    assert host.post(boottime_ms=49_000 + INTERVAL * 1000 - 1)["disposition"] == "coalesced"


def test_the_same_sequence_is_still_a_duplicate_before_coalescing(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    first = host.post(boottime_ms=5000)
    host.sequence -= 1
    registry.clock.advance(1)
    again = host.post(boottime_ms=5000)
    assert again["disposition"] == "duplicate" and again["received_at"] == first["received_at"]


def test_g12_serves_the_current_boots_sample_and_only_the_previous_boots_receipt(registry):
    boots, sessions = _rig(registry)
    observations = NodeObservations(sessions)
    first = Host(registry, sessions, boots)
    first.post(81.2)
    old_receipt = registry.clock.utc()
    read = observations.fleet_hosts()
    served = _device(read)
    assert served["host"] == {"received_at": old_receipt, "fault_code": None,
                              "metrics": [{"name": "soc_temperature", "value": 81.2,
                                           "unit": "celsius", "source": "host_sampler"}]}
    assert served["previous_boot_received_at"] is None and served["intake_full"] is False
    assert read["thresholds"] == thresholds_document()
    assert read["thresholds"]["host_silent_after_seconds"] == HOST_SILENT_AFTER_SECONDS
    assert read["thresholds"]["metrics"][0] == {"name": "soc_temperature", "unit": "celsius",
                                                "notice_at": 75, "alarm_at": 80}
    # A reboot: the new admission has a session but no sample yet. The old boot's sample is
    # newer than anything of the new boot, yet it is never served as `host`.
    registry.clock.advance(30)
    second = Host(registry, sessions, boots)
    read = observations.fleet_hosts()
    served = _device(read)
    assert read["read_at"] == registry.clock.utc()
    assert served["host"] is None and served["previous_boot_received_at"] == old_receipt
    registry.clock.advance(5)
    second.post(40.0)
    served = _device(observations.fleet_hosts())
    assert served["host"]["metrics"][0]["value"] == 40.0
    assert served["previous_boot_received_at"] is None


def test_g12_intake_full_at_the_cap_and_429_after_it(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    host.post()
    with registry.db.transaction() as conn:
        conn.execute("UPDATE node_intake_quotas SET used=%s WHERE kind='observation'",
                     (OBSERVATION_DAILY_CAP - 1,))
    observations = NodeObservations(sessions)
    assert _device(observations.fleet_hosts())["intake_full"] is False
    registry.clock.advance(INTERVAL)
    assert host.post()["disposition"] == "recorded"
    assert _device(observations.fleet_hosts())["intake_full"] is True
    registry.clock.advance(INTERVAL)
    with pytest.raises(NodeControlError, match="node_intake_capacity"):
        host.post()


def test_g12_omits_retired_boxes_and_lists_active_ones_without_a_boot(registry):
    boots, sessions = _rig(registry)
    Host(registry, sessions, boots).post()
    spare_id = "device-" + "e" * 64
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id,first_seen,last_seen) VALUES(%s,1,1)", (spare_id,))
    retired_id = "device-" + "f" * 64
    retired = enroll(registry, count=1, device_id=retired_id)[0]["player_id"]
    registry.retire(retired)
    read = NodeObservations(sessions).fleet_hosts()
    assert _device(read, retired_id) is None
    assert _device(read) is not None
    assert _device(read, spare_id) == {"device_id": spare_id, "host": None,
                                       "previous_boot_received_at": None, "intake_full": False,
                                       "preparation_intake_full": False,
                                       "boot": None, "facts": None, "preparation": None}


def test_a_held_fleet_lock_does_not_block_g12(registry):
    boots, sessions = _rig(registry)
    Host(registry, sessions, boots).post()
    held, release = threading.Event(), threading.Event()

    def hold():
        with registry.db.transaction() as conn:
            lock_fleet_assets_in(conn)
            conn.execute("SELECT 1 FROM devices WHERE device_id=%s FOR UPDATE", (DEVICE_ID,))
            conn.execute("SELECT 1 FROM fleet_device_lifecycle WHERE device_id=%s FOR UPDATE",
                         (DEVICE_ID,))
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        started = time.monotonic()
        read = NodeObservations(sessions).fleet_hosts()
        assert time.monotonic() - started < 2
        assert _device(read)["host"] is not None
    finally:
        release.set()
        holder.join()


def test_g12_route_access(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    host.post()
    app = create_app(registry.db, registry.clock, ADMIN, node_control=NODE)
    with TestClient(app) as client:
        assert client.get("/v1/operator/node/hosts").status_code == 401
        player = {"Authorization": "Bearer " + host.claim.credential,
                  "X-Node-Session": str(host.claim.session_id)}
        assert client.get("/v1/operator/node/hosts", headers=player).status_code == 401
        response = client.get("/v1/operator/node/hosts", headers={"Authorization": "Bearer " + ADMIN})
        assert response.status_code == 200, response.text
        assert _device(response.json())["host"]["metrics"][0]["name"] == "soc_temperature"
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        response = client.get("/v1/operator/node/hosts", headers={"Authorization": "Bearer " + ADMIN})
        assert response.status_code == 503 and response.json()["error"] == "node_control_disabled"


def test_g12_reads_one_producers_newest_sample_through_the_primary_key(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    host.post()
    with registry.db.transaction() as conn:
        producer = conn.execute("SELECT producer_id FROM node_producers WHERE owner='host_core'"
                                ).fetchone()["producer_id"]
        conn.execute("INSERT INTO node_host_observations(producer_id,sequence,payload,received_at) "
                     "SELECT %s,s,'{}'::bytea,1000+s FROM generate_series(2,50001) s", (producer,))
        conn.execute("ANALYZE node_host_observations")
        plan = "\n".join(row["QUERY PLAN"] for row in conn.execute(
            "EXPLAIN " + _FLEET_HOSTS_SQL, {"day": 0}).fetchall())
    assert "node_host_observations_pkey" in plan, plan
    assert "Seq Scan on node_host_observations" not in plan, plan


def test_g12_preparation_is_the_current_boots_newest_app_manager_sample(registry, monkeypatch):
    """A `refused` sample built by the node's own PreparationObservation (not a stub) reaches
    G12's `preparation` with both numbers (§63); a later `idle` sample replaces it."""
    from types import SimpleNamespace

    from appliance.node.manager_observation import PreparationObservation
    monkeypatch.setattr("appliance.node.manager_observation.boottime_ms", lambda: 9000)
    boots, sessions = _rig(registry)
    observations = NodeObservations(sessions)
    host = Host(registry, sessions, boots)
    host.post()
    assert _device(observations.fleet_hosts())["preparation"] is None
    claim = claim_for(host.offer, owner="app_manager")
    grant = sessions.enroll(claim)

    def request(method, path, body):
        assert (method, path) == ("POST", "/v2/node/app-preparation")
        observations.record_preparation(claim.session_id, claim.credential, body)
        return 200, b"{}"

    class Store(dict):
        def read(self, key):
            return self.get(key)

        def write(self, key, row):
            self[key] = row

    node = PreparationObservation(Store(), SimpleNamespace(grant=grant, request=request))
    registry.clock.advance(3)
    node.sample("refused", fault="node_storage_capacity", available_bytes=900_000_000,
                required_bytes=1_400_000_000)
    refused_at = registry.clock.utc()
    assert _device(observations.fleet_hosts())["preparation"] == {
        "received_at": refused_at, "state": "refused", "fault": "node_storage_capacity",
        "available_bytes": 900_000_000, "required_bytes": 1_400_000_000}
    registry.clock.advance(4)
    node.sample("idle")
    assert _device(observations.fleet_hosts())["preparation"] == {
        "received_at": registry.clock.utc(), "state": "idle", "fault": None,
        "available_bytes": None, "required_bytes": None}
    # A reboot: the new admission's App Manager has sent nothing, so nothing is served; the
    # old boot's sample never is.
    registry.clock.advance(30)
    Host(registry, sessions, boots)
    assert _device(observations.fleet_hosts())["preparation"] is None


class _Store(dict):
    def read(self, key):
        return self.get(key)

    def write(self, key, row):
        self[key] = row


def _app_manager(registry, sessions, host, observations, monkeypatch):
    """The node's own PreparationObservation for `host`'s boot, posting into Central, with the
    node's boot clock following Central's test clock (in ms)."""
    from types import SimpleNamespace

    from appliance.node.manager_observation import PreparationObservation
    monkeypatch.setattr("appliance.node.manager_observation.boottime_ms",
                        lambda: int(registry.clock.monotonic() * 1000))
    claim = claim_for(host.offer, owner="app_manager")
    grant = sessions.enroll(claim)
    answers = []

    def request(method, path, body):
        try:
            answers.append(observations.record_preparation(claim.session_id, claim.credential, body))
        except NodeControlError as exc:
            answers.append({"disposition": exc.code})
            return exc.status, b"{}"
        return 200, b"{}"

    return PreparationObservation(_Store(), SimpleNamespace(grant=grant, request=request)), answers


def _prep_intake(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT coalesce(sum(used),0) n FROM node_intake_quotas "
                            "WHERE kind='preparation'").fetchone()["n"]


def test_a_day_of_preparing_refused_alternation_never_fills_the_cap_and_g12_stays_refused(
        registry, monkeypatch):
    """The sustained storage refusal (§64): App Manager samples `preparing`, then `refused`
    with the drifting room, on every poll. Central coalesces per (state, operation, fault)
    per interval, so the day stores about two per interval, never reaches the derived cap, and
    G12's newest sample stays `refused` all day; `verified` after storage frees is stored at
    once."""
    from uuid import UUID

    from central.fleet.node_sessions import PREPARATION_DAILY_CAP
    registry.clock.advance(86400 - registry.clock.utc() % 86400)
    boots, sessions = _rig(registry, session_seconds=86400)
    observations = NodeObservations(sessions)
    host = Host(registry, sessions, boots)
    node, answers = _app_manager(registry, sessions, host, observations, monkeypatch)
    command = type("Command", (), {"operation_id": UUID(int=7), "fallback": None,
                                   "target": type("T", (), {"environment_sha256": "a" * 64})()})()
    not_refused = 0
    # A 6 s poll keeps the day's test time down; it is still well inside one interval, so
    # every window coalesces repeats of both states, as the real 2 s poll does.
    tick_seconds = 6
    assert tick_seconds < INTERVAL
    for tick in range(86400 // tick_seconds - 1):
        node.sample("preparing", command=command)
        node.sample("refused", command=command, fault="node_storage_capacity",
                    available_bytes=900_000_000 - tick, required_bytes=1_400_000_000)
        if tick % 150 == 0:
            served = _device(observations.fleet_hosts())
            not_refused += served["preparation"]["state"] != "refused"
            assert served["preparation_intake_full"] is False
        registry.clock.advance(tick_seconds)
    dispositions = {answer["disposition"] for answer in answers}
    assert "node_intake_capacity" not in dispositions and "coalesced" in dispositions
    stored = _prep_intake(registry)
    assert stored <= 2 * (math.ceil(86400 / INTERVAL) + 1) < PREPARATION_DAILY_CAP
    assert not_refused == 0
    # Storage frees: `verified` is a new (state, operation, fault), stored at once, and G12
    # serves it (the incident clears on this sample).
    node.sample("verified", command=command)
    assert _device(observations.fleet_hosts())["preparation"]["state"] == "verified"


def test_g12_preparation_intake_full_at_the_derived_cap(registry, monkeypatch):
    from central.fleet.node_sessions import PREPARATION_DAILY_CAP
    boots, sessions = _rig(registry)
    observations = NodeObservations(sessions)
    host = Host(registry, sessions, boots)
    node, answers = _app_manager(registry, sessions, host, observations, monkeypatch)
    node.sample("idle")
    with registry.db.transaction() as conn:
        conn.execute("UPDATE node_intake_quotas SET used=%s WHERE kind='preparation'",
                     (PREPARATION_DAILY_CAP - 1,))
    assert _device(observations.fleet_hosts())["preparation_intake_full"] is False
    registry.clock.advance(1)
    node.sample("refused", fault="node_storage_capacity", available_bytes=1, required_bytes=2)
    assert answers[-1]["disposition"] == "recorded"
    assert _device(observations.fleet_hosts())["preparation_intake_full"] is True
    registry.clock.advance(INTERVAL)
    node.sample("idle")
    assert answers[-1]["disposition"] == "node_intake_capacity"
