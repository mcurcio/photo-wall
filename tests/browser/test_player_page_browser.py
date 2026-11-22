"""The Hardware list, a Pi's Hardware page and its Software and screens page against real node
records (console DDD §9-§10, beads B1 to B3; Console by Domain E1 U1).

The CI-gated fleet browser suite (R11): the production app on the loopback harness with node
management mounted (`operator_server`), the box's boot offer through
Central's real boot service, its host line recorded through Central's real link store (decision
0034) and a real Registry Player enrolled on the same device. It proves the device-keyed join,
the node read (only on an open Pi page), the facts' labels and their Unknowns, the per-section
error boundary and the retired `#/equipment` and `#/players` bookmarks (no aliases); and (B2)
Reboot Player against Central's real reboot owner: the frozen request, its retry, its window and
late responses, and (R0) the one send rule: a dialog frozen at an older read sends nothing once
the newest read lists another outstanding request. The app operations (Stage app, the Qualified
fallback) and the old path's device facts (sessions, App Manager, the App Effect Broker) are gone
(S4c-2a-iii), and so is Display Host's exchange per Output (S4c-2a-iv).

Every assertion is behavioural (role, text, outcome).
"""

import json
import os
import re
from uuid import uuid4

import pytest
from console_tasks import connect, current_hash, go, hardware_list, open_pi, open_player
from operator_harness import (
    drive_poll,
    operator_server,
    report_readiness,
)
from playwright.sync_api import expect
from support.frames import frame_create
from support.pi_reboots import ask_reboot, reboot_queue, run_queued
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_node_boot import cold_setup
from test_node_fleet_hosts import on_line
from test_registry import enroll

from contracts.node_boot import NodeBootRequestV2
from contracts.node_observation import HostMetricsState, HostMetricV2, encode_host_metrics_state
from contracts.node_state import BIRTH_KEY, HOST_METRICS_KEY

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NAME = f"Player …{SERIAL[-6:]}"
DEVICE_READ = f"**/v1/operator/node/devices/{DEVICE_ID}"
HOSTS_READ = "/v1/operator/node/hosts"


class Box:
    """One netbooted box on its current boot: Central's boot offer for it (the Boot section's claim)
    and, once it reports, its host line as Central's link store records it (decision 0034)."""

    def __init__(self, registry, *, enrolled=True):
        self.registry = registry
        boots, _ = cold_setup(registry)
        self.offer = boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
        self.sequence = 0
        self.player_id = enroll(registry, count=1, device_id=DEVICE_ID)[0]["player_id"] if enrolled else None

    def host_sample(self):
        """One put of Host Management's metrics on its line, its birth (the line's start) first."""
        if self.sequence == 0:
            on_line(self.registry, SERIAL, BIRTH_KEY, b"{}", 1)
        self.sequence += 1
        metrics = (HostMetricV2("uptime", self.sequence, "seconds"),)
        on_line(self.registry, SERIAL, HOST_METRICS_KEY, encode_host_metrics_state(HostMetricsState(metrics)),
                self.sequence + 1)


def _node_reads(page):
    """Every node read the pages send, in order, except the shell's fleet host read (HOSTS_READ)."""
    sent = []
    page.on("request", lambda request: sent.append(request.url)
            if "/v1/operator/node/" in request.url and HOSTS_READ not in request.url else None)
    return sent


def _layer(page, layer):
    """One software layer's rows on the Software and screens page."""
    return page.get_by_role("region", name="Layers", exact=True).get_by_role(
        "group", name=layer, exact=True)


def _host(page):
    """Host Management's line rows: Link and sessions, on the Pi's Hardware page."""
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
    with operator_server(registry.db, registry.clock) as origin:
        sent = _node_reads(page)
        connect(page, origin, "hardware")
        row = hardware_list(page).get_by_role("table", name="Not driving a Frame", exact=True).get_by_role(
            "row").filter(has=page.get_by_role("link", name=NAME, exact=True))
        expect(row).to_contain_text("Standing: Unbound")
        page.wait_for_timeout(200)
        assert sent == [], "the Hardware list read node records"

        pi = open_pi(page, NAME)
        expect(pi).to_contain_text("Standing: Unbound")
        # The age is the database's clock less the host line's record (decision 0034).
        expect(_host(page)).to_contain_text(
            re.compile(r"Last reported: Host Management last reported \d+ s ago"))
        assert any(url.endswith(f"/v1/operator/node/devices/{DEVICE_ID}") for url in sent)
        # Leaving the page stops its node read.
        go(page, "wall")
        count = len(sent)
        page.wait_for_timeout(300)
        assert len(sent) == count


def test_one_layer_and_no_app_section_on_the_software_page(page, registry):
    """S4c-2a-iii and 2a-iv: the Software and screens page shows the Player app's layer (Host
    Management lives on the Pi's Hardware page), the Boot section names the host line's start,
    and nothing of the old path is left: no App section, no Stage app, no Qualified fallback, no
    App Manager, App Effect Broker or Display Host row. Mutation probe: restore the App section
    in PlayerPage.jsx -> its region is found, fails."""
    box = Box(registry)
    report_readiness(registry, box.player_id)
    box.host_sample()
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_player(page, NAME)
        layers = page.get_by_role("region", name="Layers", exact=True).get_by_role("listitem")
        expect(layers).to_have_count(1)
        for gone in ("Host Management", "App Manager", "App Effect Broker", "Display Host"):
            expect(_layer(page, gone)).to_have_count(0)
        expect(_layer(page, "Player app")).to_contain_text("Player app last reported")
        expect(page.get_by_role("region", name="Boot", exact=True)).to_contain_text(
            "Host line: Host Management reported line start epoch")
        main = page.get_by_role("main")
        expect(page.get_by_role("region", name="App", exact=True)).to_have_count(0)
        expect(main.get_by_role("button", name=re.compile("^Stage app"))).to_have_count(0)
        expect(main.get_by_role("button", name=re.compile("^Begin qualifying"))).to_have_count(0)
        expect(main).not_to_contain_text("Qualified fallback")
        expect(main).not_to_contain_text("App operations")


def test_a_missing_field_reads_unknown_naming_it(page, registry):
    box = Box(registry)
    box.host_sample()

    def without_host_value(route):
        data = route.fetch().json()
        data["host"]["received_at"] = None
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route(DEVICE_READ, without_host_value)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_pi(page, NAME)
        expect(_host(page)).to_contain_text(
            "Last reported: Unknown: host.received_at not served")


def test_a_malformed_read_blanks_one_section_only(page, registry):
    box = Box(registry)
    box.host_sample()

    def drifted(route):
        data = route.fetch().json()
        data["boot_claims"] = 5  # payload drift: a list became a number
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route(DEVICE_READ, drifted)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        player = open_player(page, NAME)
        expect(page.get_by_role("region", name="Boot", exact=True).get_by_role("alert")).to_have_text(
            "This section could not be shown. The rest of the page is current.")
        # The other sections, the page and the console stay up.
        expect(_layer(page, "Player app")).to_contain_text("Last reported")
        expect(player).to_contain_text("Standing: Unbound")
        expect(page.get_by_role("region", name="Outputs", exact=True)).to_contain_text("HDMI-A-1")
        go(page, "hardware")
        expect(hardware_list(page).get_by_role("link", name=NAME, exact=True)).to_be_visible()


def test_the_boot_section_holds_node_records_only(page, registry):
    box = Box(registry)
    box.host_sample()
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_player(page, NAME)
        boot = page.get_by_role("region", name="Boot", exact=True)
        expect(boot).to_contain_text("Host line: Host Management reported line start epoch")
        expect(boot).to_contain_text(
            f"Node boot offer: Issued for boot {BOOT_ID}, not proof the Player booted")
        expect(boot.locator("p[data-truth]")).to_have_count(2)
        # No V1 boot record, no kernel command line advice: every Pi boots by node path.
        expect(boot).not_to_contain_text("deprecated")
        expect(boot.get_by_role("note")).to_have_count(0)


def test_a_failed_device_read_keeps_its_rows_marked_refresh_failed(page, registry):
    box = Box(registry)
    box.host_sample()
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, paused_at=registry.clock.utc())
        open_pi(page, NAME)
        host = _host(page)
        last = re.compile(r"Host Management last reported \d+ s ago")
        expect(host).to_contain_text(last)
        page.route(DEVICE_READ, lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error": "boom"}'))
        with page.expect_response(DEVICE_READ):
            drive_poll(page)
        link = page.get_by_role("region", name="Link and sessions", exact=True)
        expect(link).to_contain_text("refresh failed")
        expect(host).to_contain_text(last)


def test_a_reboot_render_error_leaves_the_header_and_health_standing(page, registry):
    """Reboot, on the Pi's Hardware page, sits behind its own boundary."""
    box = Box(registry)
    box.host_sample()

    def drifted(route):
        data = route.fetch().json()
        data["reboot_commands"] = 5  # payload drift: only Reboot reads this list
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    page.route(DEVICE_READ, drifted)
    with operator_server(registry.db, registry.clock) as origin:
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
    """The Pi's Hardware page: Reboot, Health (from Central's real fleet host read), Light (the
    Pi's light sensor), then Link and sessions; Health's raw disclosure holds the sample's lines; Link and sessions keeps
    Host Management's "Last reported" and its line's start."""
    box = Box(registry)
    box.host_sample()
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        pi = open_pi(page, NAME)
        sections = pi.locator("section[aria-label]").evaluate_all("(all) => all.map((s) => s.ariaLabel)")
        assert sections == ["Reboot", "Health", "Light", "Link and sessions", "Danger zone"], sections
        health = page.get_by_role("region", name="Health", exact=True)
        # Health's age is the database's clock less its host line's record (decision 0034).
        expect(health).to_contain_text(re.compile(r"Host Management: Host Management last reported \d+ s ago"))
        health.get_by_role("button", name="Every reported metric", exact=True).click()
        expect(health.get_by_role("list", name="Every reported metric", exact=True)).to_contain_text(
            "uptime: 1 seconds (host sampler)")
        host = _host(page)
        expect(host).to_contain_text(re.compile(r"Last reported: Host Management last reported \d+ s ago"))
        host.get_by_role("button", name="Host Management details", exact=True).click()
        expect(host).to_contain_text("Line start epoch")
        expect(host).not_to_contain_text("uptime")
        expect(host).not_to_contain_text("visible pixels")


def test_a_box_seen_only_at_boot_is_listed_not_enrolled(page, registry):
    box = Box(registry, enrolled=False)
    box.host_sample()
    with operator_server(registry.db, registry.clock) as origin:
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
    registry.retire(box.player_id, by="console")
    with operator_server(registry.db, registry.clock) as origin:
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


def test_the_retired_list_bookmarks_land_on_home(page, registry):
    # No aliases (owner rule): the Equipment and Players lists' bookmarks are unknown routes,
    # which land where every unknown route does; the sidebar's Fleet group is Hardware.
    enroll(registry, count=1)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        for bookmark in ("#/equipment", "#/players"):
            page.goto(origin + "/console" + bookmark)
            expect(page.get_by_role("heading", level=1, name="Home", exact=True)).to_be_visible()
            assert current_hash(page) == "#/home"
        nav = page.get_by_role("navigation", name="Sections", exact=True)
        for gone in ("Equipment", "Players", "Software and screens"):
            expect(nav.get_by_role("link", name=gone, exact=True)).to_have_count(0)
        expect(nav.get_by_role("list", name="Fleet", exact=True).get_by_role("link")).to_have_text(
            ["Hardware", "Releases"])


# --- B2: Reboot Player (console DDD §10).

REBOOTS = f"**/v1/operator/node/devices/{DEVICE_ID}/reboots"
RUN_STAYS = "The Run stays active. Central sends no command to other Frames or Actuators."


def _node_server(registry):
    reboot_queue(registry.db)   # the reboot route queues S4a's RebootPi
    return operator_server(registry.db, registry.clock)


def _asks(registry) -> int:
    """The operator's reboot asks Central recorded (central/fleet/pi_reboot.py)."""
    with registry.db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM events WHERE kind='link-action' "
                            "AND body->>'kind'='reboot-asked'").fetchone()["n"]


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
    registry.create_frame(frame_create("lobby", x_mm=100, y_mm=100), by="console")
    registry.bind("lobby", box.player_id, "HDMI-A-1", expected_generation=0, by="console")
    with _node_server(registry) as origin:
        sent = _reboot_posts(page)
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True)
        expect(dialog).to_contain_text("Frame lobby: no live Run")
        expect(dialog).to_contain_text(RUN_STAYS)
        expect(dialog).to_contain_text("Central asks Host Management once, over the Pi's link.")
        # NR2: the one static sentence on what the next boot is offered, linking to Releases.
        expect(dialog).to_contain_text("On its next boot, Central offers this Player its own version (see Releases).")
        expect(dialog.get_by_role("link", name="Releases", exact=True)).to_have_attribute("href", "#/releases")
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(_reboot(page).get_by_role("status")).to_have_text(
            "Reboot recorded. Requested · delivery unknown.")
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        expect(history).to_contain_text(
            "Requested · delivery unknown · Central asks Host Management over the Pi's link")
        # While it is Requested, no new command id can be sent: only its retry is offered.
        expect(_reboot(page).get_by_role("button", name="Reboot Player", exact=True)).to_have_count(0)
        _reboot(page).get_by_role("button", name="Send the reboot request again", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Send the same request again", exact=True).click()
        expect(page.get_by_role("dialog").get_by_role("status")).to_have_text(
            "Already recorded. Requested · delivery unknown.")
        assert len(sent) == 2 and sent[0] == sent[1]
        assert set(sent[0]) == {"command_id", "reason"} and sent[0]["reason"].startswith("console/")
    assert _asks(registry) == 1


def test_a_lost_answer_is_retried_with_the_identical_body_inside_the_window(page, registry):
    Box(registry)
    with _node_server(registry) as origin:
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
    with _node_server(registry) as origin:
        sent = _reboot_posts(page)
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True).get_by_role(
            "button", name="Reboot Player", exact=True).dblclick()
        expect(_reboot(page).get_by_role("status")).to_have_text("Reboot recorded. Requested · delivery unknown.")
        page.wait_for_timeout(500)
        assert len(sent) == 1
    assert _asks(registry) == 1


def test_a_queued_reboot_once_run_settles_and_a_new_request_states_its_outcome(page, registry):
    """The worker runs the queued reboot (the real handler; this Pi has no link to answer): the
    history reads Not delivered, Reboot Player is offered again, and the new dialog names it."""
    Box(registry)
    with _node_server(registry) as origin:
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Reboot Player", exact=True).click()
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        expect(history).to_contain_text("Requested · delivery unknown")
        run_queued(registry.db, DEVICE_ID, delivered=False)
        # The first read after the run arrives within one 5 s poll interval.
        expect(history).to_contain_text("Not delivered: the Pi had no link to Central or did not answer",
                                        timeout=10_000)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        expect(page.get_by_role("dialog")).to_contain_text("The previous request: Not delivered")
        page.get_by_role("dialog").get_by_role("button", name="Cancel", exact=True).click()


def test_an_asked_reboot_shows_its_named_state(page, registry):
    Box(registry)
    ask_reboot(registry.db, registry.clock, DEVICE_ID, uuid4(), reason="operator:fixture")
    run_queued(registry.db, DEVICE_ID, delivered=True)
    with _node_server(registry) as origin:
        connect(page, origin)
        open_pi(page, NAME)
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        expect(history).to_contain_text("Host Management accepted the reboot · completion unknown")
        expect(history).to_contain_text(
            re.compile(r"Host Management reported an accepted answer to Central's ask · first received \d+ s ago"))
        # Asked is a settled answer, so a new request may be made.
        expect(_reboot(page).get_by_role("button", name="Reboot Player", exact=True)).to_be_enabled()
        expect(page.get_by_role("main")).not_to_contain_text("ubsequent boot")


def test_a_dialog_frozen_at_an_older_read_sends_nothing_once_another_request_is_outstanding(
        page, registry):
    """R0's stale dialog: the dialog freezes its request at one read; another page then records
    a different reboot request, which the next read lists as outstanding. Send is disabled, and
    the dialog's own send handler, called directly, refuses inside `sendReboot`: zero POSTs."""
    Box(registry)
    with _node_server(registry) as origin:
        sent = _reboot_posts(page)
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True)
        send = dialog.get_by_role("button", name="Reboot Player", exact=True)
        expect(send).to_be_enabled()
        # Another tab's request, recorded by Central's real reboot owner after the dialog froze.
        ask_reboot(registry.db, registry.clock, DEVICE_ID, uuid4())
        registry.clock.advance(5)
        expect(dialog.get_by_role("alert")).to_have_text(
            "Another reboot request for this Player is outstanding; close this dialog and review it.", timeout=10_000)
        expect(send).to_be_disabled()
        _call_send_directly(send)
        expect(dialog.get_by_role("status")).to_have_text(
            "Another reboot request for this Player is outstanding; close this dialog and review it.")
        assert sent == []
    assert _asks(registry) == 1


def test_centrals_outstanding_fence_reads_as_changed(page, registry):
    """A 409 node_reboot_outstanding (another page's request committed after this page's newest
    read) reads as changed, never as a refusal of this Player."""
    Box(registry)
    with _node_server(registry) as origin:
        sent = _reboot_posts(page)
        held = _HeldDeviceRead(page)
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True)
        held.freeze()  # the other page's commit lands after this page's newest read
        ask_reboot(registry.db, registry.clock, DEVICE_ID, uuid4())
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Another reboot request for this Player is outstanding; close this dialog and review it.")
        assert len(sent) == 1
    assert _asks(registry) == 1
