"""Host facts record (console DDD §64, G13, bead F1), Central side, on real PostgreSQL: every
row of the facts ingest table, the derived `host_facts` intake cap, the route's 422s, and G12's
`facts` (the current boot's host_core producer only) and `boot` (the current admission's offer
tag)."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import BASE_TAG, DEVICE_ID
from test_node_central import setup as node_setup
from test_node_fleet_hosts import Host, _device, _rig
from test_registry import ADMIN

from central.app import create_app
from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import HOST_FACTS_DAILY_CAP, NodeControlConfig, NodeControlError
from contracts.node_host_facts import BootReportV2, BootStageV2, HostFactsV2, encode_host_facts

VALUES = {"kernel_release": "6.6.51+rpt-rpi-v8", "interface": "eth0", "link_state": "up",
          "address": "192.168.1.40", "base_tag": BASE_TAG}


def _facts(producer, sequence, **fields):
    return HostFactsV2(producer, sequence, 1000 * sequence, **{**VALUES, **fields})


def _send(host, value):
    return host.observations.record_facts(host.claim.session_id, host.claim.credential,
                                          encode_host_facts(value))


def _row(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT sequence,first_received_at,received_at,payload "
                            "FROM node_host_facts").fetchall()


def _intake(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT coalesce(sum(used),0) n FROM node_intake_quotas "
                            "WHERE kind='host_facts'").fetchone()["n"]


def test_every_ingest_case(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    producer = host.grant.producer
    # No row: insert, both receipts now.
    start = registry.clock.utc()
    answer = _send(host, _facts(producer, 2))
    assert answer == {"stored": True, "disposition": "recorded", "received_at": start,
                      "authority_granted": False}
    [row] = _row(registry)
    assert (row["sequence"], row["first_received_at"], row["received_at"]) == (2, start, start)
    # Higher sequence, same values: sequence and received_at move, first_received_at stays.
    registry.clock.advance(100)
    assert _send(host, _facts(producer, 5))["disposition"] == "recorded"
    [row] = _row(registry)
    assert (row["sequence"], row["first_received_at"], row["received_at"]) == (5, start, start + 100)
    # The resend of the stored document is a duplicate (the payload moved with the sequence).
    registry.clock.advance(1)
    duplicate = _send(host, _facts(producer, 5))
    assert duplicate == {"stored": True, "disposition": "duplicate", "received_at": start + 100,
                         "authority_granted": False}
    # Same sequence, different payload: refused.
    with pytest.raises(NodeControlError, match="node_observation_identity_conflict") as refused:
        _send(host, _facts(producer, 5, link_state="down"))
    assert refused.value.status == 409
    # Lower sequence: stale, nothing changes.
    stale = _send(host, _facts(producer, 4, link_state="down"))
    assert stale["disposition"] == "stale" and stale["stored"] is False
    assert _row(registry)[0]["sequence"] == 5
    # Higher sequence, new values: replaced, both receipts now.
    registry.clock.advance(50)
    changed = registry.clock.utc()
    assert _send(host, _facts(producer, 9, link_state="down"))["disposition"] == "recorded"
    [row] = _row(registry)
    assert (row["sequence"], row["first_received_at"], row["received_at"]) == (9, changed, changed)
    assert json.loads(bytes(row["payload"]))["link_state"] == "down"
    # Only the two posts with new values claimed intake: a same-values resend at a higher
    # sequence (a Host Management restart) rewrites the row without using the day's quota.
    assert _intake(registry) == 2


RUNNING = BootReportV2((BootStageV2("handoff", "done", None, None, None),
                        BootStageV2("storage", "done", None, None, None),
                        BootStageV2("prepare", "running", None, None, None)), (), 0)
DONE = BootReportV2(tuple(BootStageV2(stage.stage, "done", None, None, None)
                          for stage in RUNNING.stages), (), 0)


def test_unchanged_detection_compares_boot_and_reads_an_older_row_without_it(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    observations = NodeObservations(sessions)
    producer = host.grant.producer
    start = registry.clock.utc()
    # An older node's document: no `boot` key, stored as it was posted.
    _send(host, _facts(producer, 1))
    assert "boot" not in json.loads(bytes(_row(registry)[0]["payload"]))
    # A restart resend without boot is unchanged against that row.
    registry.clock.advance(10)
    _send(host, _facts(producer, 2))
    assert _row(registry)[0]["first_received_at"] == start and _intake(registry) == 1
    # Boot appears: a change. Then the same nested boot again: unchanged, no intake claimed.
    registry.clock.advance(10)
    running_at = registry.clock.utc()
    _send(host, _facts(producer, 3, boot=RUNNING))
    registry.clock.advance(10)
    _send(host, _facts(producer, 4, boot=RUNNING))
    assert _row(registry)[0]["first_received_at"] == running_at and _intake(registry) == 2
    # A stage moves: a change, served by G12 as the stored plain document.
    registry.clock.advance(10)
    _send(host, _facts(producer, 5, boot=DONE))
    assert _intake(registry) == 3
    served = _device(observations.fleet_hosts())["facts"]
    assert served["first_received_at"] == registry.clock.utc()
    assert [stage["state"] for stage in served["boot"]["stages"]] == ["done", "done", "done"]
    assert served["boot"]["failed_units"] == [] and served["boot"]["failed_units_more"] == 0


def test_a_producer_not_the_sessions_is_refused(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    other = replace(host.grant.producer, incarnation_id=uuid4())
    with pytest.raises(NodeControlError, match="node_producer_mismatch") as refused:
        _send(host, _facts(other, 1))
    assert refused.value.status == 403
    assert _row(registry) == []


def test_the_host_facts_intake_cap(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    producer = host.grant.producer
    _send(host, _facts(producer, 1))
    with registry.db.transaction() as conn:
        conn.execute("UPDATE node_intake_quotas SET used=%s WHERE kind='host_facts'",
                     (HOST_FACTS_DAILY_CAP,))
    with pytest.raises(NodeControlError, match="node_intake_capacity") as refused:
        _send(host, _facts(producer, 2, address="192.168.1.41"))
    assert refused.value.status == 429
    # A duplicate claims nothing, so it still answers at the cap; so does a same-values resend
    # at a higher sequence (a restart loop cannot spend the quota a real change needs).
    assert _send(host, _facts(producer, 1))["disposition"] == "duplicate"
    assert _send(host, _facts(producer, 3))["disposition"] == "recorded"


def test_the_route_records_and_answers_422_for_each_malformed_body(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    good = json.loads(encode_host_facts(_facts(host.grant.producer, 1)))
    headers = {"Authorization": "Bearer " + host.claim.credential,
               "X-Node-Session": str(host.claim.session_id)}
    bad = [{"kernel_release": "6.6 51"}, {"kernel_release": "6" * 65}, {"link_state": "UP"},
           {"address": "192.168.01.40"}, {"interface": "eth\x070"}, {"interface": "eth‮0"},
           {"base": "v1"}, {"base_tag": "v 1"}]
    app = create_app(registry.db, registry.clock, ADMIN, node_control=NodeControlConfig("node-test"))
    with TestClient(app) as client:
        for change in bad:
            response = client.post("/v2/node/host-facts", headers=headers,
                                   content=json.dumps({**good, **change}).encode())
            assert response.status_code == 422, (change, response.text)
        assert client.post("/v2/node/host-facts", headers=headers,
                           content=b"x" * 3000).status_code == 413
        response = client.post("/v2/node/host-facts", headers=headers,
                               content=json.dumps(good).encode())
        assert response.status_code == 200 and response.json()["disposition"] == "recorded"
        assert client.post("/v2/node/host-facts", content=json.dumps(good).encode()).status_code == 401


def test_g12_serves_only_the_current_boots_facts_and_its_base_tag(registry):
    boots, sessions = _rig(registry)
    observations = NodeObservations(sessions)
    first = Host(registry, sessions, boots)
    received = registry.clock.utc()
    _send(first, _facts(first.grant.producer, 1))
    served = _device(observations.fleet_hosts())
    assert served["facts"] == {"first_received_at": received, **VALUES, "boot": None}
    assert served["boot"] == {"base_tag": BASE_TAG}
    # A reboot: the new admission has a session but no facts. The old boot's facts are the
    # newest facts Central holds for this box, yet they are never served.
    registry.clock.advance(30)
    second = Host(registry, sessions, boots)
    served = _device(observations.fleet_hosts())
    assert served["facts"] is None
    # The superseded boot's post still stores (allow_historical) and answers as the
    # observation path does; it is never served.
    assert _send(first, _facts(first.grant.producer, 2, address="192.168.1.41"))["disposition"] == "historical"
    assert _device(observations.fleet_hosts())["facts"] is None
    assert served["boot"] == {"base_tag": BASE_TAG}
    registry.clock.advance(5)
    _send(second, _facts(second.grant.producer, 1, kernel_release=None, address=None))
    served = _device(observations.fleet_hosts())
    assert served["facts"] == {**VALUES, "first_received_at": registry.clock.utc(),
                               "kernel_release": None, "address": None, "boot": None}


def test_g12_boot_is_null_without_an_admission_and_the_node_offers_base_tag_with_one(registry):
    sessions, _claim, _grant = node_setup(registry)  # an admission of a node boot offer
    spare = "device-" + "e" * 64
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id,first_seen,last_seen) VALUES(%s,1,1)", (spare,))
    read = NodeObservations(sessions).fleet_hosts()
    assert _device(read, DEVICE_ID)["boot"] == {"base_tag": BASE_TAG}
    assert _device(read, DEVICE_ID)["facts"] is None
    assert _device(read, spare)["boot"] is None and _device(read, spare)["facts"] is None

