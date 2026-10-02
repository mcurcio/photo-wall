"""The Players list and the Player page against real node records (console DDD §9-§10,
beads B1 to B3).

The CI-gated fleet browser suite (R11): the production app on the loopback harness with node
management mounted (`operator_server(..., node_control=...)`), real node sessions, host
samples, manager preparation and broker evidence recorded through Central's own owner
services, and a real Registry Player enrolled on the same device. It proves the device-keyed
join, the node read (only on an open Player page), the facts' labels and their Unknowns, the
per-section error boundary, node management being off, and the retired `#/equipment` bookmark;
and (B2) Reboot Player against Central's real reboot owner behind an open effect gate: the
frozen request, its retry, its window, late responses and the app operations' named states;
and (B3) the V1 lane: the V1 fleet target set from the Players list, and a queued maintenance
request shown on the Player page with Cancel as its only control.

Every assertion is behavioural (role, text, outcome); ages are Central's read time minus
Central's receipt time, driven by the registry's controlled clock.
"""

import json
import os
from uuid import UUID, uuid4

import pytest
from console_tasks import connect, current_hash, go, open_player
from operator_harness import drive_poll, operator_server, report_readiness
from playwright.sync_api import expect
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_fleet_rollout_gate import _certificate, _gate, _LocalImageVerifier
from test_node_boot import claim_for, cold_setup
from test_registry import enroll

from central.fleet.models import Artifact, MaintenanceRequestWrite, PolicyWrite
from central.fleet.node_commands import NodeCommands, OperatorReboot
from central.fleet.node_ingest import NodeIngest
from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import NodeControlConfig
from central.fleet.service import FleetService
from central.registry import FrameCreate
from contracts.models import FrameProfile
from contracts.node_boot import NodeBootRequestV2
from contracts.node_observation import HostMetricV2, HostObservationV2, encode_host_observation
from contracts.node_preparation import ManagerPreparationV2, encode_manager_preparation
from contracts.node_protocol import (
    AppProcessFact,
    NodeCommandResponseV2,
    NodeProcessIdentity,
    NodeSnapshotV2,
    encode_node_message,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NODE = NodeControlConfig("node-test")
NAME = f"Player …{SERIAL[-6:]}"
DEVICE_READ = f"**/v1/operator/node/devices/{DEVICE_ID}"
OFF = "Unknown: node management is off on this Central"


class Box:
    """One netbooted box with node sessions for Host Management, App Manager and the App
    Effect Broker on its current boot, each reporting through Central's owner services."""

    def __init__(self, registry, *, enrolled=True):
        self.registry = registry
        boots, self.sessions, _ = cold_setup(registry)
        offer = boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
        self.claims = {}
        for owner in ("host_core", "app_manager", "app_effect_broker"):
            claim = claim_for(offer, owner=owner)
            self.claims[owner] = (claim, self.sessions.enroll(claim))
        self.observations = NodeObservations(self.sessions)
        self.sequence = 0
        self.player_id = enroll(registry, count=1, device_id=DEVICE_ID)[0]["player_id"] if enrolled else None

    def host_sample(self):
        claim, grant = self.claims["host_core"]
        self.sequence += 1
        sample = HostObservationV2(grant.producer, self.sequence, 1000 * self.sequence,
                                   (HostMetricV2("uptime", self.sequence, "seconds"),))
        self.observations.record(grant.session_id, claim.credential, encode_host_observation(sample))

    def preparation(self):
        claim, grant = self.claims["app_manager"]
        sample = ManagerPreparationV2(grant.producer, 1, 1000, "verified", uuid4(), "d" * 64,
                                      None, 500, 100)
        self.observations.record_preparation(claim.session_id, claim.credential,
                                             encode_manager_preparation(sample))

    def app_running(self):
        claim, grant = self.claims["app_effect_broker"]
        fact = AppProcessFact(NodeProcessIdentity(100, 1, uuid4()), 1, "b" * 64, "running")
        snapshot = NodeSnapshotV2(grant.producer, uuid4(), 1, 1000, (fact,))
        NodeIngest(self.sessions).ingest(grant.session_id, claim.credential,
                                         encode_node_message(snapshot))


def _node_reads(page):
    """Every node read the page sends, in order."""
    sent = []
    page.on("request", lambda request: sent.append(request.url)
            if "/v1/operator/node/" in request.url else None)
    return sent


def _layer(page, layer):
    return page.get_by_role("region", name="Layers", exact=True).get_by_role(
        "group", name=layer, exact=True)


def test_the_tracer_reaches_a_player_page_from_the_list_with_standing_and_host_age(page, registry):
    box = Box(registry)
    box.host_sample()
    registry.clock.advance(4)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        sent = _node_reads(page)
        connect(page, origin, "players")
        row = page.get_by_role("list", name="Players", exact=True).get_by_role("listitem").filter(
            has=page.get_by_role("link", name=NAME, exact=True))
        expect(row).to_contain_text("Standing: Unbound")
        page.wait_for_timeout(200)
        assert sent == [], "the Players list read node records"

        player = open_player(page, NAME)
        expect(player).to_contain_text("Standing: Unbound")
        expect(_layer(page, "Host Management")).to_contain_text(
            "Last reported: Host Management last reported 4 s ago")
        assert any(url.endswith(f"/v1/operator/node/devices/{DEVICE_ID}") for url in sent)
        # Leaving the page stops its node read.
        go(page, "wall")
        count = len(sent)
        page.wait_for_timeout(300)
        assert len(sent) == count


def test_five_layers_and_a_silent_app_with_a_reporting_host_shows_both_ages(page, registry):
    box = Box(registry)
    report_readiness(registry, box.player_id)
    box.preparation()
    box.app_running()
    registry.clock.advance(120)
    box.host_sample()
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin)
        player = open_player(page, NAME)
        layers = page.get_by_role("region", name="Layers", exact=True).get_by_role("listitem")
        expect(layers).to_have_count(5)
        expect(_layer(page, "Host Management")).to_contain_text(
            "Host Management last reported 0 s ago")
        expect(_layer(page, "App Manager")).to_contain_text(
            "App Manager last reported 2 min ago · preparation verified")
        broker = _layer(page, "App Effect Broker")
        expect(broker).to_contain_text(
            "Last reported: Unknown: Central does not serve when this layer last reported")
        expect(broker).to_contain_text(
            "App process: App Effect Broker reported the app running · first received 2 min ago")
        expect(_layer(page, "Display Host")).to_contain_text(
            "Unknown: Central does not hold Display Host's current presentation")
        # The Player app's readiness is two minutes old while its host reports now.
        expect(_layer(page, "Player app")).to_contain_text("Player app last reported 2 min ago")
        expect(player).to_contain_text("Panel pixels: Unknown: no layer observes them")
        expect(page.get_by_role("region", name="Boot", exact=True)).to_contain_text(
            f"Current node session's boot: Boot {BOOT_ID} (claimed at boot by the box, unverified)")


def test_a_missing_field_reads_unknown_naming_it(page, registry):
    box = Box(registry)
    box.host_sample()

    def without_host_sample(route):
        data = route.fetch().json()
        for session in data["sessions"]:
            session["host_observation"] = None
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route(DEVICE_READ, without_host_sample)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin)
        open_player(page, NAME)
        expect(_layer(page, "Host Management")).to_contain_text(
            "Last reported: Unknown: host_observation.received_at not served")


def test_a_malformed_read_blanks_one_section_only(page, registry):
    box = Box(registry)
    box.host_sample()

    def drifted(route):
        data = route.fetch().json()
        data["boot_claims"] = 5  # payload drift: a list became a number
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route(DEVICE_READ, drifted)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin)
        player = open_player(page, NAME)
        expect(page.get_by_role("region", name="Boot", exact=True).get_by_role("alert")).to_have_text(
            "This section could not be shown. The rest of the page is current.")
        # The other sections, the page and the console stay up.
        expect(_layer(page, "Host Management")).to_contain_text("Host Management last reported")
        expect(player).to_contain_text("Standing: Unbound")
        expect(page.get_by_role("region", name="Outputs", exact=True)).to_contain_text("HDMI-A-1")
        go(page, "players")
        expect(page.get_by_role("link", name=NAME, exact=True)).to_be_visible()


def test_with_node_management_off_the_node_layers_read_unknown_and_the_page_works(page, registry):
    identity, _, request = enroll(registry, count=1)
    report_readiness(registry, identity["player_id"])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        player = open_player(page, f"Player {request.device_id}")
        for layer in ("Host Management", "App Manager", "App Effect Broker", "Display Host"):
            expect(_layer(page, layer)).to_contain_text(OFF)
        expect(_layer(page, "Player app")).to_contain_text("Player app last reported 0 s ago")
        expect(page.get_by_role("region", name="Reboot", exact=True)).to_contain_text(f"Reboot: {OFF}")
        expect(page.get_by_role("region", name="App", exact=True)).to_contain_text(f"App operations: {OFF}")
        expect(player.get_by_role("button", name=f"Retire player {identity['player_id']}", exact=True)
               ).to_be_visible()


def test_a_failed_device_read_keeps_its_rows_marked_refresh_failed(page, registry):
    box = Box(registry)
    box.host_sample()
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin, paused_at=registry.clock.utc())
        open_player(page, NAME)
        host = _layer(page, "Host Management")
        expect(host).to_contain_text("Host Management last reported 0 s ago")
        page.route(DEVICE_READ, lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error": "boom"}'))
        with page.expect_response(DEVICE_READ):
            drive_poll(page)
        layers = page.get_by_role("region", name="Layers", exact=True)
        expect(layers).to_contain_text("refresh failed")
        expect(host).to_contain_text("Host Management last reported 0 s ago")


def test_a_box_seen_only_at_boot_is_listed_not_enrolled(page, registry):
    box = Box(registry, enrolled=False)
    box.host_sample()
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        sent = _node_reads(page)
        connect(page, origin, "players")
        row = page.get_by_role("list", name="Players", exact=True).get_by_role("listitem").filter(
            has=page.get_by_role("link", name=NAME, exact=True))
        expect(row).to_contain_text("Standing: Not enrolled")
        page.wait_for_timeout(200)
        assert sent == []
        player = open_player(page, NAME)
        expect(_layer(page, "Host Management")).to_contain_text("Host Management last reported")
        expect(_layer(page, "Player app")).to_contain_text("Unknown: this box has not enrolled")
        expect(player).to_contain_text("Not enrolled: the Player app has reported no Outputs.")


def test_a_retired_player_page_reads_no_node_records(page, registry):
    box = Box(registry)
    box.host_sample()
    registry.retire(box.player_id)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        sent = _node_reads(page)
        connect(page, origin)
        visit_hash = f"#/players/{DEVICE_ID}"
        page.evaluate("(route) => { window.location.hash = route; }", visit_hash)
        player = page.locator("main > section:not([hidden])")
        expect(player).to_contain_text("Standing: Retired")
        expect(player).to_contain_text("Not read: Player retired")
        page.wait_for_timeout(300)
        assert sent == []


def test_the_equipment_bookmark_lands_on_players(page, registry):
    enroll(registry, count=1)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        page.goto(origin + "/console#/equipment")
        expect(page.get_by_role("heading", level=1, name="Players", exact=True)).to_be_visible()
        expect(page.get_by_role("list", name="Players", exact=True)).to_be_visible()
        assert current_hash(page) == "#/players"
        nav = page.get_by_role("navigation", name="Sections", exact=True)
        expect(nav.get_by_role("link", name="Equipment", exact=True)).to_have_count(0)


# --- B2: Reboot Player and the app operations (console DDD §10).

REBOOTS = f"**/v1/operator/node/devices/{DEVICE_ID}/reboots"
APP_READ = f"**/v1/operator/node/devices/{DEVICE_ID}/app-attempts"
RUN_STAYS = "The Run stays active. Central sends no command to other Frames or Actuators."


def _open_gate(registry):
    """Open the fleet effect gate, as a certified deployment does; returns its generation."""
    gate, _ = _gate(registry, _certificate(expires_in=300))
    return gate, gate.open(expected_revision=0).generation


def _gated_server(registry):
    return operator_server(registry.db, registry.clock, node_control=NODE,
                           node_serving_verifier=_LocalImageVerifier())


def _respond(box, command_id, decision):
    """Host Management answers a recorded reboot request (stored with no expiry check)."""
    with box.registry.db.transaction() as conn:
        payload = json.loads(bytes(conn.execute(
            "SELECT payload FROM node_reboot_commands WHERE command_id=%s", (command_id,)
        ).fetchone()["payload"]))
    claim, grant = box.claims["host_core"]
    response = NodeCommandResponseV2(grant.producer, command_id, payload["command_sha256"],
                                     grant.session_id, "operator_reboot", decision, "fixture")
    NodeIngest(box.sessions).ingest(grant.session_id, claim.credential, encode_node_message(response))


def _reboot_posts(page):
    """The body of every reboot request the page sends, in order."""
    sent = []
    page.on("request", lambda request: sent.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/reboots") else None)
    return sent


def _lose_first_response(page):
    """The first reboot request reaches Central (its effect is kept) but its answer is lost."""
    state = {"lost": False}

    def handle(route):
        if state["lost"]:
            route.fallback()
            return
        state["lost"] = True
        route.fetch()
        route.fulfill(status=502, body="")

    page.route(REBOOTS, handle)


def _reboot(page):
    return page.get_by_role("region", name="Reboot", exact=True)


def test_the_reboot_dialog_names_frames_and_a_recorded_reboot_is_requested(page, registry):
    box = Box(registry)
    registry.create_frame(FrameCreate(id="lobby", surface_id="wall", x_mm=100, y_mm=100, width_mm=400,
                                      height_mm=300, profile=FrameProfile(width_px=1920, height_px=1080,
                                                                          diagonal_inches=24)))
    registry.bind("lobby", box.player_id, "HDMI-A-1", expected_generation=0)
    _open_gate(registry)
    with _gated_server(registry) as origin:
        sent = _reboot_posts(page)
        connect(page, origin)
        open_player(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True)
        expect(dialog).to_contain_text("Frame lobby: no live Run")
        expect(dialog).to_contain_text(RUN_STAYS)
        expect(dialog).to_contain_text("Central offers the request to Host Management for 30 s.")
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(_reboot(page).get_by_role("status")).to_have_text(
            "Reboot recorded. Requested · delivery unknown.")
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        expect(history).to_contain_text(
            "Requested · delivery unknown · Central offers it to Host Management until")
        # While it is Requested, no new command id can be sent: only its retry is offered.
        expect(_reboot(page).get_by_role("button", name="Reboot Player", exact=True)).to_have_count(0)
        _reboot(page).get_by_role("button", name="Retry reboot request", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Retry the same request", exact=True).click()
        expect(page.get_by_role("dialog").get_by_role("status")).to_have_text(
            "Already recorded. Requested · delivery unknown.")
        assert len(sent) == 2 and sent[0] == sent[1]
        assert sent[0]["operator_audit_ref"].startswith("console/")
        assert sent[0]["valid_for_seconds"] == 30
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_reboot_commands").fetchone()["n"] == 1


def test_a_lost_answer_is_retried_with_the_identical_body_inside_the_window(page, registry):
    Box(registry)
    _open_gate(registry)
    with _gated_server(registry) as origin:
        sent = _reboot_posts(page)
        _lose_first_response(page)
        connect(page, origin)
        open_player(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Central did not answer. Check this after the next refresh.")
        dialog.get_by_role("button", name="Retry the same request", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("Already recorded. Requested · delivery unknown.")
        assert len(sent) == 2 and sent[0] == sent[1]


def test_a_retry_after_the_window_is_outcome_unknown_and_a_late_response_moves_it_on(page, registry):
    box = Box(registry)
    _open_gate(registry)
    with _gated_server(registry) as origin:
        sent = _reboot_posts(page)
        _lose_first_response(page)
        connect(page, origin)
        open_player(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog.get_by_role("button", name="Retry the same request", exact=True)).to_be_enabled()
        registry.clock.advance(31)
        dialog.get_by_role("button", name="Retry the same request", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Outcome unknown: Central stopped offering this request before it was confirmed.")
        expect(dialog.get_by_role("button", name="Retry the same request", exact=True)).to_have_count(0)
        assert len(sent) == 2 and sent[0] == sent[1]
        dialog.get_by_role("button", name="Close", exact=True).click()
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        expect(history).to_contain_text(
            "Outcome unknown: no response from Host Management; Central stopped offering it at")
        # After the window a NEW request may be made; it states the previous outcome and boot.
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        expect(page.get_by_role("dialog")).to_contain_text(
            "The previous request's outcome is unknown. Host Management's current session is the "
            "same boot that request targeted.")
        page.get_by_role("dialog").get_by_role("button", name="Cancel", exact=True).click()
        # A late response is stored and shown: Outcome unknown was never terminal.
        _respond(box, UUID(sent[0]["command_id"]), "accepted")
        expect(history).to_contain_text("Accepted by Host Management, not yet started", timeout=10_000)


def test_a_closed_gate_disables_reboot_with_its_reason(page, registry):
    Box(registry)
    with _gated_server(registry) as origin:
        sent = _reboot_posts(page)
        connect(page, origin)
        open_player(page, NAME)
        expect(_reboot(page).get_by_role("button", name="Reboot Player", exact=True)).to_be_disabled()
        expect(_reboot(page)).to_contain_text("Reboot unavailable: Central's effect gate is closed")
        assert sent == []


def test_rejected_reboots_and_app_operations_show_their_named_states(page, registry):
    box = Box(registry)
    gate, generation = _open_gate(registry)
    _, grant = box.claims["host_core"]
    command_id = uuid4()
    NodeCommands(box.sessions, gate).request_reboot(
        DEVICE_ID, OperatorReboot(command_id, grant.session_id, 1, "operator:fixture", generation))
    registry.clock.advance(2)
    _respond(box, command_id, "rejected")

    def rejected_stage(route):
        data = route.fetch().json()
        data["operations"] = [{
            "operation_id": str(uuid4()), "command_id": str(uuid4()), "operator_audit_ref": "fixture",
            "state": "staged", "command_response": {"decision": "rejected", "received_at": data["read_at"] - 3},
            "latest_effect": None, "physical_output": "unknown", "artifact_roots_retained": True}]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route(APP_READ, rejected_stage)
    with _gated_server(registry) as origin:
        connect(page, origin)
        open_player(page, NAME)
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        expect(history).to_contain_text("Rejected by Host Management")
        expect(history).to_contain_text('Host Management reported a "rejected" response · first received 0 s ago')
        # Rejected is a settled answer, so a new request may be made.
        expect(_reboot(page).get_by_role("button", name="Reboot Player", exact=True)).to_be_enabled()
        app = page.get_by_role("region", name="App", exact=True).get_by_role(
            "list", name="App operations", exact=True)
        expect(app).to_contain_text("Rejected by App Effect Broker")
        expect(app).not_to_contain_text("Staged")
        expect(page.get_by_role("main")).not_to_contain_text("ubsequent boot")


# --- B3: the V1 lane, labelled "V1 boot offers" (console DDD §9, Q2).

V1_TAG = "v1.2.3"
V1_DIGEST = "a" * 64


def _v1_box(registry):
    """A box Central knows from a V1 netboot, an app release V1 offers can carry, and the
    Registry Player enrolled on the same device; returns its Player id."""
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,%s,1,1)", (DEVICE_ID, SERIAL))
        conn.execute(
            "INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
            "discovered_at,updated_at,mirror_state,payload_url,payload_sha256,"
            "payload_size,payload_format,payload_base_abi,payload_source_manifest) "
            "VALUES(%s,1,2,3,FALSE,1,1,'mirrored','https://example.invalid/app',"
            "%s,123,'pw-player-data-v1',%s,'manifest.v2.json')",
            (V1_TAG, V1_DIGEST, "sha256:" + "b" * 64))
    return enroll(registry, count=1, device_id=DEVICE_ID)[0]["player_id"]


def test_the_v1_fleet_target_is_set_from_the_players_list(page, registry):
    _v1_box(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "players")
        v1 = page.get_by_role("region", name="V1 boot offers", exact=True)
        expect(v1).to_contain_text("V1 fleet app target: none")
        expect(v1.get_by_role("button", name="Queue online update")).to_have_count(0)
        v1.get_by_label("V1 fleet app").select_option(V1_TAG)
        v1.get_by_role("button", name="Set V1 fleet target", exact=True).click()
        expect(v1.get_by_role("status")).to_contain_text(f"V1 fleet app target set: {V1_TAG}.")
        expect(v1).to_contain_text(f"V1 fleet app target: {V1_TAG} · {V1_DIGEST[:12]}")
    policy = FleetService(registry.db, registry.clock).status()["fleet_policy"]
    assert policy["target"]["tag"] == V1_TAG


def test_a_queued_maintenance_request_is_shown_and_can_only_be_canceled(page, registry):
    _v1_box(registry)
    service = FleetService(registry.db, registry.clock)
    revision = service.set_app_policy(PolicyWrite(
        expected_revision=0, target=Artifact(tag=V1_TAG, sha256=V1_DIGEST, size=123)))["revision"]
    service.request_maintenance(DEVICE_ID, MaintenanceRequestWrite(
        request_id=uuid4(), expected_device_generation=1, expected_policy_source="explicit",
        expected_policy_revision=revision, expected_target_sha256=V1_DIGEST, ttl_seconds=3600))
    with operator_server(registry.db, registry.clock) as origin:
        posts = []
        page.on("request", lambda request: posts.append(request.url)
                if request.method == "POST" and "maintenance-requests" in request.url else None)
        connect(page, origin)
        open_player(page, NAME)
        v1 = page.get_by_role("region", name="V1 boot offers", exact=True)
        expect(v1).to_contain_text(f"V1 app target: {V1_TAG} · {V1_DIGEST[:12]} · explicit")
        expect(v1).to_contain_text(f"V1 maintenance request: queued · {V1_TAG}")
        expect(v1.get_by_role("group", name="V1 records", exact=True)).to_contain_text(
            "V1 record · Loader OS session:")
        expect(v1.get_by_role("button", name="Queue online update")).to_have_count(0)
        v1.get_by_role("button", name="Cancel maintenance request", exact=True).click()
        expect(v1.get_by_role("status")).to_have_text("V1 maintenance request canceled.")
        expect(v1).to_contain_text("V1 maintenance request: canceled")
        expect(v1.get_by_role("button", name="Cancel maintenance request")).to_have_count(0)
        assert posts == [], "the console created a maintenance request"
