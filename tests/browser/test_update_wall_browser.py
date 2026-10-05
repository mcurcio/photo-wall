"""Fleet › Releases › Update the wall (console DDD Part E §25a, beads NU1 and B8) in the
production app.

Central is real for everything the journey's own judgement rests on: the release catalog and the
deployment Central ingests from it (filled the way the media worker fills it), Select's
compare-and-set, the Registry's Players, Frames and bindings, and their readiness (Frame
health). No media worker runs here, so the release's download readiness is set on Central's real
release read by the test (`Fleet.readiness`): Ready unless a test says otherwise. The node layer is a
stand-in (`Fleet`): each box's node read, its app operations read and the node writes are served
by the test, so a box can "reboot onto the selection" between two reads, deterministically. The
send rules themselves are Central's elsewhere (tests/browser/test_player_page_browser.py runs
Reboot and Stage against Central's real owners); here the evidence is the journey's ORDER: how
many requests it sends, and when.

Every assertion is behavioural (role, text, request count).
"""

import json
import os
import re
from itertools import count

import pytest
from console_tasks import connect, visit
from operator_harness import operator_server, reload_after, report_readiness
from playwright.sync_api import expect
from test_node_boot import cold_setup
from test_registry import enroll
from test_releases_browser import _catalog

from central.fleet.node_boot import NodeBootService
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.registry import FrameCreate
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OLD_APP = "11" * 32
OPEN = {"effective_state": "open", "state": "open", "generation": 7, "reason": None}
CLOSED = {"effective_state": "closed", "state": "closed", "generation": 6, "reason": "never_certified",
          "changed_at": 1_759_363_000}
JOURNEY = "#/releases/update/v9.0.0"
PUT_TITLE = "Put release v9.0.0 on the wall?"
REBOOT = "Put it on the wall and reboot"
NO_REBOOT = "No Player is rebooted; each gets it at its next boot."
KEEP_WORDS = "These are the Players this console knows. Select is fleet-wide: any other Pi that boots by node path is offered deployment"
GATE_CLOSED_KEEP = "Each Player is offered it at its next boot; this page cannot reboot them while the gate is closed."
TRIED_RUNNING = "App Effect Broker reported the staged app running"
SELECT_SCOPE = ("Every Player that boots by node path from now on is offered this deployment, including Players "
                "Central has not seen. Central cannot list which Players will boot.")


class Fleet:
    """The node layer's reads and writes for a few boxes, served from memory.

    Each box has a current boot (Host Management and App Effect Broker sessions on it), the app
    its Player app linked, its stored acceptances, its reboot commands and its app operations.
    `reboot(device)` is the box coming back on a new boot with the target app linked."""

    _ids = count(1)

    def __init__(self, page, devices, gate=OPEN, accepted=True):
        self.page, self.gate, self.target_app = page, gate, None
        self.readiness = {"readiness": "ready", "readiness_reason": None, "missing_bytes": 0}
        self.boxes = {device: {"boot": f"boot-{next(self._ids)}", "linked": OLD_APP,
                               "acceptances": [OLD_APP] if accepted else [], "commands": [], "operations": []}
                      for device in devices}
        self.reboots, self.stages, self.begins, self.samples = [], [], [], []
        page.route("**/v1/operator/node/status", lambda route: self._json(
            route, {"transport_enabled": True, "effect_gate": self.gate}))
        page.route(re.compile(r".*/v1/operator/node/devices/[^/]+(/[a-z-]+)?$"), self._device)
        page.route("**/v1/operator/node/app-qualifications/*/sample", self._sample)
        page.route("**/v1/operator/node/releases", self._releases)

    def _releases(self, route):
        """Central's real release read, with the download readiness this test sets."""
        response = route.fetch()
        body = response.json()
        for row in body["releases"]:
            if row["deployment_id"] is not None:
                row.update(self.readiness)
        route.fulfill(response=response, json=body)

    @staticmethod
    def _json(route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def _sessions(self, box):
        return [{"session_id": f"host-{box['boot']}", "current": True, "scope": "operator_reboot",
                 "command_eligible": True, "producer": {"owner": "host_core", "kernel_boot_id": box["boot"]}},
                {"session_id": f"broker-{box['boot']}", "current": True, "scope": "app_effect",
                 "producer": {"owner": "app_effect_broker", "kernel_boot_id": box["boot"]}}]

    def _device(self, route):
        request = route.request
        device, _, tail = request.url.split("/node/devices/", 1)[1].partition("/")
        box = self.boxes[device]
        if request.method == "GET" and tail == "":
            return self._json(route, {"device_id": device, "device_generation": 3, "read_at": 1_759_363_300,
                                      "sessions": self._sessions(box), "reboot_commands": box["commands"],
                                      "boot_claims": [], "deprecated_boot": None, "display_outputs": []})
        if request.method == "GET" and tail == "app-attempts":
            return self._json(route, {"device_id": device, "generation": 3, "read_at": 1_759_363_300,
                                      "operations": box["operations"], "qualification": {
                                          "linked_app": {"environment_sha256": box["linked"], "admitted_at": 1},
                                          "acceptances": [{"environment_sha256": sha, "base_content_key": "k" * 64,
                                                           "base_tag": "v9.0.0", "accepted_at": 2}
                                                          for sha in box["acceptances"]]}})
        body = request.post_data_json
        if tail == "reboots":
            self.reboots.append(device)
            box["commands"].insert(0, {"command_id": body["command_id"], "issued_at": 1_759_363_300,
                                       "expires_at": 1_759_363_330, "outstanding": True, "responses": [],
                                       "effects": [], "operator_audit_ref": body["operator_audit_ref"],
                                       "command": {"command_session_id": body["session_id"]}})
            return self._json(route, {"duplicate": False})
        if tail == "app-stages":
            self.stages.append(device)
            box["operations"].insert(0, {"operation_id": body["operation_id"], "command_id": body["command_id"],
                                         "operator_audit_ref": body["operator_audit_ref"], "state": "staged",
                                         "command_response": None, "latest_effect": None})
            return self._json(route, {"command": {}, "duplicate": False})
        if tail == "app-qualifications":
            self.begins.append(device)
            self.qualifying = device
            return self._json(route, {"duplicate": False})
        raise AssertionError(f"unexpected node request {request.method} {request.url}")

    def _sample(self, route):
        self.samples.append(route.request.url)
        self.boxes[self.qualifying]["acceptances"].append(self.boxes[self.qualifying]["linked"])
        self._json(route, {"status": "accepted", "accepted": True})

    def add(self, device):
        """A box that enrolled after the test began, on its first boot with the old app."""
        self.boxes[device] = {"boot": f"boot-{next(self._ids)}", "linked": OLD_APP, "acceptances": [OLD_APP],
                              "commands": [], "operations": []}

    def reboot(self, device):
        """The box restarts: a new boot, its earlier commands no longer outstanding, a stage ended,
        and its Player app enrolls again with Central (a new authority epoch) and reports readiness."""
        registry, key, player_id = _ENROLLED[device]
        assert enroll(registry, key, count=1, device_id=device)[0]["player_id"] == player_id
        report_readiness(registry, player_id)
        box = self.boxes[device]
        box["boot"], box["linked"] = f"boot-{next(self._ids)}", self.target_app
        for command in box["commands"]:
            command["outstanding"] = False
        for operation in box["operations"][:1]:
            operation["state"] = ("ended_by_later_boot" if operation["state"] in ("target_running", "fallback_running")
                                  else "interrupted_by_reboot")

    def run_stage(self, device):
        """The App Effect Broker switches the box to the staged app."""
        box = self.boxes[device]
        box["operations"][0]["state"] = "target_running"
        box["operations"][0]["latest_effect"] = {"phase": "running", "sequence": 4, "received_at": 1_759_363_310}
        box["linked"] = self.target_app


# device -> (registry, key, player): what a box needs to re-enroll its Player app on a new boot.
_ENROLLED = {}


def _add_player(registry, index):
    """One enrolled Player driving Frame `frame-<index>` and reporting readiness: (player, device)."""
    enrolled, key, _ = enroll(registry, count=1)
    player_id = enrolled["player_id"]
    frame = f"frame-{index}"
    registry.create_frame(FrameCreate(id=frame, surface_id="wall", x_mm=100 + 500 * index, y_mm=100, width_mm=400,
                                      height_mm=300, profile=FrameProfile(width_px=1920, height_px=1080,
                                                                          diagonal_inches=24)))
    registry.bind(frame, player_id, "HDMI-A-1", expected_generation=0)
    report_readiness(registry, player_id)
    device = next(p.device_id for p in registry.inventory().players if p.id == player_id)
    _ENROLLED[device] = (registry, key, player_id)
    return player_id, device


def _wall(registry, players=3):
    """`players` enrolled Players, each driving one Frame and reporting readiness: [(player, device)]."""
    return [_add_player(registry, index) for index in range(players)]


def _writes(page, method, fragment):
    sent = []
    page.on("request", lambda request: sent.append(request)
            if request.method == method and fragment in request.url else None)
    return sent


def _section(page, name):
    return page.get_by_role("region", name=name, exact=True)


def _put(page, label=REBOOT, says=None, double=False):
    """Put v9.0.0 on the wall: the ONE confirmation (Select's own R17 words, and what it costs),
    confirmed with `label` (double-clicked when `double`)."""
    _section(page, "Choose").or_(_section(page, "Look")).get_by_role(
        "button", name="Put v9.0.0 on the wall…", exact=True).click()
    dialog = page.get_by_role("dialog", name=PUT_TITLE)
    expect(dialog).to_contain_text(SELECT_SCOPE)  # Select's own R17 words (releases.js `selectionConfirmation`)
    for text in says or ():
        expect(dialog).to_contain_text(text)
    button = dialog.get_by_role("button", name=label, exact=True)
    if double:
        button.dblclick()
    else:
        button.click()
    return dialog


def _rolling(page):
    """The Put landed with the gate open: rolling has begun, with no second confirmation."""
    section = _section(page, "Put on the wall")
    expect(section).to_be_visible(timeout=10_000)
    expect(section.get_by_role("status")).to_have_text(
        re.compile(r"^Rebooting one at a time · 0 of \d+ Players on the selection$"))
    return section


def _settle(page, ms=5000, times=1):
    """Run the paused page clock (the 5 s active-row read) and let the answers land."""
    for _ in range(times):
        page.clock.run_for(ms)
        page.wait_for_timeout(250)


def _start(registry, monkeypatch):
    """The catalog's release (Central's real catalog and ingest) and a wall of three Players."""
    release, _ = _catalog(registry, monkeypatch)
    return release, _wall(registry)


def _journey(page, origin, registry, route=JOURNEY):
    connect(page, origin, "releases", paused_at=registry.clock.utc())
    visit(page, route)
    expect(page.get_by_role("heading", level=2, name="Update the wall with release v9.0.0")).to_be_visible()
    expect(_section(page, "Choose").or_(_section(page, "Put on the wall"))).to_be_visible(timeout=15_000)


def test_put_sends_one_put_then_one_reboot_at_a_time_each_after_the_previous_rejoins_and_undo_selects_previous(
        page, registry, monkeypatch):
    _, _, before = cold_setup(registry)  # the selection this Put replaces: Undo puts it back by id
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        puts = _writes(page, "PUT", "/boot-policy")
        _journey(page, origin, registry)
        expect(_section(page, "Choose")).to_contain_text(KEEP_WORDS)
        expect(_section(page, "Choose")).to_contain_text("Download: Ready")
        # ONE confirmation names the Players, the reboot cost and the readiness; a double click
        # starts one rollout: one PUT, one reboot.
        _put(page, says=["Each Frame a Player drives is blank while it reboots",
                         "Ready: Central has every file of v9.0.0."], double=True)
        keep = _rolling(page)
        assert len(puts) == 1
        _settle(page)
        assert len(fleet.reboots) == 1
        first = fleet.reboots[0]
        rows = keep.get_by_role("list", name="Players to keep it on", exact=True)
        expect(rows.get_by_role("listitem").nth(0)).to_contain_text("Requested · delivery unknown")
        _settle(page, times=4)  # 20 s of reads: the first Player has not rejoined, so nothing more is sent
        assert len(fleet.reboots) == 1
        fleet.reboot(first)
        _settle(page, times=2)
        expect(rows.get_by_role("listitem").nth(0)).to_contain_text("Rejoined")
        assert len(fleet.reboots) == 2 and fleet.reboots[1] != first
        _settle(page, times=3)
        assert len(fleet.reboots) == 2
        fleet.reboot(fleet.reboots[1])
        _settle(page, times=2)
        assert len(fleet.reboots) == 3
        fleet.reboot(fleet.reboots[2])
        _settle(page, times=2)
        expect(_section(page, "Done")).to_contain_text("Done · 3 of 3 Players on the selection")
        assert sorted(fleet.reboots) == sorted(device for _, device in wall)
        assert len(puts) == 1
        # Undo selects the previous selection's deployment directly, by id (Select alone).
        _section(page, "Done").get_by_role("button", name=re.compile("^Undo: put deployment")).click()
        undo = page.get_by_role("dialog", name=f"Put deployment {str(before.deployment_id)[:4]}… back for every boot?")
        expect(undo).to_contain_text(NO_REBOOT)
        undo.get_by_role("button", name="Select for every boot", exact=True).click()
        expect(undo).to_be_hidden()
        assert [put.post_data_json["deployment_id"] for put in puts][1:] == [str(before.deployment_id)]
        assert len(fleet.reboots) == 3


def test_a_reload_mid_rollout_sends_nothing_until_resume_and_rederives_the_rows(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        _put(page)
        _rolling(page)
        _settle(page)
        assert len(fleet.reboots) == 1
        reload_after(page, lambda: fleet.reboot(fleet.reboots[0]))  # it comes back while the page is away
        keep = _section(page, "Put on the wall")
        expect(keep.get_by_role("status")).to_have_text("Paused · 1 of 3 Players on the selection", timeout=10_000)
        expect(keep).to_contain_text("Paused means this page sends no more reboots. Select is fleet-wide")
        rows = keep.get_by_role("list", name="Players to keep it on", exact=True).get_by_role("listitem")
        expect(rows).to_have_count(3)
        expect(rows.filter(has_text="Rejoined")).to_have_count(1)
        expect(rows.filter(has_text="Waiting")).to_have_count(2)
        _settle(page, times=4)
        assert len(fleet.reboots) == 1
        # The reload forgot the frozen rollout: Resume first names the Players it will reboot.
        keep.get_by_role("button", name="Resume", exact=True).click()
        _settle(page, times=2)
        assert len(fleet.reboots) == 1
        dialog = page.get_by_role("dialog", name="Reboot these Players one at a time?")
        expect(dialog.get_by_role("list", name="Players this page reboots", exact=True).get_by_role("listitem")).to_have_count(3)
        dialog.get_by_role("button", name="Reboot one at a time", exact=True).click()
        _settle(page)
        assert len(fleet.reboots) == 2


def test_ten_minutes_without_rejoining_pauses_with_zero_further_posts(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        _put(page)
        _rolling(page)
        _settle(page)
        assert len(fleet.reboots) == 1
        page.clock.fast_forward(10 * 60 * 1000)
        _settle(page)
        keep = _section(page, "Put on the wall")
        expect(keep.get_by_role("status")).to_have_text("Paused · 0 of 3 Players on the selection", timeout=10_000)
        expect(keep).to_contain_text("has not come back on a new boot after 10 minutes")
        _settle(page, times=4)
        assert len(fleet.reboots) == 1


def test_try_samples_then_stages_then_looks_and_back_out_reboots_only_the_tried_player(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    tried, tried_device = wall[1]
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall], accepted=False)
        fleet.target_app = release.app_environment.environment_sha256
        puts = _writes(page, "PUT", "/boot-policy")
        _journey(page, origin, registry)
        _section(page, "Choose").get_by_role("link", name=re.compile(f"^Try on .*{tried_device}")).click()
        qualify = _section(page, "Qualify")
        expect(qualify).to_contain_text("needs a qualified fallback first")
        qualify.get_by_role("button", name=re.compile("^Begin qualifying app")).click()
        stage = _section(page, "Try on one Frame")
        expect(stage).to_be_visible(timeout=10_000)
        assert fleet.begins == [tried_device] and len(fleet.samples) >= 1
        expect(stage).to_contain_text("Applies to this boot only.")
        expect(stage).to_contain_text("then rejoins its Run at the current point")
        expect(stage).to_contain_text("A switch on a Frame-bound Player is proven on Central only")
        stage.get_by_role("button", name=re.compile("^Stage release v9.0.0 on")).click()
        expect(stage).to_contain_text("Staged; no response from App Effect Broker", timeout=10_000)
        assert fleet.stages == [tried_device]
        fleet.run_stage(tried_device)
        _settle(page)
        look = _section(page, "Look")
        expect(look).to_contain_text(TRIED_RUNNING, timeout=10_000)
        # A double click sends one reboot: Back out is held in flight from the first click.
        look.get_by_role("button", name=re.compile("^Back out: reboot")).dblclick()
        expect(_section(page, "Back out")).to_be_visible(timeout=10_000)
        assert fleet.reboots == [tried_device]
        fleet.reboot(tried_device)
        _settle(page)
        expect(_section(page, "Done")).to_contain_text("Backed out:", timeout=10_000)
        assert fleet.reboots == [tried_device] and puts == [] and fleet.stages == [tried_device]


def test_a_closed_gate_sends_no_stage_or_reboot_and_put_is_select_alone(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall], gate=CLOSED)
        fleet.target_app = release.app_environment.environment_sha256
        puts = _writes(page, "PUT", "/boot-policy")
        _journey(page, origin, registry)
        choose = _section(page, "Choose")
        expect(choose).to_contain_text("Try unavailable while the effect gate is closed.")
        expect(choose.get_by_role("link", name=re.compile("^Try on"))).to_have_count(0)
        expect(choose).to_contain_text(GATE_CLOSED_KEEP)
        dialog = _put(page, "Select for every boot", says=["Selects v9.0.0 for every boot.", NO_REBOOT,
                                                           "Ready: Central has every file of v9.0.0."])
        expect(dialog.get_by_role("list", name="Players this page reboots", exact=True)).to_have_count(0)
        keep = _section(page, "Put on the wall")
        expect(keep).to_contain_text(GATE_CLOSED_KEEP, timeout=10_000)
        expect(keep.get_by_role("button", name="Resume", exact=True)).to_have_count(0)
        _settle(page, times=4)
        assert len(puts) == 1 and fleet.reboots == [] and fleet.stages == []


def test_a_base_changing_target_withdraws_try_with_its_words(page, registry, monkeypatch):
    cold_setup(registry)  # a selection on another base
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        choose = _section(page, "Choose")
        expect(choose).to_contain_text("Try unavailable: This release changes the base, so it cannot be tried live; "
                                       "putting it on the wall reboots each Player onto it.")
        expect(choose.get_by_role("link", name=re.compile("^Try on"))).to_have_count(0)
        assert fleet.stages == []


def test_put_names_its_plan_and_a_player_skipped_in_choose_receives_no_reboot(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    skipped_device = wall[1][1]
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        puts = _writes(page, "PUT", "/boot-policy")
        _journey(page, origin, registry)
        choose = _section(page, "Choose")
        plan = choose.get_by_role("list", name="Players this page reboots, in order", exact=True).get_by_role("listitem")
        expect(plan).to_have_count(3)
        plan.filter(has_text=skipped_device).get_by_role("button", name=re.compile("^Skip ")).click()
        expect(plan.filter(has_text=skipped_device)).to_contain_text("· Skipped")
        choose.get_by_role("button", name="Put v9.0.0 on the wall…", exact=True).click()
        dialog = page.get_by_role("dialog", name=PUT_TITLE)
        named = dialog.get_by_role("list", name="Players this page reboots", exact=True).get_by_role("listitem")
        expect(named).to_have_count(2)
        expect(named.filter(has_text=skipped_device)).to_have_count(0)
        expect(dialog).to_contain_text("Skipped:")
        dialog.get_by_role("button", name=REBOOT, exact=True).click()
        _rolling(page)
        assert len(puts) == 1
        for _ in range(2):
            _settle(page)
            assert len(fleet.reboots) >= 1
            fleet.reboot(fleet.reboots[-1])
            _settle(page, times=2)
        expect(_section(page, "Done")).to_contain_text("Done · 2 of 3 Players on the selection", timeout=10_000)
        assert len(fleet.reboots) == 2 and skipped_device not in fleet.reboots


def test_after_try_put_reboots_the_tried_player_first_its_stage_is_not_the_selection(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    tried_device = wall[2][1]
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        _section(page, "Choose").get_by_role("link", name=re.compile(f"^Try on .*{tried_device}")).click()
        stage = _section(page, "Try on one Frame")
        stage.get_by_role("button", name=re.compile("^Stage release v9.0.0 on")).click()
        expect(stage).to_contain_text("Staged; no response from App Effect Broker", timeout=10_000)
        fleet.run_stage(tried_device)
        _settle(page)
        expect(_section(page, "Look")).to_contain_text(TRIED_RUNNING, timeout=10_000)
        _put(page)
        keep = _rolling(page)
        rows = keep.get_by_role("list", name="Players to keep it on", exact=True).get_by_role("listitem")
        expect(rows.nth(0)).to_contain_text(tried_device)
        _settle(page)
        assert fleet.reboots == [tried_device]


def test_a_player_enrolled_after_put_is_not_in_the_rollout_and_gets_zero_reboots(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        _put(page)
        keep = _rolling(page)
        _, late_device = _add_player(registry, 3)  # enrolls after the Keep confirmation named three
        fleet.add(late_device)
        rows = keep.get_by_role("list", name="Players to keep it on", exact=True).get_by_role("listitem")
        _settle(page, times=2)
        expect(rows).to_have_count(4)
        expect(rows.filter(has_text=late_device)).to_contain_text("Not in this rollout")
        for _ in range(3):
            _settle(page)
            assert fleet.reboots and late_device not in fleet.reboots
            fleet.reboot(fleet.reboots[-1])
            _settle(page, times=2)
        expect(_section(page, "Done")).to_contain_text("Done · 3 of 3 Players on the selection", timeout=10_000)
        _settle(page, times=3)
        assert len(fleet.reboots) == 3 and late_device not in fleet.reboots


def test_a_player_skipped_before_a_reload_gets_zero_reboots_after_resume(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    skipped_device = wall[0][1]
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        choose = _section(page, "Choose")
        plan = choose.get_by_role("list", name="Players this page reboots, in order", exact=True).get_by_role("listitem")
        plan.filter(has_text=skipped_device).get_by_role("button", name=re.compile("^Skip ")).click()
        expect(plan.filter(has_text=skipped_device)).to_contain_text("· Skipped")
        assert "/skip/" in page.evaluate("window.location.hash")  # an operator choice: in the URL
        _put(page)
        _rolling(page)
        _settle(page)
        assert len(fleet.reboots) == 1 and skipped_device not in fleet.reboots
        reload_after(page, lambda: fleet.reboot(fleet.reboots[0]))
        keep = _section(page, "Put on the wall")
        expect(keep.get_by_role("status")).to_have_text("Paused · 1 of 3 Players on the selection", timeout=10_000)
        rows = keep.get_by_role("list", name="Players to keep it on", exact=True).get_by_role("listitem")
        expect(rows.filter(has_text=skipped_device)).to_contain_text("Skipped")
        keep.get_by_role("button", name="Resume", exact=True).click()
        dialog = page.get_by_role("dialog", name="Reboot these Players one at a time?")
        named = dialog.get_by_role("list", name="Players this page reboots", exact=True).get_by_role("listitem")
        expect(named).to_have_count(2)
        expect(named.filter(has_text=skipped_device)).to_have_count(0)
        dialog.get_by_role("button", name="Reboot one at a time", exact=True).click()
        _settle(page)
        assert len(fleet.reboots) == 2
        fleet.reboot(fleet.reboots[-1])
        _settle(page, times=2)
        expect(_section(page, "Done")).to_contain_text("Done · 2 of 3 Players on the selection", timeout=10_000)
        _settle(page, times=3)
        assert len(fleet.reboots) == 2 and skipped_device not in fleet.reboots


DECODE_RECOVERY = ("The Player could not decode this assignment. Check that the media is supported, "
                   "or choose another item.")


def test_a_rebooted_player_with_a_readiness_failure_is_not_rejoined_and_gets_zero_further_reboots(
        page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    frames = {device: (player, f"frame-{index}") for index, (player, device) in enumerate(wall)}
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        failing = []

        def snapshot(route):
            # Central's real snapshot, plus a current readiness failure for each failing Frame.
            response = route.fetch(headers=route.request.headers)
            body = response.json()
            body["readiness_diagnostics"] = [{
                "player_id": player, "authority_epoch": 1, "sequence": 1, "plan_id": "plan-a", "revision": 1,
                "assignment_id": "assignment-a", "frame_id": frame, "output_id": "HDMI-A-1",
                "binding_generation": 1, "failure_code": "decode", "received_at": body["read_at"] - 1,
                "observed_at": body["read_at"] - 1, "layer_start": body["read_at"] - 1,
                "layer_end": body["read_at"] + 60} for player, frame in failing]
            route.fulfill(response=response, json=body)

        _journey(page, origin, registry)
        page.route("**/v1/operator/snapshot", snapshot)  # after sign-in: route.fetch carries the session
        _put(page)
        keep = _rolling(page)
        _settle(page)
        assert len(fleet.reboots) == 1
        first = fleet.reboots[0]
        failing.append(frames[first])
        fleet.reboot(first)  # a later boot, linking the target's app, but it cannot decode its assignment
        _settle(page, times=2)
        expect(keep.get_by_role("status")).to_have_text("Paused · 0 of 3 Players on the selection", timeout=10_000)
        rows = keep.get_by_role("list", name="Players to keep it on", exact=True).get_by_role("listitem")
        expect(rows.filter(has_text=first)).to_contain_text("Not rejoined: it reports a readiness failure.")
        expect(keep).to_contain_text(DECODE_RECOVERY)
        _settle(page, times=4)
        assert fleet.reboots == [first]


def test_the_first_reboot_waits_for_centrals_download_then_rolls(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        fleet.readiness = {"readiness": "downloading", "readiness_reason": None, "missing_bytes": 1_500_000_000}
        _journey(page, origin, registry)
        expect(_section(page, "Choose")).to_contain_text("Download: Downloading · 1.5 GB left")
        _put(page, says=["Downloading: Central still has 1.5 GB of v9.0.0 to download",
                         "The first reboot waits until Central has downloaded it."])
        keep = _rolling(page)
        expect(keep).to_contain_text("Waiting for Central to download v9.0.0: 1.5 GB left")
        _settle(page, times=4)
        assert fleet.reboots == []
        fleet.readiness = {"readiness": "ready", "readiness_reason": None, "missing_bytes": 0}
        page.clock.run_for(30_000)  # the next release read
        _settle(page, times=2)
        assert len(fleet.reboots) == 1


def test_a_lost_put_answer_starts_rolling_only_once_a_read_shows_it_selected(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)

    def lost(route):
        route.fetch()  # Central records it; the answer never reaches the page
        route.fulfill(status=502, body="bad gateway")

    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        page.route("**/v1/operator/node/boot-policy", lost)
        dialog = _put(page)
        expect(dialog.get_by_role("status")).to_contain_text("Outcome unknown: Central did not answer.")
        dialog.get_by_role("button", name="Close", exact=True).click()
        # The read after the lost answer shows the target selected: rolling starts, one reboot.
        _rolling(page)
        _settle(page)
        assert len(fleet.reboots) == 1


def test_a_refused_put_starts_no_rolling(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        page.route("**/v1/operator/node/boot-policy", lambda route: route.fulfill(
            status=422, content_type="application/json", body='{"error": "mystery_code"}'))
        dialog = _put(page)
        expect(dialog.get_by_role("alert")).to_have_text("Central refused: mystery_code.")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        _settle(page, times=4)
        expect(_section(page, "Choose")).to_be_visible()
        # Another page selects it meanwhile: this page shows Paused and sends no reboot, because
        # no Put of its own landed (no confirmation here started a rollout).
        with registry.db.transaction() as conn:
            [deployment] = [row["deployment_id"] for row in conn.execute("SELECT deployment_id FROM node_deployments")]
        NodeBootService(NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test"))).select(deployment, 0)
        page.clock.run_for(30_000)  # the next release read
        keep = _section(page, "Put on the wall")
        expect(keep.get_by_role("status")).to_have_text("Paused · 0 of 3 Players on the selection", timeout=10_000)
        _settle(page, times=4)
        assert fleet.reboots == []
