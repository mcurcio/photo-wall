"""The host-health tracer in the console (console DDD §61-§63, bead T1), against a stubbed
fleet host read (G12): the banded temperature fact on the Hardware list, the Bound Player's
host-silence row on Needs attention with its [Frame] and [Player] links (plain text on a Show
page, R4), no row for an Unbound Player, no band and no incident without a served threshold,
and silence wording composed from the served limit. Console by Domain (E1 U1): the Hardware
list's groups (Driving a Frame worst first, Not driving a Frame, Retired), the Pi's Hardware
page with Health and its raw disclosure, and the tables scrolling sideways at phone width. A1 (§61-§62): the strip's Frames-then-Players summary and
its " · host health not read" suffix, Player rows after Frame rows, and the Status host chip.

The rest of the app is real (production app on the loopback harness, node control on); only
`GET /v1/operator/node/hosts` is fulfilled by the test, so its `read_at` and receipts are
exact. Central's own G12 is proven in tests/test_node_fleet_hosts.py.
"""

import json
import os
import re

import pytest
from console_tasks import connect, go, hardware_list, open_frame, open_pi, player_name, visible_page
from operator_harness import assert_fits_width, operator_server, report_readiness
from playwright.sync_api import expect
from test_registry import enroll

from central.registry import FrameCreate
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

HOSTS = "**/v1/operator/node/hosts"
FRAME = "lobby-left"
THRESHOLDS = {"host_silent_after_seconds": 60,
              "metrics": [{"name": "soc_temperature", "unit": "celsius", "notice_at": 75, "alarm_at": 80}]}


def _players(registry, *, bound=True):
    """A bound Player on Frame lobby-left (when `bound`) and an Unbound one; their device ids
    and names."""
    bound_identity, _, bound_request = enroll(registry, count=1)
    spare_identity, _, spare_request = enroll(registry, count=1)
    registry.create_frame(FrameCreate(id=FRAME, surface_id="wall", x_mm=100, y_mm=100, width_mm=300,
                                      height_mm=500, profile=FrameProfile(
                                          width_px=1080, height_px=1920, diagonal_inches=24)))
    if bound:
        registry.bind(FRAME, bound_identity["player_id"], "HDMI-A-1", expected_generation=0)
    return ((bound_request.device_id, player_name(registry, bound_identity["player_id"])),
            (spare_request.device_id, player_name(registry, spare_identity["player_id"])))


def _row(device_id, received_at, temperature):
    return {"device_id": device_id, "previous_boot_received_at": None, "intake_full": False,
            "host": {"received_at": received_at, "fault_code": None,
                     "metrics": [{"name": "soc_temperature", "value": temperature,
                                  "unit": "celsius", "source": "host_sampler"}]}}


def _stub(page, document):
    """Fulfil the shell's fleet host read with `document` (a dict, re-read on every poll)."""
    page.route(HOSTS, lambda route: route.fulfill(status=200, content_type="application/json",
                                                  body=json.dumps(document)))


def _card(page, name):
    """A Pi's row in the Hardware list, whichever group holds it."""
    return hardware_list(page).get_by_role("row").filter(has=page.get_by_role("link", name=name, exact=True))


def _attention(page):
    return visible_page(page).get_by_role("list", name="Frames and Players needing attention", exact=True)


def test_a_hot_reporting_player_shows_the_banded_fact_and_no_attention_row(page, registry):
    (bound, bound_name), (spare, spare_name) = _players(registry)
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS,
                 "devices": [_row(bound, 996.0, 81.2), _row(spare, 999.0, 62)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        expect(_card(page, bound_name)).to_contain_text(
            "Temperature: 81.2 °C · hot (Central's inference: at or above 80 °C, Central's threshold)")
        expect(_card(page, spare_name)).to_contain_text(
            "Temperature: Host Management last reported 1 s ago · 62 °C")
        go(page, "attention")
        expect(visible_page(page)).not_to_contain_text("Host Management silent")


def test_a_silent_bound_player_raises_one_row_with_both_links_and_an_unbound_one_none(page, registry):
    (bound, bound_name), (spare, spare_name) = _players(registry)
    document = {"read_at": 1000.0, "thresholds": THRESHOLDS,
                "devices": [_row(bound, 880.0, 95), _row(spare, 880.0, 95)]}
    _stub(page, document)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        # The last value reads "at last report", unbanded.
        expect(_card(page, bound_name)).to_contain_text(
            "Temperature: Host Management last reported 2 min ago · 95 °C at last report")
        expect(_card(page, bound_name)).not_to_contain_text("hot")
        go(page, "attention")
        rows = _attention(page).get_by_role("listitem").filter(has_text="Host Management silent")
        expect(rows).to_have_count(1)
        expect(rows).to_contain_text(
            f"{bound_name} (Frame {FRAME}) — Host Management silent · last reported 2 min ago")
        expect(rows.get_by_role("link", name=f"Frame {FRAME}", exact=True)).to_have_attribute(
            "href", re.compile(rf"^#/wall/frames/{FRAME}/"))
        expect(rows.get_by_role("link", name=bound_name, exact=True)).to_have_attribute(
            "href", f"#/hardware/{bound}")
        expect(visible_page(page)).not_to_contain_text(spare_name)
        # On a Show page the strip's rows are plain text (R4).
        go(page, "now")
        page.get_by_role("button", name="Show list", exact=True).click()
        strip = page.get_by_role("region", name="Wall attention", exact=True)
        entry = strip.get_by_role("listitem").filter(has_text="Host Management silent")
        expect(entry).to_have_count(1)
        expect(entry.get_by_role("link")).to_have_count(0)
        expect(entry.get_by_role("button")).to_have_count(0)


def test_no_served_threshold_shows_the_value_unbanded_and_raises_nothing(page, registry):
    (bound, bound_name), _ = _players(registry)
    _stub(page, {"read_at": 1000.0, "thresholds": {"host_silent_after_seconds": 60, "metrics": []},
                 "devices": [_row(bound, 996.0, 95)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        expect(_card(page, bound_name)).to_contain_text(
            "Temperature: Host Management last reported 4 s ago · 95 °C")
        expect(_card(page, bound_name)).not_to_contain_text("Central's inference")
        go(page, "attention")
        expect(visible_page(page)).not_to_contain_text("Host Management")


def test_the_served_limit_composes_and_moves_the_silence_judgement(page, registry):
    (bound, bound_name), _ = _players(registry)
    document = {"read_at": 1000.0, "thresholds": {**THRESHOLDS, "host_silent_after_seconds": 150},
                "devices": [_row(bound, 880.0, 50)]}
    _stub(page, document)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "attention")
        # 120 s against a served 150 s: reporting, no row.
        expect(visible_page(page)).not_to_contain_text("Host Management silent")
        document["thresholds"] = {**THRESHOLDS, "host_silent_after_seconds": 90}
        page.reload()
        go(page, "hardware")
        go(page, "attention")
        expect(_attention(page).get_by_role("listitem").filter(
            has_text="Host Management silent")).to_have_count(1)
        go(page, "hardware")
        expect(_card(page, bound_name)).to_contain_text("· 50 °C at last report")
        expect(_card(page, bound_name)).to_contain_text(
            "Host Management: Host Management silent · last reported 2 min ago "
            "(Central's inference: no report for over 90 s, Central's limit)")


# N1 (console DDD §62-§63): the firmware flags, CPU and storage lines on the Players card.
_FLAGS = ("under_voltage", "frequency_capped", "throttled", "soft_temperature_limit")
N1_THRESHOLDS = {"host_silent_after_seconds": 60, "metrics": [
    *THRESHOLDS["metrics"],
    *({"name": f"{flag}_now", "unit": "boolean", "notice_at": None, "alarm_at": 1} for flag in _FLAGS),
    *({"name": f"{flag}_occurred", "unit": "boolean", "notice_at": 1, "alarm_at": None} for flag in _FLAGS)]}


def _n1_row(device_id, bits, extra=()):
    row = _row(device_id, 996.0, 50)
    row["host"]["metrics"] += [
        {"name": f"{flag}_{when}", "value": (bits >> (index + shift)) & 1, "unit": "boolean", "source": "firmware"}
        for when, shift in (("now", 0), ("occurred", 16)) for index, flag in enumerate(_FLAGS)]
    row["host"]["metrics"] += list(extra)
    return row


def _line(page, name, label):
    return _card(page, name).locator("p[data-truth]").filter(has_text=f"{label}: ")


def test_throttling_bands_cpu_unbanded_and_unknowns(page, registry):
    (bound, bound_name), (spare, spare_name) = _players(registry)
    cpu = {"name": "cpu_busy", "value": 23, "unit": "percent", "source": "host_sampler"}
    run = {"name": "runtime_available", "value": 1.2e9, "unit": "bytes", "source": "host_sampler"}
    document = {"read_at": 1000.0, "thresholds": N1_THRESHOLDS, "devices": [
        {**_n1_row(bound, 0x50005, [cpu, run]),
         "preparation": {"received_at": 994.0, "state": "refused", "fault": "node_storage_capacity",
                         "available_bytes": 9e8, "required_bytes": 1.4e9}},
        _n1_row(spare, 0x10000, [cpu, cpu])]}
    _stub(page, document)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        throttling = _line(page, bound_name, "Throttling")
        expect(throttling).to_have_attribute("data-band", "alarm")
        expect(throttling).to_contain_text("Throttling: Throttled now · Under-voltage now (Central's "
                                           "inference: the firmware flag is set, Central's threshold)")
        # The same words on a spare carry no band: a spare is never alarmed (G2).
        occurred = _line(page, spare_name, "Throttling")
        expect(occurred).to_have_attribute("data-band", "none")
        expect(occurred).to_contain_text("None now · under-voltage occurred recently (the firmware's sticky flag)")
        expect(_line(page, bound_name, "CPU")).to_have_attribute("data-band", "none")
        expect(_line(page, bound_name, "CPU")).to_contain_text("CPU: Host Management last reported 4 s ago · 23 % busy")
        # The same cataloged name twice in one sample reads Unknown.
        expect(_line(page, spare_name, "CPU")).to_contain_text("CPU: Unknown: two values reported")
        storage = _line(page, bound_name, "Storage")
        expect(storage.first).to_contain_text("Storage: Host Management last reported 4 s ago · 1.2 GB free in /run")
        expect(storage.nth(1)).to_contain_text("App Manager last reported 6 s ago · App Manager refused a "
                                               "preparation: needs 1.4 GB, room 0.9 GB")
        expect(_line(page, spare_name, "Storage")).to_contain_text("Storage: Unknown: not reported")
        # One flag missing: the Throttling item reads Unknown.
        document["devices"][0]["host"]["metrics"] = [
            metric for metric in document["devices"][0]["host"]["metrics"] if metric["name"] != "throttled_now"]
        page.reload()
        expect(_line(page, bound_name, "Throttling")).to_contain_text("Throttling: Unknown: not reported")


def test_host_facts_and_the_base_render_under_one_record_receipt(page, registry):
    (bound, bound_name), (spare, spare_name) = _players(registry)
    facts = {"first_received_at": 1000.0 - 3 * 86400, "kernel_release": "6.6.51+rpt-rpi-v8",
             "interface": "eth0", "link_state": "up", "address": "192.168.1.40", "base_tag": "2026.10.01"}
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS, "devices": [
        {**_row(bound, 996.0, 50), "boot": {"base_tag": "2026.10.01"}, "facts": facts},
        {**_row(spare, 996.0, 50), "boot": None, "facts": None}]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        card = _card(page, bound_name)
        expect(card).to_contain_text("Host facts first received 3 d ago")
        expect(card).to_contain_text("Network: Host Management reported eth0 up")
        expect(card).to_contain_text("Network: Host Management reported address 192.168.1.40")
        # The Software facts are not Hardware columns (Console by Domain § Fleet); until the
        # Software list lands they read on the Pi's Hardware page, in Health.
        expect(card).not_to_contain_text("Software:")
        # The record's receipt is worded once, not on each fact.
        expect(card.get_by_text(re.compile("first received"))).to_have_count(1)
        spare_card = _card(page, spare_name)
        expect(spare_card).to_contain_text("Host facts: Unknown: no host facts received on this boot")
        expect(spare_card).not_to_contain_text("first received")
        open_pi(page, bound_name)
        health = page.get_by_role("region", name="Health", exact=True)
        expect(health).to_contain_text("Software: Host Management reported kernel 6.6.51+rpt-rpi-v8")
        expect(health).to_contain_text("Software: Host Management reported base 2026.10.01")
        expect(health).to_contain_text("Software: Central's offer: base 2026.10.01 "
                                       "(claimed at boot by this boot's node session, unverified)")
        expect(health).not_to_contain_text("Base differs")
        expect(health.get_by_text(re.compile("first received"))).to_have_count(1)
        open_pi(page, spare_name)
        health = page.get_by_role("region", name="Health", exact=True)
        expect(health).to_contain_text("Software: Unknown: no current node boot admission")
        expect(health).not_to_contain_text("first received")


# Console by Domain (E1 U1): the Hardware list's groups and the Pi's Hardware page.


def _enrolled(registry, count):
    """`count` enrolled, Unbound Players: (device id, name) each."""
    boxes = []
    for _ in range(count):
        identity, _, request = enroll(registry, count=1)
        boxes.append((request.device_id, player_name(registry, identity["player_id"])))
    return boxes


def _table_names(page):
    """The Pis' names down the Hardware list, every group in order."""
    return hardware_list(page).locator("tbody th > a:first-child").all_inner_texts()


def test_rows_run_worst_first_alarm_notice_unknown_ok(page, registry):
    # Bound Players: only they are tiered (G2).
    (_, ok, ok_name), (_, unknown, unknown_name), (_, notice, notice_name), (_, alarm, alarm_name) = (
        _frames_with_players(registry, ["f-ok", "f-unknown", "f-notice", "f-alarm"]))
    # `unknown` is absent from the read: "Unknown: not read".
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS, "devices": [
        _row(ok, 999.0, 50), _row(notice, 999.0, 76), _row(alarm, 999.0, 81)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        expect(_card(page, unknown_name)).to_contain_text("Host Management: Unknown: not read")
        assert _table_names(page) == [alarm_name, notice_name, unknown_name, ok_name]
        expect(_card(page, alarm_name)).to_have_attribute("data-severity", "alarm")
        # Every Pi drives a Frame: one group, one table; no card list beside it, no counts line.
        expect(hardware_list(page).get_by_role("table")).to_have_count(1)
        expect(hardware_list(page).get_by_role("table", name="Driving a Frame", exact=True)).to_be_visible()
        expect(page.get_by_role("list", name="Players", exact=True)).to_have_count(0)


def test_spares_are_never_alarms_and_retired_boxes_sort_below_healthy_players(page, registry):
    # G2: a silent Unbound spare and a box seen at boot sit below every Bound row with no tier
    # and no band (the spare's silence reads as a plain receipt age); a retired box comes last, stating
    # it is not read, never "Unknown: not read" in every column.
    ((_, bound, bound_name),) = _frames_with_players(registry, [FRAME])
    ((spare, spare_name),) = _enrolled(registry, 1)
    retired_identity, _, _ = enroll(registry, count=1)
    retired_name = player_name(registry, retired_identity["player_id"])
    registry.retire(retired_identity["player_id"])
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS,
                 "devices": [_row(bound, 999.0, 50), _row(spare, 100.0, 95)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        expect(_card(page, retired_name)).to_contain_text("Not read: Player retired")
        assert _table_names(page) == [bound_name, spare_name, retired_name]
        spare_row = _card(page, spare_name)
        expect(spare_row).to_have_attribute("data-severity", "none")
        # Its silence reads as a plain receipt age; none of Central's threshold judgements
        # reaches its words (the words, not only the class).
        expect(spare_row).to_contain_text("last reported 15 min ago")
        expect(spare_row).to_contain_text("95 °C")
        for judgement in ("silent", "hot", "at last report", "Central's"):
            expect(spare_row).not_to_contain_text(judgement)
        expect(spare_row.locator("[data-band=alarm], [data-band=notice]")).to_have_count(0)
        retired_row = _card(page, retired_name)
        expect(retired_row).to_have_attribute("data-severity", "none")
        expect(retired_row).not_to_contain_text("Unknown: not read")
        expect(_card(page, bound_name)).to_have_attribute("data-severity", "ok")
        # Each in its group.
        for group, name in (("Driving a Frame", bound_name), ("Not driving a Frame", spare_name),
                            ("Retired", retired_name)):
            expect(hardware_list(page).get_by_role("table", name=group, exact=True).get_by_role(
                "link", name=name, exact=True)).to_be_visible()
        go(page, "attention")
        expect(visible_page(page)).not_to_contain_text(spare_name)


def test_player_health_shows_both_bases_and_names_a_mismatch(page, registry):
    (bound, bound_name), _ = _players(registry)
    facts = {"first_received_at": 1000.0 - 60, "kernel_release": "6.6.51+rpt-rpi-v8",
             "interface": "eth0", "link_state": "up", "address": "192.168.1.40", "base_tag": "2026.09.30"}
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS, "devices": [
        {**_row(bound, 996.0, 50), "boot": {"base_tag": "2026.10.01"}, "facts": facts}]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_pi(page, bound_name)
        software = page.get_by_role("region", name="Health", exact=True).get_by_role(
            "group", name="Software", exact=True)
        expect(software).to_contain_text("Software: Host Management reported base 2026.09.30")
        expect(software).to_contain_text("Software: Central's offer: base 2026.10.01 "
                                         "(claimed at boot by this boot's node session, unverified)")
        expect(software).to_contain_text(
            "Software: Base differs: Host Management reported 2026.09.30, Central's offer 2026.10.01 "
            "(Central's inference: the reported tag and the offered tag differ)")
        # A derived fact, not an alarm: no band and no incident.
        expect(software.locator("[data-band=alarm]")).to_have_count(0)
        go(page, "attention")
        expect(visible_page(page)).not_to_contain_text("Base differs")


def test_a_spare_is_listed_under_not_driving_a_frame_with_its_hint(page, registry):
    (bound, bound_name), (spare, spare_name) = _players(registry)
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS,
                 "devices": [_row(bound, 999.0, 50), _row(spare, 997.0, 50)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        spares = page.get_by_role("region", name="Not driving a Frame", exact=True)
        entry = spares.get_by_role("row").filter(has=page.get_by_role("link", name=spare_name, exact=True))
        expect(entry).to_have_count(1)
        expect(entry).to_contain_text("Standing: Unbound")
        expect(entry).to_contain_text("Host Management: Host Management last reported 3 s ago")
        expect(entry.get_by_role("link", name=spare_name, exact=True)).to_have_attribute(
            "href", f"#/hardware/{spare}")
        expect(spares).not_to_contain_text(bound_name)
        # Listed, never counted.
        expect(spares).not_to_contain_text(re.compile(r"\b\d+ (Players?|spares?)\b"))


def test_the_pi_page_shows_health_after_reboot_and_holds_the_raw_lines_only_there(page, registry):
    (bound, bound_name), _ = _players(registry)
    row = _n1_row(bound, 0x50005)
    row["host"]["fault_code"] = "sampler_partial"
    row["host"]["metrics"].append({"name": "fan_rpm", "value": 1200, "unit": "rpm", "source": "host_sampler"})
    _stub(page, {"read_at": 1000.0, "thresholds": N1_THRESHOLDS, "devices": [
        {**row, "boot": {"base_tag": "2026.10.01"}, "facts": {
            "first_received_at": 1000.0 - 3 * 86400, "kernel_release": "6.6.51+rpt-rpi-v8",
            "interface": "eth0", "link_state": "up", "address": "192.168.1.40"}}]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        player = open_pi(page, bound_name)
        sections = player.locator("section[aria-label]").evaluate_all("(all) => all.map((s) => s.ariaLabel)")
        assert sections[:2] == ["Reboot", "Health"], sections
        health = page.get_by_role("region", name="Health", exact=True)
        expect(health.get_by_role("group", name="Thermal", exact=True)).to_contain_text("Temperature: ")
        expect(health.get_by_role("group", name="Power and throttling", exact=True)).to_contain_text(
            "Throttling: Throttled now · Under-voltage now")
        expect(health.get_by_role("group", name="Network", exact=True)).to_contain_text(
            "Network: Host Management reported eth0 up")
        expect(health.get_by_role("group", name="Software", exact=True)).to_contain_text(
            "Software: Central's offer: base 2026.10.01 (claimed at boot by this boot's node session, unverified)")
        expect(health.get_by_text(re.compile("first received"))).to_have_count(1)
        raw = health.get_by_role("list", name="Every reported metric", exact=True)
        expect(raw).to_be_hidden()
        health.get_by_role("button", name="Every reported metric", exact=True).click()
        expect(raw).to_contain_text("fan rpm: 1200 rpm (host sampler)")
        expect(raw).to_contain_text("soc temperature: 50 celsius (host sampler)")
        expect(raw).to_contain_text("Reported fault: sampler partial")
        expect(raw).to_contain_text("Host samples do not show visible pixels.")
        # The raw lines live only in Health's disclosure.
        for text in ("fan rpm: 1200", "Host samples do not show visible pixels", "Reported fault"):
            expect(player.get_by_text(text, exact=False)).to_have_count(1)
        expect(page.get_by_role("region", name="Link and sessions", exact=True)).not_to_contain_text("fan rpm")


def test_the_hardware_tables_scroll_sideways_at_phone_width_and_the_page_does_not(page, registry):
    (bound, bound_name), (spare, _) = _players(registry)
    page.set_viewport_size({"width": 390, "height": 844})
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS,
                 "devices": [_row(bound, 999.0, 50), _row(spare, 999.0, 50)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        expect(_card(page, bound_name)).to_be_visible()
        assert_fits_width(page, "Pis")
        driving = hardware_list(page).get_by_role("table", name="Driving a Frame", exact=True)
        assert driving.evaluate("(table) => table.parentElement.scrollWidth > table.parentElement.clientWidth")
        expect(hardware_list(page).get_by_role("table")).to_have_count(2)
        expect(page.get_by_role("list", name="Players", exact=True)).to_have_count(0)


# A1 (console DDD §61-§62): the strip and Needs attention count Players too; the Status chip.


def _frames_with_players(registry, frame_ids, device_ids=None):
    """One Frame per id, each bound to its own newly enrolled Player: (player id, device id,
    name) each. `device_ids` fixes the boxes' ids (else random, so name order is random)."""
    boxes = []
    for index, frame_id in enumerate(frame_ids):
        identity, _, request = enroll(registry, count=1, device_id=device_ids[index] if device_ids else None)
        registry.create_frame(FrameCreate(id=frame_id, surface_id="wall", x_mm=100 + 400 * index, y_mm=100,
                                          width_mm=300, height_mm=500, profile=FrameProfile(
                                              width_px=1080, height_px=1920, diagonal_inches=24)))
        registry.bind(frame_id, identity["player_id"], "HDMI-A-1", expected_generation=0)
        boxes.append((identity["player_id"], request.device_id, player_name(registry, identity["player_id"])))
    return boxes


def _summary(page):
    return page.get_by_role("region", name="Wall attention", exact=True).get_by_role("status")


def test_the_strip_counts_frames_then_players_and_player_rows_follow_frame_rows(page, registry):
    (_, host_device, host_name), _, _ = _frames_with_players(registry, ["host-sick", "quiet-a", "quiet-b"])
    # The two other Players never report: their Frames are alarms after four minutes.
    registry.clock.advance(240)
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS, "devices": [_row(host_device, 880.0, 50)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "attention")
        expect(_summary(page)).to_have_text("3 Frames · 1 Player need attention")
        items = _attention(page).get_by_role("listitem")
        expect(items).to_have_count(4)
        # Frame rows first, then the Player row; no host item on any Frame row.
        expect(items.last).to_contain_text(
            f"{host_name} (Frame host-sick) — Host Management silent · last reported 2 min ago")
        for index in range(3):
            expect(items.nth(index)).not_to_contain_text("Host Management")
        expect(items.last.get_by_role("link", name=host_name, exact=True)).to_have_attribute(
            "href", f"#/hardware/{host_device}")
        # On a Show page every row, the Player row too, is plain text (R4).
        go(page, "now")
        strip = page.get_by_role("region", name="Wall attention", exact=True)
        strip.get_by_role("button", name="Show list", exact=True).click()
        expect(strip.get_by_role("listitem")).to_have_count(4)
        expect(strip.get_by_role("list").get_by_role("link")).to_have_count(0)
        expect(strip.get_by_role("list").get_by_role("button")).to_have_count(0)


def test_two_frame_alarms_and_one_player_incident(page, registry):
    boxes = _frames_with_players(registry, ["a-quiet", "b-quiet", "c-hot"])
    registry.clock.advance(240)
    report_readiness(registry, boxes[2][0])
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS, "devices": [_row(boxes[2][1], 999.0, 82)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "attention")
        expect(_summary(page)).to_have_text("2 Frames · 1 Player need attention")
        expect(_attention(page).get_by_role("listitem").last).to_contain_text(
            f"{boxes[2][2]} (Frame c-hot) — 82 °C · hot")


def test_a_player_incident_alone_makes_the_strip_an_alarm(page, registry):
    ((player_id, device, name),) = _frames_with_players(registry, [FRAME])
    report_readiness(registry, player_id)  # the Frame is heard: no Frame incident
    _stub(page, {"read_at": 1000.0, "thresholds": N1_THRESHOLDS, "devices": [_n1_row(device, 0x4)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "attention")
        expect(_summary(page)).to_have_text("1 Player needs attention")
        expect(_summary(page)).to_have_class(re.compile(r"\bhealth--alarm\b"))
        expect(_attention(page).get_by_role("listitem")).to_have_text(
            [re.compile(rf"^{re.escape(name)} \(Frame {FRAME}\) — throttled now")])


def test_a_failing_host_read_is_named_and_no_host_row_claims_health(page, registry):
    ((player_id, _, _),) = _frames_with_players(registry, [FRAME])
    report_readiness(registry, player_id)
    page.route(HOSTS, lambda route: route.fulfill(status=500, content_type="application/json",
                                                  body=json.dumps({"detail": "boom"})))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "attention")
        expect(_summary(page)).to_have_text("No Frame or Player needs attention · host health not read")
        expect(visible_page(page)).not_to_contain_text("Host Management")
        # The Status chip does not judge a box it could not read.
        inspector = open_frame(page, FRAME, "status")
        expect(inspector.get_by_role("link", name=re.compile(r"· host health not read$"))).to_have_attribute(
            "data-severity", "unknown")


def test_the_status_chip_names_the_worst_item_and_links_to_the_pis_hardware_page(page, registry):
    ((player_id, device, name),) = _frames_with_players(registry, [FRAME])
    report_readiness(registry, player_id)
    document = {"read_at": 1000.0, "thresholds": N1_THRESHOLDS, "devices": [_n1_row(device, 0x4)]}
    _stub(page, document)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        inspector = open_frame(page, FRAME, "status")
        chip = inspector.get_by_role("link", name=f"{name} · throttled now", exact=True)
        expect(chip).to_have_attribute("href", f"#/hardware/{device}")
        # Under the chip, the first line is the Frame's planned fact (console DDD §34, S1).
        expect(inspector.locator("p:has(> a[href^='#/hardware/']) + .facet__planned")).to_contain_text(
            "On top: nothing · no Run puts a layer on this Frame now (Central's Runs; the Panel "
            "is not observed)")
        document["devices"] = [_row(device, 880.0, 50)]
        page.reload()
        expect(inspector.get_by_role("link", name=f"{name} · Host Management silent 2 min", exact=True)
               ).to_be_visible()
        document["devices"] = [_row(device, 997.0, 50)]
        page.reload()
        chip = inspector.get_by_role("link", name=f"{name} · Host Management last reported 3 s ago", exact=True)
        expect(chip).to_be_visible()
        chip.click()
        expect(page.get_by_role("heading", level=2, name=name, exact=True)).to_be_visible()
        expect(page.get_by_role("heading", level=1, name="Hardware", exact=True)).to_be_visible()
