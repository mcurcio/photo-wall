"""The Hardware list, a Pi's Hardware page and its Software and screens page against real node
records (console DDD §9-§10, beads B1 to B3; Console by Domain E1 U1).

The CI-gated fleet browser suite (R11): the production app on the loopback harness with node
management mounted (`operator_server(..., node_control=...)`), real node sessions, host
samples, manager preparation and broker evidence recorded through Central's own owner
services, and a real Registry Player enrolled on the same device. It proves the device-keyed
join, the node read (only on an open Player page), the facts' labels and their Unknowns, the
per-section error boundary and the retired `#/equipment` and `#/players` bookmarks (no aliases);
and (B2) Reboot Player against Central's real reboot owner behind an open effect gate: the
frozen request, its retry, its window, late responses and the app operations' named states,
and (R0) the one send rule: a dialog frozen at an older read sends nothing once the newest read
lists another outstanding request;
and (C2) Display Host's newest exchange per Output (the display read);
and (NS1, Part E) Stage app against Central's real stage owner: a Frame-bound Player stages
(D16), the dialog states this boot only and the bound rule, every served state renders through
Ended by a later boot, a switch in progress refuses a stale dialog with zero POSTs, a lost answer
resends the identical body, and refusals read in Central's words;
and (NS2, Part E) Qualified fallback: Begin names the linked app, this page samples every 2 s
only while visible, stops with zero further samples on a terminal, unlisted or
node_control_disabled answer and after 2 minutes without progress, and a qualification Central
accepts is listed and admits a following Stage as its fallback;
and (NV1, Part E) the V2 posture: a Central without node control shows one banner, one "not
shown" line and sends no node read; the Boot section holds node records only, plus the one
deprecated-path line Central serves; the effect gate comes from the shell's one status read.

Every assertion is behavioural (role, text, outcome); ages are Central's read time minus
Central's receipt time, driven by the registry's controlled clock.
"""

import json
import os
import re
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from console_tasks import connect, current_hash, go, hardware_list, open_pi, open_player
from operator_harness import (
    answer_first,
    drive_poll,
    operator_server,
    report_readiness,
    run_page_clock,
)
from playwright.sync_api import expect
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_fleet_rollout_gate import _certificate, _gate, _LocalImageVerifier
from test_node_acceptance import Witnesses
from test_node_boot import claim_for, cold_setup, environment, publish_deployment
from test_node_lifecycle import Rig
from test_registry import enroll

from central.fleet.node_boot import NodeBootService, parse_node_deployment
from central.fleet.node_commands import NodeCommands, OperatorReboot
from central.fleet.node_ingest import NodeIngest
from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import NodeControlConfig
from central.registry import FrameCreate
from contracts.models import FrameProfile
from contracts.node_boot import NodeBootRequestV2
from contracts.node_display import DisplayExchange, encode_display_exchange
from contracts.node_lifecycle import parse_stage_command
from contracts.node_observation import HostMetricV2, HostObservationV2, encode_host_observation
from contracts.node_preparation import ManagerPreparationV2, encode_manager_preparation
from contracts.node_protocol import (
    AppProcessFact,
    NodeCommandResponseV2,
    NodeProcessIdentity,
    NodeSnapshotV2,
    OutputKey,
    encode_node_message,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NODE = NodeControlConfig("node-test")
NAME = f"Player …{SERIAL[-6:]}"
DEVICE_READ = f"**/v1/operator/node/devices/{DEVICE_ID}"
BANNER = ("Node management is off on this Central. It was started without node control: Players "
          "that boot by node path are refused, and node records, Reboot, App operations and Releases "
          "have nothing to read. Start Central with central.node_app and set PHOTO_WALL_NODE_AUDIENCE "
          "(runbook › Node control).")
NOT_SHOWN = "Node records are not shown: node management is off (see the banner)."
STATUS_READ = "/v1/operator/node/status"
HOSTS_READ = "/v1/operator/node/hosts"


class Box:
    """One netbooted box with node sessions for Host Management, App Manager and the App
    Effect Broker on its current boot, each reporting through Central's owner services."""

    def __init__(self, registry, *, enrolled=True):
        self.registry = registry
        boots, self.sessions, _ = cold_setup(registry)
        offer = self.offer = boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
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

    def display_exchange(self, output_id="HDMI-A-1"):
        """One Display Host exchange on this boot, stored as Central's display owner stores it.
        Inserted directly: the read under test is the device read's, not the display owner's
        decision path (tests/test_node_display.py proves that)."""
        claim = claim_for(self.offer, owner="display_host")
        grant = self.sessions.enroll(claim)
        output = OutputKey(grant.producer.kernel_boot_id, grant.producer.incarnation_id, output_id, 1, 1)
        exchange = DisplayExchange(grant.producer, uuid4(), 1000, output, True)
        with self.registry.db.transaction() as conn:
            conn.execute("INSERT INTO node_display_exchanges(producer_id,request_id,session_id,output_id,"
                         "sampled_boottime_ms,request,response,decision_id,received_at) "
                         "SELECT producer_id,%s,session_id,%s,1000,%s,'{}',%s,%s FROM node_sessions "
                         "WHERE session_id=%s",
                         (exchange.request_id, output_id, encode_display_exchange(exchange), uuid4(),
                          self.registry.clock.utc(), grant.session_id))


def _node_reads(page):
    """Every node read the pages send, in order, except the shell's node status read and its
    fleet host read (HOSTS_READ, gated with it on node control)."""
    sent = []
    page.on("request", lambda request: sent.append(request.url)
            if "/v1/operator/node/" in request.url and STATUS_READ not in request.url
            and HOSTS_READ not in request.url else None)
    return sent


def _status_reads(page):
    """Every node status read the console sends (the shell's, and nobody else's)."""
    sent = []
    page.on("request", lambda request: sent.append(request.url) if STATUS_READ in request.url else None)
    return sent


def _layer(page, layer):
    """One software layer's rows on the Software and screens page."""
    return page.get_by_role("region", name="Layers", exact=True).get_by_role(
        "group", name=layer, exact=True)


def _host(page):
    """Host Management's session rows: Link and sessions, on the Pi's Hardware page."""
    return page.get_by_role("region", name="Link and sessions", exact=True).get_by_role(
        "group", name="Host Management", exact=True)


def _software(page, name=NAME):
    """From a Pi's Hardware page, follow the Pi header to its Software and screens page."""
    page.get_by_role("link", name="Software and screens", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Software and screens", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", level=2, name=name, exact=True)).to_be_visible()


def test_the_tracer_reaches_a_pi_page_from_the_list_with_standing_and_host_age(page, registry):
    box = Box(registry)
    box.host_sample()
    registry.clock.advance(4)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        sent = _node_reads(page)
        connect(page, origin, "hardware")
        row = hardware_list(page).get_by_role("table", name="Not driving a Frame", exact=True).get_by_role(
            "row").filter(has=page.get_by_role("link", name=NAME, exact=True))
        expect(row).to_contain_text("Standing: Unbound")
        page.wait_for_timeout(200)
        assert sent == [], "the Hardware list read node records"

        pi = open_pi(page, NAME)
        expect(pi).to_contain_text("Standing: Unbound")
        expect(_host(page)).to_contain_text(
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
    box.display_exchange()
    registry.clock.advance(120)
    box.host_sample()
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin)
        player = open_player(page, NAME)
        # Host Management (L0) lives on the Pi's Hardware page; the software layers stay here.
        layers = page.get_by_role("region", name="Layers", exact=True).get_by_role("listitem")
        expect(layers).to_have_count(4)
        expect(_layer(page, "Host Management")).to_have_count(0)
        expect(_layer(page, "App Manager")).to_contain_text(
            "App Manager last reported 2 min ago · preparation verified")
        broker = _layer(page, "App Effect Broker")
        expect(broker).to_contain_text(
            "Last reported: Unknown: App Effect Broker sends evidence only on change, and Central "
            "stores no receipt of its polls")
        expect(broker).to_contain_text(
            "App process: App Effect Broker reported the app running · first received 2 min ago")
        display = _layer(page, "Display Host")
        expect(display).to_contain_text("Last reported: Display Host last reported 2 min ago")
        expect(display).to_contain_text(
            "Output HDMI-A-1: Display Host last reported 2 min ago · Panel connector: connected")
        expect(display).to_contain_text("Display Host reported no app surface admitted")
        expect(display).to_contain_text("No compositor receipt for that surface in this report")
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
        open_pi(page, NAME)
        expect(_host(page)).to_contain_text(
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
        expect(_layer(page, "App Manager")).to_contain_text("Last reported")
        expect(player).to_contain_text("Standing: Unbound")
        expect(page.get_by_role("region", name="Outputs", exact=True)).to_contain_text("HDMI-A-1")
        go(page, "hardware")
        expect(hardware_list(page).get_by_role("link", name=NAME, exact=True)).to_be_visible()


def test_with_node_control_off_one_banner_one_line_no_node_reads_and_the_page_works(page, registry):
    identity, _, request = enroll(registry, count=2)
    player_id = identity["player_id"]
    registry.create_frame(FrameCreate(id="off-1", surface_id="wall", x_mm=100, y_mm=100, width_mm=400,
                                      height_mm=300, profile=FrameProfile(width_px=1920, height_px=1080,
                                                                          diagonal_inches=24)))
    page.route(f"**/v1/operator/players/{player_id}/outputs/HDMI-A-2/identify", lambda route: route.fulfill(
        status=202, content_type="application/json", body=json.dumps({
            "request_id": "synthetic-request", "output_id": "HDMI-A-2",
            "expires_at": registry.clock.utc() + 15})))
    name = f"Player {request.device_id}"
    with operator_server(registry.db, registry.clock, node_control=None) as origin:
        sent = _node_reads(page)
        host_reads = []
        page.on("request", lambda request: host_reads.append(request.url)
                if HOSTS_READ in request.url else None)
        connect(page, origin)
        banner = page.get_by_role("region", name="Node control", exact=True)
        expect(banner).to_have_text(BANNER)
        expect(banner).to_have_count(1)
        pi = open_pi(page, name)
        expect(banner).to_have_count(1)
        # In place of each page's node sections, exactly one line.
        expect(pi.get_by_text(NOT_SHOWN, exact=True)).to_have_count(1)
        for section in ("Health", "Link and sessions", "Reboot"):
            expect(page.get_by_role("region", name=section, exact=True)).to_have_count(0)
        expect(pi).not_to_contain_text("Unknown: node management")
        _software(page, name)
        player = page.locator("main > section:not([hidden])")
        expect(player.get_by_text(NOT_SHOWN, exact=True)).to_have_count(1)
        for section in ("Layers", "Boot", "App"):
            expect(page.get_by_role("region", name=section, exact=True)).to_have_count(0)
        expect(player).not_to_contain_text("Unknown: node management")
        # Identify, Bind, Unbind and Retire still work.
        outputs = page.get_by_role("list", name=f"Outputs of {name}", exact=True)
        outputs.get_by_role("button", name="Identify Panel HDMI-A-2", exact=True).click()
        expect(outputs.get_by_role("status")).to_contain_text("Identify requested for HDMI-A-2.")
        page.get_by_role("combobox", name=f"Frame for {player_id[-6:]} · HDMI-A-1 · Free", exact=True
                         ).select_option("off-1")
        outputs.get_by_role("button", name="Bind HDMI-A-1", exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Frame off-1", exact=True)).to_be_visible()
        assert registry.inventory().frames[0].player_id == player_id
        player = open_player(page, name)
        player.get_by_role("button", name=f"Unbind all outputs of {player_id}", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="Confirm unbind all", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("1 of 1 unbound")
        assert registry.inventory().frames[0].player_id is None
        dialog.get_by_role("button", name="Close", exact=True).click()
        # Retire lives on the Pi's Hardware page.
        pi = open_pi(page, name)
        pi.get_by_role("button", name=f"Retire player {player_id}", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label(f"Type {player_id[-6:]} to confirm", exact=True).fill(player_id[-6:])
        dialog.get_by_role("button", name="Confirm retire", exact=True).click()
        expect(pi).to_contain_text("Standing: Retired")
        page.wait_for_timeout(300)
        assert sent == [], "a node read was sent to a Central without node control"
        assert host_reads == [], "the shell read fleet hosts from a Central without node control"


def test_with_node_control_on_there_is_no_banner_and_the_player_page_reads_no_status_itself(
        page, registry):
    box = Box(registry)
    box.host_sample()
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        status = _status_reads(page)
        connect(page, origin)
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        expect(page.get_by_role("region", name="Node control", exact=True)).to_have_count(0)
        go(page, "hardware")
        page.wait_for_timeout(200)
        shell_reads = len(status)
        assert shell_reads >= 1, "the shell did not read node status"
        with page.expect_response(DEVICE_READ):
            open_pi(page, NAME)
        expect(_host(page)).to_contain_text("Host Management last reported")
        with page.expect_response(DEVICE_READ):
            _software(page)
        expect(_layer(page, "App Manager")).to_contain_text("Last reported")
        # A second device read (5 s cadence) sends no status read of the page's own.
        with page.expect_response(DEVICE_READ, timeout=10_000):
            pass
        assert len(status) == shell_reads, "the Player page read node status itself"
        expect(page.get_by_role("region", name="Node control", exact=True)).to_have_count(0)


def test_the_boot_section_holds_node_records_and_the_one_deprecated_path_line(page, registry):
    box = Box(registry)
    box.host_sample()
    deprecated = {"on": False}

    def stub(route):
        data = route.fetch().json()
        if deprecated["on"]:
            data["deprecated_boot"] = {"path": "offer", "recorded_at": data["read_at"] - 120}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route(DEVICE_READ, stub)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin)
        open_player(page, NAME)
        boot = page.get_by_role("region", name="Boot", exact=True)
        expect(boot).to_contain_text(
            f"Current node session's boot: Boot {BOOT_ID} (claimed at boot by the box, unverified)")
        expect(boot).to_contain_text(
            f"Node boot offer: Issued for boot {BOOT_ID}, not proof the Player booted")
        expect(boot).to_contain_text(
            "A Pi boots by node path when its kernel command line carries photowall.node=v2.")
        expect(boot).not_to_contain_text("deprecated")
        expect(boot.locator("p[data-truth]")).to_have_count(2)
        deprecated["on"] = True
        warning = boot.get_by_role("note")
        expect(warning).to_have_text(
            "Booted by the deprecated path: Central's newest boot record for this box is a deprecated "
            "boot offer · recorded 2 min ago · its kernel command line lacks photowall.node=v2; "
            "Select and Stage do not reach it", timeout=10_000)
        expect(warning).to_have_count(1)
        expect(boot).not_to_contain_text("A Pi boots by node path")
        expect(boot.locator("p[data-truth]")).to_have_count(3)


def test_a_failed_device_read_keeps_its_rows_marked_refresh_failed(page, registry):
    box = Box(registry)
    box.host_sample()
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin, paused_at=registry.clock.utc())
        open_pi(page, NAME)
        host = _host(page)
        expect(host).to_contain_text("Host Management last reported 0 s ago")
        page.route(DEVICE_READ, lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error": "boom"}'))
        with page.expect_response(DEVICE_READ):
            drive_poll(page)
        link = page.get_by_role("region", name="Link and sessions", exact=True)
        expect(link).to_contain_text("refresh failed")
        expect(host).to_contain_text("Host Management last reported 0 s ago")


def test_a_reboot_render_error_leaves_the_header_and_health_standing(page, registry):
    """Reboot, on the Pi's Hardware page, sits behind its own boundary."""
    box = Box(registry)
    box.host_sample()

    def drifted(route):
        data = route.fetch().json()
        data["reboot_commands"] = 5  # payload drift: only Reboot reads this list
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route(DEVICE_READ, drifted)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin)
        pi = open_pi(page, NAME)
        header = pi.locator("header")
        expect(pi.get_by_role("region", name="Reboot", exact=True).get_by_role("alert")).to_have_text(
            "This section could not be shown. The rest of the page is current.")
        expect(header.get_by_role("heading", level=2, name=NAME, exact=True)).to_be_visible()
        expect(header).to_contain_text("Standing: Unbound")
        expect(page.get_by_role("region", name="Health", exact=True)).to_contain_text(
            "Host Management: Host Management last reported")


def test_health_follows_reboot_from_the_fleet_host_read_and_link_keeps_only_the_receipt(page, registry):
    """The Pi's Hardware page: Reboot, Health (from Central's real fleet host read), then Link
    and sessions; Health's raw disclosure holds the sample's lines; Link and sessions keeps
    Host Management's "Last reported" and its session line."""
    box = Box(registry)
    box.host_sample()
    registry.clock.advance(2)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin)
        pi = open_pi(page, NAME)
        sections = pi.locator("section[aria-label]").evaluate_all("(all) => all.map((s) => s.ariaLabel)")
        assert sections == ["Reboot", "Health", "Link and sessions", "Danger zone"], sections
        health = page.get_by_role("region", name="Health", exact=True)
        expect(health).to_contain_text("Host Management: Host Management last reported 2 s ago")
        health.get_by_role("button", name="Every reported metric", exact=True).click()
        expect(health.get_by_role("list", name="Every reported metric", exact=True)).to_contain_text(
            "uptime: 1 seconds (host sampler)")
        host = _host(page)
        expect(host).to_contain_text("Last reported: Host Management last reported 2 s ago")
        host.get_by_role("button", name="Host Management details", exact=True).click()
        expect(host).to_contain_text("Session ")
        expect(host).not_to_contain_text("uptime")
        expect(host).not_to_contain_text("visible pixels")


def test_a_box_seen_only_at_boot_is_listed_not_enrolled(page, registry):
    box = Box(registry, enrolled=False)
    box.host_sample()
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        sent = _node_reads(page)
        connect(page, origin, "hardware")
        row = hardware_list(page).get_by_role("table", name="Not driving a Frame", exact=True).get_by_role(
            "row").filter(has=page.get_by_role("link", name=NAME, exact=True))
        expect(row).to_contain_text("Standing: Not enrolled")
        page.wait_for_timeout(200)
        assert sent == []
        open_pi(page, NAME)
        expect(_host(page)).to_contain_text("Host Management last reported")
        _software(page)
        player = page.locator("main > section:not([hidden])")
        expect(_layer(page, "Player app")).to_contain_text("Unknown: this box has not enrolled")
        expect(player).to_contain_text("Not enrolled: the Player app has reported no Outputs.")


def test_a_retired_player_page_reads_no_node_records(page, registry):
    box = Box(registry)
    box.host_sample()
    registry.retire(box.player_id)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        sent = _node_reads(page)
        connect(page, origin)
        for visit_hash in (f"#/hardware/{DEVICE_ID}", f"#/players/{DEVICE_ID}"):
            page.evaluate("(route) => { window.location.hash = route; }", visit_hash)
            player = page.locator("main > section:not([hidden])")
            expect(player).to_contain_text("Standing: Retired")
            expect(player).to_contain_text("Not read: Player retired")
            expect(player.get_by_role("button", name=re.compile("^Retire player"))).to_have_count(0)
        page.wait_for_timeout(300)
        assert sent == []


def test_the_retired_list_bookmarks_land_on_the_wall(page, registry):
    # No aliases (owner rule): the Equipment and Players lists' bookmarks are unknown routes,
    # which land where every unknown route does; the sidebar's Fleet group is Hardware.
    enroll(registry, count=1)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        for bookmark in ("#/equipment", "#/players"):
            page.goto(origin + "/console" + bookmark)
            expect(page.get_by_role("heading", level=1, name="Wall", exact=True)).to_be_visible()
            assert current_hash(page) == "#/wall"
        nav = page.get_by_role("navigation", name="Sections", exact=True)
        for gone in ("Equipment", "Players", "Software and screens"):
            expect(nav.get_by_role("link", name=gone, exact=True)).to_have_count(0)
        expect(nav.get_by_role("list", name="Fleet", exact=True).get_by_role("link")).to_have_text(
            ["Hardware", "Releases"])


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


def _respond(box, command_id, decision, reason="reboot_scope_or_expiry"):
    """Host Management answers a recorded reboot request (stored with no expiry check)."""
    with box.registry.db.transaction() as conn:
        payload = json.loads(bytes(conn.execute(
            "SELECT payload FROM node_reboot_commands WHERE command_id=%s", (command_id,)
        ).fetchone()["payload"]))
    claim, grant = box.claims["host_core"]
    response = NodeCommandResponseV2(grant.producer, command_id, payload["command_sha256"],
                                     grant.session_id, "operator_reboot", decision, reason)
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


class _HeldDeviceRead:
    """Serve the device read as last fetched once frozen: a read that has not arrived yet.

    `freeze()` returns only once the page holds a read fetched before it and no fetch is in
    flight, so a read already on its way to Central cannot arrive after the test moves
    Central on (its clock, another page's commit) and overtake the frozen one.
    """

    def __init__(self, page):
        self.page, self.on, self.body, self.in_flight, self.served = page, False, None, 0, 0
        page.route(DEVICE_READ, self._handle)

    def _handle(self, route):
        if self.on and self.body is not None:
            self.served += 1
            route.fulfill(status=200, content_type="application/json", body=self.body)
            return
        self.in_flight += 1
        try:
            response = route.fetch()
            self.body = response.text()
            route.fulfill(response=response)
        finally:
            self.in_flight -= 1

    def freeze(self, timeout_ms=15_000):
        self.on, self.served = True, 0
        waited = 0
        while self.in_flight or not self.served:
            assert waited < timeout_ms, "no held device read was served"
            self.page.wait_for_timeout(50)
            waited += 50

    def release(self):
        self.on = False


def _call_send_directly(dialog_button):
    """Run the dialog's own send handler even though its button is disabled.

    A click on a disabled button runs nothing, so this calls React's onClick from the
    element's props: the send path is then `sendReboot` alone, whose call-time check on the
    hook's newest read is what is under test.
    """
    dialog_button.evaluate("""(button) => {
        const key = Object.keys(button).find((name) => name.startsWith("__reactProps$"));
        button[key].onClick();
    }""")


def _reboot(page):
    """Reboot, on the Pi's Hardware page (Console by Domain § Fleet)."""
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
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True)
        expect(dialog).to_contain_text("Frame lobby: no live Run")
        expect(dialog).to_contain_text(RUN_STAYS)
        expect(dialog).to_contain_text("Central offers the request to Host Management for 30 s.")
        # NR2: the one static sentence on what the next boot is offered, linking to Releases.
        expect(dialog).to_contain_text("On its next boot, Central offers this Player the boot selection current "
                                       "at that moment (see Releases).")
        expect(dialog.get_by_role("link", name="Releases", exact=True)).to_have_attribute("href", "#/releases")
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(_reboot(page).get_by_role("status")).to_have_text(
            "Reboot recorded. Requested · delivery unknown.")
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        expect(history).to_contain_text(
            "Requested · delivery unknown · Central offers it to Host Management until")
        # While it is Requested, no new command id can be sent: only its retry is offered.
        expect(_reboot(page).get_by_role("button", name="Reboot Player", exact=True)).to_have_count(0)
        _reboot(page).get_by_role("button", name="Send the reboot request again", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Send the same request again", exact=True).click()
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
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Central did not answer. Check this after the next refresh.")
        dialog.get_by_role("button", name="Send the same request again", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("Already recorded. Requested · delivery unknown.")
        assert len(sent) == 2 and sent[0] == sent[1]


def test_a_double_click_on_reboot_player_sends_one_post(page, registry):
    """R1: the dialog's send, double-clicked, sends one reboot request (the dialog's hold and the
    per-device guard inside `sendReboot`, tests/test_console_fleet_commands.py)."""
    Box(registry)
    _open_gate(registry)
    with _gated_server(registry) as origin:
        sent = _reboot_posts(page)
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True).get_by_role(
            "button", name="Reboot Player", exact=True).dblclick()
        expect(_reboot(page).get_by_role("status")).to_have_text("Reboot recorded. Requested · delivery unknown.")
        page.wait_for_timeout(500)
        assert len(sent) == 1
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_reboot_commands").fetchone()["n"] == 1


def test_a_retry_after_the_window_is_outcome_unknown_and_a_late_response_moves_it_on(page, registry):
    box = Box(registry)
    _open_gate(registry)
    with _gated_server(registry) as origin:
        sent = _reboot_posts(page)
        _lose_first_response(page)
        held = _HeldDeviceRead(page)
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog.get_by_role("button", name="Send the same request again", exact=True)).to_be_enabled()
        # The window ends before the next read arrives: Central, not the page, answers the retry.
        held.freeze()
        registry.clock.advance(31)
        dialog.get_by_role("button", name="Send the same request again", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Outcome unknown: Central stopped offering this request before it was confirmed.")
        held.release()
        expect(dialog.get_by_role("button", name="Send the same request again", exact=True)).to_have_count(0)
        assert len(sent) == 2 and sent[0] == sent[1]
        dialog.get_by_role("button", name="Close", exact=True).click()
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        # The first read after the release arrives within one 5 s poll interval.
        expect(history).to_contain_text(
            "Outcome unknown: no response from Host Management; Central stopped offering it at",
            timeout=10_000)
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
        open_pi(page, NAME)
        expect(_reboot(page).get_by_role("button", name="Reboot Player", exact=True)).to_be_disabled()
        expect(_reboot(page)).to_contain_text(
            "Reboot unavailable: Effect gate closed · Central's reason: no deployment certification has opened it")
        # NR2: the gate reason links to the gate's home, which shows the same words.
        _reboot(page).get_by_role("link", name="See Releases › Effect gate", exact=True).click()
        expect(page.get_by_role("region", name="Effect gate", exact=True)).to_contain_text(
            "Effect gate: Effect gate closed · Central's reason: no deployment certification has opened it")
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
            "latest_effect": None, "physical_output": "unknown", "artifact_roots_retained": True}, {
            "operation_id": str(uuid4()), "command_id": str(uuid4()), "operator_audit_ref": "fixture",
            "state": "superseded", "command_response": {"decision": "rejected", "received_at": data["read_at"] - 3},
            "latest_effect": None, "physical_output": "unknown", "artifact_roots_retained": True}]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route(APP_READ, rejected_stage)
    with _gated_server(registry) as origin:
        connect(page, origin)
        open_pi(page, NAME)
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        expect(history).to_contain_text("Rejected by Host Management")
        expect(history).to_contain_text(
            'Host Management reported a "rejected" response (reboot scope or expiry) · first received 0 s ago')
        # Rejected is a settled answer, so a new request may be made.
        expect(_reboot(page).get_by_role("button", name="Reboot Player", exact=True)).to_be_enabled()
        _software(page)
        app = page.get_by_role("region", name="App", exact=True).get_by_role(
            "list", name="App operations", exact=True)
        expect(app).to_contain_text("Rejected by App Effect Broker")
        expect(app).not_to_contain_text("Staged")
        # A superseded stage the broker had rejected still shows the rejection.
        superseded = app.get_by_role("listitem").filter(has_text="Replaced by a later stage")
        expect(superseded).to_contain_text('App Effect Broker reported a "rejected" response')
        expect(page.get_by_role("main")).not_to_contain_text("ubsequent boot")


def test_a_dialog_frozen_at_an_older_read_sends_nothing_once_another_request_is_outstanding(
        page, registry):
    """R0's stale dialog: the dialog freezes its request at one read; another page then records
    a different reboot request, which the next read lists as outstanding. Send is disabled, and
    the dialog's own send handler, called directly, refuses inside `sendReboot`: zero POSTs."""
    box = Box(registry)
    gate, generation = _open_gate(registry)
    with _gated_server(registry) as origin:
        sent = _reboot_posts(page)
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True)
        send = dialog.get_by_role("button", name="Reboot Player", exact=True)
        expect(send).to_be_enabled()
        # Another tab's request, recorded by Central's real reboot owner after the dialog froze.
        _, grant = box.claims["host_core"]
        NodeCommands(box.sessions, gate).request_reboot(
            DEVICE_ID, OperatorReboot(uuid4(), grant.session_id, 1, "operator:other-tab", generation))
        registry.clock.advance(5)
        expect(dialog.get_by_role("alert")).to_have_text(
            "Another reboot request for this Player is outstanding; close this dialog and review it.", timeout=10_000)
        expect(send).to_be_disabled()
        _call_send_directly(send)
        expect(dialog.get_by_role("status")).to_have_text(
            "Another reboot request for this Player is outstanding; close this dialog and review it.")
        assert sent == []
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_reboot_commands").fetchone()["n"] == 1


def test_centrals_outstanding_fence_reads_as_changed(page, registry):
    """A 409 node_reboot_outstanding (another page's request committed after this page's newest
    read) reads as changed, never as a refusal of this Player."""
    box = Box(registry)
    gate, generation = _open_gate(registry)
    with _gated_server(registry) as origin:
        sent = _reboot_posts(page)
        held = _HeldDeviceRead(page)
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True)
        held.freeze()  # the other page's commit lands after this page's newest read
        _, grant = box.claims["host_core"]
        NodeCommands(box.sessions, gate).request_reboot(
            DEVICE_ID, OperatorReboot(uuid4(), grant.session_id, 1, "operator:other-tab", generation))
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Another reboot request for this Player is outstanding; close this dialog and review it.")
        assert len(sent) == 1
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_reboot_commands").fetchone()["n"] == 1


# --- NS1 (Part E): Stage app against Central's real stage owner behind an open effect gate.

STAGES = f"**/v1/operator/node/devices/{DEVICE_ID}/app-stages"
BOUND_RULE = ("Each Frame this Player drives shows the base page while the app switches, then rejoins its Run "
              "at the current point (missed content is not replayed), as on Reboot.")
BOUND_PROVEN = "A switch on a Frame-bound Player is proven on Central only; the Player's side of it is not yet qualified."
THIS_BOOT = "Applies to this boot only. Any later boot, including an unplanned one, is offered the boot selection"
RECORDED = "Stage recorded; App Effect Broker has not responded yet."


def _stage_posts(page):
    """The body of every stage request the page sends, in order."""
    sent = []
    page.on("request", lambda request: sent.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/app-stages") else None)
    return sent


def _app(page):
    return page.get_by_role("region", name="App", exact=True)


def _stage_dialog(page):
    _app(page).get_by_role("button", name="Stage app…", exact=True).click()
    return page.get_by_role("dialog", name=f"Stage an app on {NAME}?", exact=True)


def _choose(dialog, deployment):
    dialog.get_by_role("radio", name=re.compile(f"^Deployment {str(deployment.deployment_id)[:4]}…")).check()


def test_a_bound_player_stages_with_the_bound_rule_and_renders_every_served_state(page, registry):
    """D16 (G6): a Frame-bound Player is staged; the dialog states this boot only and the bound
    rule; a stranded Staged offers a newer stage saying what it replaces; the operation then reads
    switching (Stage disabled), the target running, and ended by a later boot (G2)."""
    fixture = Rig(registry, unbound=False, gate_seconds=300)
    with _gated_server(registry) as origin:
        sent = _stage_posts(page)
        connect(page, origin)
        open_player(page, NAME)
        dialog = _stage_dialog(page)
        expect(dialog).to_contain_text(THIS_BOOT)
        expect(dialog).to_contain_text(BOUND_RULE)
        expect(dialog).to_contain_text(BOUND_PROVEN)  # the node half is unqualified, as on the journey
        expect(dialog).to_contain_text("Frames this Player drives: node-f0, node-f1.")
        for deployment in fixture.deployments:  # each published deployment carrying an app is offered
            expect(dialog.get_by_role("radio", name=re.compile(
                f"^Deployment {str(deployment.deployment_id)[:4]}…"))).to_have_count(1)
        _choose(dialog, fixture.deployments[0])
        dialog.get_by_role("button", name="Stage app", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(_app(page).get_by_role("status")).to_have_text(RECORDED)
        operations = _app(page).get_by_role("list", name="App operations", exact=True)
        expect(operations).to_contain_text("Staged; no response from App Effect Broker")
        # A stranded stage: a newer stage is offered and says what it replaces, never "retry".
        dialog = _stage_dialog(page)
        expect(dialog).to_contain_text("Sends a newer stage. It replaces stage")
        expect(dialog).to_contain_text("which App Effect Broker has not responded to.")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        with registry.db.transaction() as conn:
            [row] = conn.execute("SELECT command_payload FROM node_app_operations").fetchall()
        command = parse_stage_command(bytes(row["command_payload"]))
        assert command.target == fixture.deployments[0].app_environment
        fixture.report(command, "intent_stop", 1)
        expect(operations).to_contain_text("App Effect Broker reported switching (intent stop)", timeout=10_000)
        stage_button = _app(page).get_by_role("button", name="Stage app…", exact=True)
        expect(stage_button).to_be_disabled()
        expect(_app(page)).to_contain_text("Stage app unavailable: A switch is in progress; wait for it to finish.")
        for sequence, phase in enumerate(("stopped", "starting_new", "running"), start=2):
            fixture.report(command, phase, sequence)
        expect(operations).to_contain_text("App Effect Broker reported the staged app running", timeout=10_000)
        expect(stage_button).to_be_enabled()
        offer = NodeBootService(fixture.sessions).offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64))
        fixture.sessions.enroll(claim_for(offer, owner="app_effect_broker"))
        expect(operations).to_contain_text("Ended by a later boot", timeout=10_000)
        expect(operations).to_contain_text("a later boot was admitted; Central offers each boot the boot selection")
        assert len(sent) == 1
        assert sent[0]["deployment_id"] == str(fixture.deployments[0].deployment_id)
        assert sent[0]["rollout_generation"] == fixture.generation
        assert sent[0]["operator_audit_ref"].startswith("console/")


def test_a_closed_gate_disables_stage_with_its_reason_and_a_link_to_the_gate(page, registry):
    fixture = Rig(registry, gate_seconds=300)
    fixture.service.gate.close()
    with _gated_server(registry) as origin:
        sent = _stage_posts(page)
        connect(page, origin)
        open_player(page, NAME)
        expect(_app(page).get_by_role("button", name="Stage app…", exact=True)).to_be_disabled()
        expect(_app(page)).to_contain_text("Stage app unavailable: Effect gate closed · Central's reason:")
        _app(page).get_by_role("link", name="See Releases › Effect gate", exact=True).click()
        expect(page.get_by_role("region", name="Effect gate", exact=True)).to_contain_text("Effect gate closed")
        assert sent == []


def test_a_stale_stage_dialog_sends_nothing_once_a_switch_is_in_progress(page, registry):
    """The dialog froze while nothing was switching; another tab's stage then starts switching. Send
    is disabled, and the dialog's own send handler, called directly, refuses inside sendStage."""
    fixture = Rig(registry, gate_seconds=300)
    with _gated_server(registry) as origin:
        sent = _stage_posts(page)
        connect(page, origin)
        open_player(page, NAME)
        dialog = _stage_dialog(page)
        _choose(dialog, fixture.deployments[0])
        send = dialog.get_by_role("button", name="Stage app", exact=True)
        expect(send).to_be_enabled()
        other = fixture.stage(1)  # another tab, through Central's real stage owner
        fixture.report(other, "intent_stop", 1)
        registry.clock.advance(1)
        expect(dialog.get_by_role("alert")).to_have_text(
            "A switch is in progress; wait for it to finish.", timeout=10_000)
        expect(send).to_be_disabled()
        _call_send_directly(send)
        expect(dialog.get_by_role("status")).to_have_text("A switch is in progress; wait for it to finish.")
        assert sent == []
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_app_operations").fetchone()["n"] == 1


def test_a_lost_stage_answer_resends_the_identical_body_and_a_recorded_one_is_not_resent(page, registry):
    """A stage whose request failed at a gateway is held unknown and re-sent byte-identical; one
    Central did record is settled by the next read, which lists it, and is never re-sent."""
    # Name a deployment whose app is not the qualified one: the release read lists deployments
    # published at one instant in no fixed order, so "the first radio" may be the running app.
    fixture = Rig(registry, gate_seconds=300)
    with _gated_server(registry) as origin:
        sent = _stage_posts(page)
        answer_first(page, STAGES, lambda route: route.fulfill(status=502, body=""))  # never reaches Central
        connect(page, origin)
        open_player(page, NAME)
        dialog = _stage_dialog(page)
        _choose(dialog, fixture.deployments[0])
        dialog.get_by_role("button", name="Stage app", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("Central did not answer. Check this after the next refresh.")
        expect(dialog.get_by_role("radio", name=re.compile(
            f"^Deployment {str(fixture.deployments[0].deployment_id)[:4]}…"))).to_be_disabled()  # the choice is locked once sent
        dialog.get_by_role("button", name="Send the same request again", exact=True).click()
        expect(dialog).to_have_count(0, timeout=10_000)
        expect(_app(page).get_by_role("status")).to_have_text(RECORDED)
        assert len(sent) == 2 and sent[0] == sent[1]

        # Recorded by Central, answer lost: the next read lists it, so nothing is re-sent.
        def lose(route):
            route.fetch()
            route.fulfill(status=502, body="")

        page.unroute(STAGES)
        answer_first(page, STAGES, lose)
        dialog = _stage_dialog(page)
        expect(dialog).to_contain_text("Sends a newer stage. It replaces stage")
        _choose(dialog, fixture.deployments[0])
        dialog.get_by_role("button", name="Stage app", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("Central did not answer. Check this after the next refresh.")
        expect(dialog.get_by_role("alert")).to_have_text("Central already recorded this stage.", timeout=10_000)
        expect(dialog.get_by_role("button", name="Send the same request again", exact=True)).to_be_disabled()
        assert len(sent) == 3
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_app_operations").fetchone()["n"] == 2


def test_stage_refusals_read_in_centrals_words_and_unlisted_codes_are_refused(page, registry):
    Rig(registry, gate_seconds=300)
    answers = iter([(503, "rollout_serving_verifier_unavailable"), (422, "node_app_stage_invalid"),
                    (409, "node_app_qualified_fallback_required")])

    def refuse(route):
        status, code = next(answers)
        route.fulfill(status=status, content_type="application/json", body=json.dumps({"error": code}))

    with _gated_server(registry) as origin:
        sent = _stage_posts(page)
        page.route(STAGES, refuse)
        connect(page, origin)
        open_player(page, NAME)
        for words in ("Central refused the effect: rollout_serving_verifier_unavailable.",
                      "Central refused: node_app_stage_invalid.",
                      "No qualified fallback for this Player's current Outputs and base: qualify the running app first."):
            dialog = _stage_dialog(page)
            dialog.get_by_role("radio").first.check()
            dialog.get_by_role("button", name="Stage app", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text(words)
            # A refusal is final: no resend offered, nothing re-sent.
            expect(dialog.get_by_role("button", name="Send the same request again", exact=True)).to_have_count(0)
            dialog.get_by_role("button", name="Close", exact=True).click()
        assert len(sent) == 3


# --- NS2 (Part E): Qualified fallback; this page samples (Q7) against Central's real owner.

SAMPLES = "**/v1/operator/node/app-qualifications/*/sample"
STEADY = "Begin again when the Player app and its Outputs are steady."
AWAITING = {"status": "awaiting_new_witnesses", "accepted": False}


class _Samples:
    """Answer every sample request with `answer` (a (status, body) pair), counting them; `real`
    lets them through to Central instead."""

    def __init__(self, page, answer=(200, AWAITING)):
        self.page, self.answer, self.real, self.sent, self.answered = page, answer, False, 0, 0
        page.route(SAMPLES, self._handle)

    def _handle(self, route):
        self.sent += 1
        if self.real:
            route.fallback()
        else:
            status, body = self.answer
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        self.answered += 1

    def wait_for(self, count, timeout_ms=10_000):
        waited = 0
        while self.answered < count:
            assert waited < timeout_ms, f"{self.answered} sample answers, expected {count}"
            self.page.wait_for_timeout(20)
            waited += 20
        self.page.wait_for_timeout(50)  # let the page apply the answer


def _set_visibility(page, state):
    page.evaluate("""(state) => {
        Object.defineProperty(document, "visibilityState", {configurable: true, get: () => state});
        document.dispatchEvent(new Event("visibilitychange"));
    }""", state)


def _qualified_fallback(page):
    return _app(page).get_by_role("region", name="Qualified fallback", exact=True)


def _begin(page, environment_sha256):
    section = _qualified_fallback(page)
    section.get_by_role("button", name=f"Begin qualifying app {environment_sha256[:6]}…", exact=True).click()
    return section


def _begun(registry):
    """The qualification ids the console began (Central's rows, by the console's audit reference)."""
    with registry.db.transaction() as conn:
        return [row["qualification_id"] for row in conn.execute(
            "SELECT qualification_id FROM node_app_qualifications WHERE operator_audit_ref LIKE 'console/%'")]


def test_begin_names_the_linked_app_and_samples_every_two_seconds_only_while_visible(page, registry):
    fixture = Rig(registry, unbound=False)
    linked = fixture.proof.challenge.environment_sha256
    with _gated_server(registry) as origin:
        samples = _Samples(page)
        connect(page, origin, paused_at=registry.clock.utc())
        open_player(page, NAME)
        section = _qualified_fallback(page)
        expect(section).to_contain_text(f"App Effect Broker reported the linked Player app {linked[:6]}…")
        expect(section).to_contain_text("physical pixels unknown")  # the Rig's stored acceptance
        _begin(page, linked)
        samples.wait_for(1)  # the first sample at once
        assert len(_begun(registry)) == 1
        progress = section.get_by_role("status", name="Qualification progress")
        expect(progress).to_have_text("Waiting for new reports from the Player app and Display Host")
        for expected in (2, 3, 4):
            page.clock.run_for(2000)
            samples.wait_for(expected)
        _set_visibility(page, "hidden")
        page.clock.run_for(20_000)
        page.wait_for_timeout(300)
        assert samples.sent == 4, "a hidden tab sampled"
        _set_visibility(page, "visible")
        samples.wait_for(5)  # sampling resumes at once on return
        page.clock.run_for(2000)
        samples.wait_for(6)
        expect(section.get_by_role("button", name=re.compile("^Begin qualifying"))).to_be_disabled()


@pytest.mark.parametrize("status, code, words", [
    (409, "node_qualification_process_changed", "Stopped: the Player app's linked process changed or unlinked."),
    (409, "node_something_new", "Stopped: Central refused: node_something_new."),
    (503, "node_control_disabled", "Stopped: Central refused: node_control_disabled."),
])
def test_a_terminal_or_unlisted_sample_answer_stops_sampling_with_zero_further_posts(
        page, registry, status, code, words):
    fixture = Rig(registry, unbound=False)
    with _gated_server(registry) as origin:
        samples = _Samples(page, (status, {"error": code}))
        connect(page, origin, paused_at=registry.clock.utc())
        open_player(page, NAME)
        section = _begin(page, fixture.proof.challenge.environment_sha256)
        samples.wait_for(1)
        expect(section.get_by_role("status", name="Qualification progress")).to_have_text(f"{words} {STEADY}")
        run_page_clock(page, 30_000)
        _set_visibility(page, "hidden")
        _set_visibility(page, "visible")
        page.wait_for_timeout(300)
        assert samples.sent == 1
        # Begin is offered again, as a new attempt.
        expect(section.get_by_role("button", name=re.compile("^Begin qualifying app"))).to_be_enabled()


def test_two_minutes_of_waiting_answers_stop_sampling_with_zero_further_posts(page, registry):
    fixture = Rig(registry, unbound=False)
    with _gated_server(registry) as origin:
        samples = _Samples(page)
        connect(page, origin, paused_at=registry.clock.utc())
        open_player(page, NAME)
        section = _begin(page, fixture.proof.challenge.environment_sha256)
        samples.wait_for(1)
        progress = section.get_by_role("status", name="Qualification progress")
        for expected in range(2, 62):  # the first answer is at 0 s, the 61st at 120 s
            page.clock.run_for(2000)
            samples.wait_for(expected)
        expect(progress).to_have_text("Stopped: no progress for 2 minutes. Last answer: Waiting for new reports "
                                      "from the Player app and Display Host.")
        run_page_clock(page, 30_000)
        page.wait_for_timeout(300)
        assert samples.sent == 61


def test_an_accepted_qualification_is_listed_and_a_following_stage_is_admitted_against_it(page, registry):
    """The page begins and samples; Central's real owners supply advancing witnesses and Central
    accepts on the 30 s window. The page's next real sample reads accepted, the acceptance is
    listed, and a Stage of another app is admitted with it as the fallback."""
    witness = Witnesses(registry)
    linked = witness.proof.challenge.environment_sha256
    with registry.db.transaction() as conn:
        base = parse_node_deployment(bytes(conn.execute("SELECT document FROM node_deployments").fetchone()["document"]))
    target = environment("b")
    selected = replace(base, deployment_id=uuid4(), app_environment=target, environment_sources={
        base.manager_primary.environment_sha256: base.environment_sources[base.manager_primary.environment_sha256],
        target.environment_sha256: "https://example.invalid/target"})
    publish_deployment(registry.db, selected, registry.clock)
    _open_gate(registry)
    with _gated_server(registry) as origin:
        samples = _Samples(page)
        sent = _stage_posts(page)
        connect(page, origin)
        open_player(page, NAME)
        section = _begin(page, linked)
        samples.wait_for(1)
        [qualification] = _begun(registry)
        witness.qualification = qualification
        for _ in range(7):
            result = witness.advance()
        assert result["status"] == "accepted"
        samples.real = True  # the page's next sample reaches Central
        expect(section.get_by_role("status", name="Qualification progress")).to_have_text(
            "Qualification: Qualified · physical pixels unknown", timeout=10_000)
        acceptances = section.get_by_role("list", name="Qualified fallbacks", exact=True)
        expect(acceptances.get_by_role("listitem")).to_have_count(1, timeout=10_000)
        expect(acceptances).to_contain_text(f"Qualified on this Player: app {linked[:6]}… on base")
        settled = samples.sent
        page.wait_for_timeout(2500)
        assert samples.sent == settled, "the page sampled after Central accepted"

        dialog = _stage_dialog(page)
        _choose(dialog, selected)
        dialog.get_by_role("button", name="Stage app", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(_app(page).get_by_text(RECORDED, exact=True)).to_be_visible()
        assert len(sent) == 1
    with registry.db.transaction() as conn:
        [row] = conn.execute("SELECT command_payload FROM node_app_operations").fetchall()
    assert parse_stage_command(bytes(row["command_payload"])).fallback.environment_sha256 == linked
