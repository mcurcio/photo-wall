"""Central's reading of a silent Player, replayed from the 2026-10-09 incident (1b C4, owner q3:
"Central notices a silent Player"; requirements U4).

The incident on the test Pi: the Player app on HDMI-A-1 died and nothing restarted it, while
Host Management kept reporting. Central's view of that box is rebuilt here through Central's own
owner services on real PostgreSQL (enrollment, the Player's readiness report, Host Management's
samples, the App Effect Broker's process facts with no app link) and read through the
production operator API at T + SILENT_AFTER_SECONDS + 1 on Central's test clock, T being the
Player's last accepted report. Each case asserts what the console classifies from: the
snapshot's `last_report_at` against its served `silent_after_seconds`, the fleet host read's
receipt against its served host-silence limit, and the broker's exit in the node device read the
Player page's layer evidence reads (central/console/src/nodeRead.js `processFacts`).
tests/browser/test_silent_player_browser.py drives the console on the same state.
"""

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from support.calibrated_frame import calibrate_frame
from test_coordination import report
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_node_boot import claim_for, cold_setup
from test_registry import ADMIN, enroll

from central.app import create_app
from central.coordination import Coordinator
from central.fleet.node_ingest import NodeIngest
from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import NodeControlConfig
from central.node_runtime_reconciliation import NodeRuntimeReconciler
from central.registry import FrameCreate
from contracts.liveness import SILENT_AFTER_SECONDS
from contracts.models import FrameProfile
from contracts.node_boot import NodeBootRequestV2
from contracts.node_observation import (
    HOST_OBSERVATION_INTERVAL_SECONDS,
    HostMetricV2,
    HostObservationV2,
    encode_host_observation,
)
from contracts.node_protocol import (
    AppProcessFact,
    NodeEventV2,
    NodeProcessIdentity,
    NodeSnapshotV2,
    encode_node_message,
)

NODE = NodeControlConfig("node-test")
OPERATOR = {"Authorization": "Bearer " + ADMIN}
FRAME = "incident"
OUTPUT = "HDMI-A-1"


class Incident:
    """One netbooted box in the incident's state, built through Central's owner services.

    `last_report_at` (T) is the Player app's last accepted readiness report; the App Effect
    Broker reported the app running before it and exited after it, with no app link admitted on
    the boot (so Central can link the exit to no Output); Host Management reports every
    interval up to `read_at` = T + SILENT_AFTER_SECONDS + 1."""

    def __init__(self, registry, *, bound=True):
        self.registry = registry
        boots, self.sessions, _ = cold_setup(registry)
        offer = boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
        self.observations = NodeObservations(self.sessions)
        self.host_sequence = 0
        identity, _, _ = enroll(registry, count=2, device_id=DEVICE_ID)
        self.player_id = identity["player_id"]
        if bound:
            registry.create_frame(FrameCreate(
                id=FRAME, surface_id="wall", x_mm=100, y_mm=100, width_mm=300, height_mm=500,
                profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)))
            registry.bind(FRAME, self.player_id, OUTPUT, expected_generation=0)
            calibrate_frame(registry.db, FRAME)
        # The node sessions are admitted after the Position commit: an admitted node boot turns
        # the commit into a native trial, which this replay does not exercise.
        self.claims = {}
        for owner in ("host_core", "app_effect_broker"):
            claim = claim_for(offer, owner=owner)
            self.claims[owner] = (claim, self.sessions.enroll(claim))
        self.process = NodeProcessIdentity(321, 7, uuid4())
        self._broker(NodeSnapshotV2, 1, "running")
        self.host_sample()
        self.coordinator = Coordinator(registry.db, registry.clock)
        registry.clock.advance(1)
        self.coordinator.advance()
        assert self.coordinator.readiness(self.player_id, report(self.coordinator, identity))
        self.last_report_at = registry.clock.utc()
        registry.clock.advance(2)
        self.exit_evidence = self._broker(NodeEventV2, 2, "exited")
        self.exited_at = registry.clock.utc()
        self.read_at = self.last_report_at + SILENT_AFTER_SECONDS + 1
        while registry.clock.utc() + HOST_OBSERVATION_INTERVAL_SECONDS < self.read_at:
            registry.clock.advance(HOST_OBSERVATION_INTERVAL_SECONDS)
            self.host_sample()
        registry.clock.advance(self.read_at - registry.clock.utc())
        self.host_sample()
        NodeRuntimeReconciler(self.sessions, self.coordinator).advance()

    def host_sample(self):
        """One Host Management sample on its own boot clock (the test clock's monotonic)."""
        claim, grant = self.claims["host_core"]
        self.host_sequence += 1
        sample = HostObservationV2(grant.producer, self.host_sequence,
                                   int(self.registry.clock.monotonic() * 1000),
                                   (HostMetricV2("uptime", self.host_sequence, "seconds"),))
        self.observations.record(grant.session_id, claim.credential, encode_host_observation(sample))

    def _broker(self, kind, sequence, state):
        """The App Effect Broker's process fact for the Player app, as a snapshot or an event."""
        claim, grant = self.claims["app_effect_broker"]
        evidence = uuid4()
        fact = AppProcessFact(self.process, 1, "b" * 64, state)
        NodeIngest(self.sessions).ingest(grant.session_id, claim.credential, encode_node_message(
            kind(grant.producer, evidence, sequence, 1000 * sequence, (fact,))))
        return evidence


def _reads(registry):
    """The production operator API's three reads the console classifies from."""
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                               node_control=NODE)) as client:
        reads = [client.get(path, headers=OPERATOR) for path in (
            "/v1/operator/snapshot", "/v1/operator/node/hosts", f"/v1/operator/node/devices/{DEVICE_ID}")]
    for response in reads:
        assert response.status_code == 200, response.text
    return [response.json() for response in reads]


def _reads_silent_while_its_host_reports(registry, incident):
    snapshot, hosts, _ = _reads(registry)
    inventory = snapshot["inventory"]
    player = next(entry for entry in inventory["players"] if entry["id"] == incident.player_id)
    assert player["device_id"] == DEVICE_ID and player["retired_at"] is None
    assert player["last_report_at"] == incident.last_report_at
    assert inventory["silent_after_seconds"] == SILENT_AFTER_SECONDS
    assert inventory["read_at"] == incident.read_at
    assert inventory["read_at"] - player["last_report_at"] > inventory["silent_after_seconds"]
    # Host Management reads current against Central's own served host-silence limit.
    device = next(entry for entry in hosts["devices"] if entry["device_id"] == DEVICE_ID)
    assert device["host"] is not None
    assert hosts["read_at"] - device["host"]["received_at"] <= hosts["thresholds"]["host_silent_after_seconds"]
    return inventory


def test_a_bound_player_that_stops_reporting_reads_silent_while_its_host_reports(registry):
    incident = Incident(registry)
    inventory = _reads_silent_while_its_host_reports(registry, incident)
    frame = next(entry for entry in inventory["frames"] if entry["id"] == FRAME)
    assert (frame["player_id"], frame["output_id"]) == (incident.player_id, OUTPUT)


def test_an_unbound_player_that_stops_reporting_reads_silent_while_its_host_reports(registry):
    """U4: Central tells a silent Player from an unobserved host whether or not it is bound."""
    incident = Incident(registry, bound=False)
    inventory = _reads_silent_while_its_host_reports(registry, incident)
    assert all(frame["player_id"] != incident.player_id for frame in inventory["frames"])


def test_an_app_exit_with_no_app_link_is_readable_on_the_player(registry):
    incident = Incident(registry)
    with registry.db.transaction() as conn:
        work = conn.execute("SELECT completed_at,result FROM node_reconciliation_work "
                            "WHERE evidence_id=%s", (incident.exit_evidence,)).fetchone()
        links = conn.execute("SELECT count(*) n FROM node_app_links").fetchone()["n"]
    # The replay's premise: no app link, so the exit stays pending work and fences nothing.
    assert links == 0
    assert dict(work) == {"completed_at": None, "result": "awaiting_process_link"}
    _, _, device = _reads(registry)
    broker = [session for session in device["sessions"]
              if session["current"] and session["producer"]["owner"] == "app_effect_broker"]
    assert len(broker) == 1
    # Read as nodeRead.js processFacts reads the projection.
    facts = []
    for entry in broker[0]["projection"]:
        item = json.loads(entry["payload"])["message"]["facts"][entry["fact_index"]]
        if item["type"] == "AppProcessFact":
            facts.append((item["process"]["pid"], item["state"], entry["received_at"]))
    assert facts == [(incident.process.pid, "exited", incident.exited_at)]
