"""Hardware, the design-system tracer (Console by Domain E1 U1; console design system DS1).

The Hardware list's groups and their order (Driving a Frame worst first, then Not driving a
Frame, then Retired), the Pi focus that lives only in the address (design rule H2: `?pi=` narrows
the list, its chip clears it, and leaving the list ends it), and a Pi's Hardware page, whose
Reboot and Retire still go through their confirmation dialogs.

The production app on the loopback harness. The list's host values come from a stubbed fleet
host read (as tests/browser/test_host_health_browser.py does), so the tiers are exact; the Pi
page runs against real node records and Central's real reboot owner behind an open effect gate
(as tests/browser/test_player_page_browser.py does).
"""

import os
import re

import pytest
from console_tasks import connect, current_hash, go, hardware_list, open_pi, player_name, visit
from operator_harness import operator_server
from playwright.sync_api import expect
from test_fleet_attempts import SERIAL
from test_host_health_browser import THRESHOLDS, _frames_with_players, _row, _stub
from test_player_page_browser import NAME, Box, _gated_server, _open_gate
from test_registry import enroll

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

GROUPS = ("Driving a Frame", "Not driving a Frame", "Retired")


def _names(page, group):
    """The Pis' names down one group's table, in order."""
    return hardware_list(page).get_by_role("table", name=group, exact=True).locator(
        "tbody th > a:first-child").all_inner_texts()


def _fleet(registry):
    """Three Bound Pis (ok, alarm, notice), one Unbound and one retired; the stubbed host read
    judges the Bound ones. Their device ids are fixed so that name order, the order the list
    receives its rows in, is the reverse of worst-first (ok, notice, alarm): a list that did
    not sort would fail. Returns their names and device ids."""
    (_, ok, ok_name), (_, alarm, alarm_name), (_, notice, notice_name) = _frames_with_players(
        registry, ["f-ok", "f-alarm", "f-notice"],
        device_ids=["device-a-ok", "device-c-alarm", "device-b-notice"])
    spare_identity, _, spare_request = enroll(registry, count=1)
    retired_identity, _, retired_request = enroll(registry, count=1)
    registry.retire(retired_identity["player_id"])
    names = {"ok": ok_name, "alarm": alarm_name, "notice": notice_name,
             "spare": player_name(registry, spare_identity["player_id"]),
             "retired": player_name(registry, retired_identity["player_id"])}
    devices = {"ok": ok, "alarm": alarm, "notice": notice, "spare": spare_request.device_id,
               "retired": retired_request.device_id}
    return names, devices


def test_the_list_groups_pis_worst_first_and_a_focus_narrows_and_clears(page, registry):
    names, devices = _fleet(registry)
    _stub(page, {"read_at": 1000.0, "thresholds": THRESHOLDS, "devices": [
        _row(devices["ok"], 999.0, 50), _row(devices["alarm"], 999.0, 81), _row(devices["notice"], 999.0, 76),
        _row(devices["spare"], 999.0, 95)]})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "hardware")
        listing = hardware_list(page)
        # Groups in order, each its own table; the Bound Pis worst first, though registered
        # ok, alarm, notice.
        expect(listing.get_by_role("table")).to_have_count(3)
        titles = listing.get_by_role("table").evaluate_all("(all) => all.map((t) => t.ariaLabel)")
        assert titles == list(GROUPS)
        assert _names(page, "Driving a Frame") == [names["alarm"], names["notice"], names["ok"]]
        assert _names(page, "Not driving a Frame") == [names["spare"]]
        assert _names(page, "Retired") == [names["retired"]]
        # A Pi not driving a Frame is never judged (G2), however hot it reports.
        spare = listing.get_by_role("row").filter(has=page.get_by_role("link", name=names["spare"], exact=True))
        expect(spare).to_have_attribute("data-severity", "none")
        expect(spare).not_to_contain_text("hot")
        expect(page.get_by_role("group", name="Focus", exact=True)).to_have_count(0)

        # Focus on one Pi: the address carries it, the chip names it, every group narrows.
        listing.get_by_role("link", name=f"Focus on {names['notice']}", exact=True).click()
        assert current_hash(page) == f"#/hardware?pi={devices['notice']}"
        chip = page.get_by_role("group", name="Focus", exact=True)
        expect(chip).to_contain_text(f"Focused on {names['notice']}")
        expect(listing.get_by_role("table")).to_have_count(1)
        assert _names(page, "Driving a Frame") == [names["notice"]]
        for other in ("ok", "alarm", "spare", "retired"):
            expect(listing.get_by_role("link", name=names[other], exact=True)).to_have_count(0)

        # The chip clears it.
        chip.get_by_role("link", name="Show every Pi", exact=True).click()
        assert current_hash(page) == "#/hardware"
        expect(page.get_by_role("group", name="Focus", exact=True)).to_have_count(0)
        expect(listing.get_by_role("table")).to_have_count(3)
        assert _names(page, "Driving a Frame") == [names["alarm"], names["notice"], names["ok"]]

        # A typed address focuses too; leaving the list ends the focus (H2: no state beside
        # the address).
        visit(page, f"#/hardware?pi={devices['retired']}")
        expect(page.get_by_role("group", name="Focus", exact=True)).to_contain_text(
            f"Focused on {names['retired']}")
        expect(listing.get_by_role("table", name="Retired", exact=True)).to_be_visible()
        go(page, "wall")
        go(page, "hardware")
        assert current_hash(page) == "#/hardware"
        expect(page.get_by_role("group", name="Focus", exact=True)).to_have_count(0)
        expect(listing.get_by_role("table")).to_have_count(3)

        # A focus on a Pi Central does not know says so, and still clears.
        visit(page, "#/hardware?pi=device-unknown")
        expect(listing).to_contain_text("Pi device-unknown is not known to Central.")
        expect(listing.get_by_role("table")).to_have_count(0)


def test_the_pi_page_reboots_and_retires_only_through_their_confirmation_dialogs(page, registry):
    box = Box(registry)
    box.host_sample()
    _open_gate(registry)
    with _gated_server(registry) as origin:
        writes = []
        page.on("request", lambda request: writes.append(request.url)
                if request.method == "POST" and re.search(r"/(reboots|retire)$", request.url) else None)
        connect(page, origin)
        pi = open_pi(page, NAME)
        # The Pi header links to the Pi's other page, and back to the list.
        expect(pi.get_by_role("link", name="Software and screens", exact=True)).to_have_attribute(
            "href", re.compile(r"^#/players/device-"))
        expect(pi.get_by_role("link", name="All Pis", exact=True)).to_have_attribute("href", "#/hardware")

        # Reboot opens its dialog; Cancel sends nothing.
        reboot = pi.get_by_role("region", name="Reboot", exact=True)
        reboot.get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True)
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).to_have_count(0)

        # Retire opens its dialog; it is sent only once the handle is typed and confirmed.
        retire = pi.get_by_role("region", name="Danger zone", exact=True).get_by_role(
            "button", name=f"Retire player {box.player_id}", exact=True)
        retire.click()
        dialog = page.get_by_role("dialog")
        confirm = dialog.get_by_role("button", name="Confirm retire", exact=True)
        expect(confirm).to_be_disabled()
        page.wait_for_timeout(200)
        assert writes == [], "a write was sent before its dialog was confirmed"
        handle = SERIAL[-6:]  # a netbooted Pi's handle is its serial's (ConfirmAction.jsx)
        dialog.get_by_label(f"Type {handle} to confirm", exact=True).fill(handle)
        confirm.click()
        expect(pi).to_contain_text("Standing: Retired")
        assert [url for url in writes if url.endswith("/retire")], writes
        assert not [url for url in writes if url.endswith("/reboots")], writes
        assert registry.inventory().players[0].retired_at is not None
