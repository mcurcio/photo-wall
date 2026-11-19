"""Central's reading of a silent Player, replayed from the 2026-10-09 incident (1b C4, owner q3:
"Central notices a silent Player"; requirements U4).

The incident on the test Pi: the Player app on HDMI-A-1 died and nothing restarted it, while
Host Management kept reporting. Central's view of that box is rebuilt here through Central's own
owner services on real PostgreSQL (enrollment, the Player's readiness report, Host Management's
metrics on its host line) and read through the production operator API at
T + SILENT_AFTER_SECONDS + 1 on Central's test clock, T being when Central last heard from the
Player. Each case asserts what the console classifies from: the snapshot's `last_report_at`
against its served `silent_after_seconds`, and the fleet host read's receipt against its served
host-silence limit. tests/browser/test_silent_player_browser.py drives the console on the same
state.
"""

from fastapi.testclient import TestClient
from support.frames import frame_create
from support.player_state import heard_in
from support.position import PORTRAIT_ON_WIDE, save_position
from test_coordination import report
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_node_boot import cold_setup
from test_node_fleet_hosts import on_line
from test_registry import ADMIN, enroll

from central.app import create_app
from central.coordination import Coordinator
from contracts.liveness import SILENT_AFTER_SECONDS
from contracts.node_boot import NodeBootRequestV2
from contracts.node_observation import HostMetricsState, HostMetricV2, encode_host_metrics_state
from contracts.node_state import BIRTH_KEY, HOST_METRICS_KEY

OPERATOR = {"Authorization": "Bearer " + ADMIN}
FRAME = "incident"
OUTPUT = "HDMI-A-1"


class Incident:
    """One netbooted box in the incident's state, built through Central's owner services.

    `last_report_at` (T) is when Central last heard from the Player app (its session socket's close
    after its readiness report; liveness is its own domain, central/infra/player_contact_store.py);
    Host Management states its metrics on its host line, last at `read_at` =
    T + SILENT_AFTER_SECONDS + 1."""

    def __init__(self, registry, *, bound=True):
        self.registry = registry
        boots, _ = cold_setup(registry)
        boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
        self.host_sequence = 0
        identity, _, _ = enroll(registry, count=2, device_id=DEVICE_ID)
        self.player_id = identity["player_id"]
        if bound:
            registry.create_frame(frame_create(FRAME, x_mm=100, y_mm=100), by="console")
            registry.bind(FRAME, self.player_id, OUTPUT, expected_generation=0, by="console")
            save_position(registry, FRAME, PORTRAIT_ON_WIDE)
        self.host_sample()
        self.coordinator = Coordinator(registry.db, registry.clock)
        registry.clock.advance(1)
        self.coordinator.advance()
        assert self.coordinator.readiness(self.player_id, report(self.coordinator, identity))
        with registry.db.transaction() as conn:     # its socket closed: Central last heard it
            heard_in(conn, self.player_id, identity["authority_epoch"], registry.clock.utc())
        self.last_report_at = registry.clock.utc()
        self.read_at = self.last_report_at + SILENT_AFTER_SECONDS + 1
        registry.clock.advance(self.read_at - registry.clock.utc())
        self.host_sample()

    def host_sample(self):
        """Host Management's metrics on its host line (the fleet host read and the device read,
        decision 0034), the line's birth first."""
        if self.host_sequence == 0:
            on_line(self.registry, SERIAL, BIRTH_KEY, b"{}", 1)
        self.host_sequence += 1
        metrics = (HostMetricV2("uptime", self.host_sequence, "seconds"),)
        on_line(self.registry, SERIAL, HOST_METRICS_KEY, encode_host_metrics_state(HostMetricsState(metrics)),
                self.host_sequence + 1)


def _reads(registry):
    """The production operator API's three reads the console classifies from."""
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
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
