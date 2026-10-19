"""Fleet › Releases › Update the wall (console DDD Part E §25a, beads NU1 and B8) in the
production app.

Central is real for the Registry's Players, Frames and bindings and their readiness (Frame
health), and for the deployment Central ingests from the release catalog (filled the way the media
worker fills it), which Try stages. Central's Releases read and Update the wall's PUT (a version per
Player, Q2 A 2026-10-16) are the central lane's; until they land here, `Fleet` serves them with the
contract's shapes (ReleasesView, VersionChoice), the target's download readiness set by the test
(`Fleet.readiness`): Ready unless a test says otherwise. The node layer is a stand-in too: each
box's node read, its app operations read and the node writes are served by the test, so a box can
"reboot onto its version" between two reads, deterministically. The
send rules themselves are Central's elsewhere (tests/browser/test_player_page_browser.py runs
Reboot and Stage against Central's real owners); here the evidence is the journey's ORDER: how
many requests it sends, and when.

Every assertion is behavioural (role, text, request count). Mutation probe: Update the wall that
calls `installOnPlayer` per Player -> the main case's "no POST install" fails.
"""

import json
import os
import re
from itertools import count

import pytest
from console_tasks import connect, visit
from operator_harness import operator_server, reload_after, report_readiness, run_page_clock
from playwright.sync_api import expect
from support.frames import frame_create
from test_registry import enroll
from test_releases_browser import _catalog

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OLD_APP = "11" * 32
JOURNEY = "#/releases/update/v9.0.0"
PUT_TITLE = "Put release v9.0.0 on the wall?"
REBOOT = "Put it on the wall and reboot"
NO_REBOOT = "No Player is rebooted; each gets it at its next boot."
WALL_SCOPE = ("Every Player Central knows boots this release from its next boot on. A Player that enrols later boots "
              "the newest ready release until it is given one.")
KEEP_WORDS = f"These are the Players this console knows. {WALL_SCOPE}"
TRIED_RUNNING = "App Effect Broker reported the staged app running"
OLD = "v8.0.0"


class Fleet:
    """The node layer's reads and writes for a few boxes, served from memory.

    Each box has a current boot (Host Management and App Effect Broker sessions on it), the app
    its Player app linked, its stored acceptances, its reboot commands and its app operations.
    `reboot(device)` is the box coming back on a new boot with the target app linked."""

    _ids = count(1)

    def __init__(self, page, devices, accepted=True):
        self.page, self.target_app = page, None
        self.readiness = {"readiness": "ready", "readiness_reason": None, "missing_bytes": 0}
        # Central's Releases read, a version per Player: every Player on OLD, on the target's base
        # unless `old_base` says otherwise. `walls`: each Update the wall PUT's version;
        # `installs`: each Install POST (Update the wall sends none).
        self.base, self.old_base = "v1.0.0", "v1.0.0"
        self.players = {device: {"version": OLD, "previous": None} for device in devices}
        self.walls, self.installs, self.wall_answer = [], [], None
        self.deployments = {}  # version -> the deployment Central's real Releases read says it became
        self.boxes = {device: {"boot": f"boot-{next(self._ids)}", "linked": OLD_APP,
                               "acceptances": [OLD_APP] if accepted else [], "commands": [], "operations": []}
                      for device in devices}
        self.reboots, self.stages, self.begins, self.samples = [], [], [], []
        page.route(re.compile(r".*/v1/operator/node/devices/[^/]+(/[a-z-]+)?$"), self._device)
        page.route("**/v1/operator/node/app-qualifications/*/sample", self._sample)
        page.route("**/v1/operator/releases", self._view)
        page.route("**/v1/operator/releases/wall", self._wall)
        page.route("**/v1/operator/players/*/install", self._install)

    def view(self):
        """Central's ReleasesView: the target, the release every Player booted before, each Player's version."""
        release = lambda version, base, app, extra: {  # noqa: E731
            "version": version, "stable": True, "problem": None, "deployment_id": self.deployments.get(version),
            "base_tag": base, "app_environment_sha256": app,
            "in_window": True, "discovered_at": 1_759_363_000, "readiness": "ready", "readiness_reason": None,
            "missing_bytes": 0, **extra}
        return {"read_at": 1_759_363_300, "default": "v9.0.0",
                "releases": [release("v9.0.0", self.base, self.target_app, self.readiness),
                             release(OLD, self.old_base, OLD_APP, {})],
                "players": [{"serial": device, "player_id": None, "name": device, "version": entry["version"],
                             "chosen": True, "previous": entry["previous"], "install": None, "running": None}
                            for device, entry in self.players.items()]}

    def put_all(self, version):
        """Every Player's version becomes `version` (Update the wall, here or on another page)."""
        for entry in self.players.values():
            if entry["version"] != version:
                entry["previous"], entry["version"] = entry["version"], version

    def _view(self, route):
        """Central's real Releases read names the deployment each release became (Try stages it);
        the Players' versions and the target's readiness are this test's."""
        real = route.fetch().json()
        self.deployments.update({row["version"]: row["deployment_id"] for row in real["releases"]})
        self._json(route, self.view())

    def _wall(self, route):
        version = route.request.post_data_json["version"]
        self.walls.append(version)
        if self.wall_answer is not None and self.wall_answer[0] != 502:
            return self._json(route, {"error": self.wall_answer[1]}, self.wall_answer[0])
        self.put_all(version)
        if self.wall_answer is not None:  # recorded, but the answer never reaches the page
            return route.fulfill(status=502, body="bad gateway")
        self._json(route, self.view())

    def _install(self, route):
        self.installs.append(route.request.url)
        self._json(route, {"error": "unknown_player"}, 404)

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
                                      "boot_claims": [], "display_outputs": []})
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
        """A box that enrolled after the test began, on its first boot with the old app; Central gives
        it the wall's version (a Player with none boots the newest ready release)."""
        self.players[device] = {"version": "v9.0.0", "previous": None}
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
    registry.create_frame(frame_create(frame, x_mm=100 + 500 * index, y_mm=100))
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
    """Put v9.0.0 on the wall: the ONE confirmation (every Player's version, and what it costs),
    confirmed with `label` (double-clicked when `double`)."""
    _section(page, "Choose").or_(_section(page, "Look")).get_by_role(
        "button", name="Put v9.0.0 on the wall…", exact=True).click()
    dialog = page.get_by_role("dialog", name=PUT_TITLE)
    expect(dialog).to_contain_text(WALL_SCOPE)  # releases.js `WALL_SCOPE`
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
        re.compile(r"^Rebooting one at a time · 0 of \d+ Players on this release$"))
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


def test_put_sends_one_put_then_one_reboot_at_a_time_each_after_the_previous_rejoins_and_undo_puts_previous(
        page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        expect(_section(page, "Choose")).to_contain_text(KEEP_WORDS)
        expect(_section(page, "Choose")).to_contain_text("Download: Ready")
        # ONE confirmation names the Players, the reboot cost and the readiness; a double click
        # starts one rollout: one PUT, one reboot.
        _put(page, says=["Each Frame a Player drives is blank while it reboots",
                         "Ready: Central has every file of v9.0.0."], double=True)
        keep = _rolling(page)
        assert fleet.walls == ["v9.0.0"]
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
        expect(_section(page, "Done")).to_contain_text("Done · 3 of 3 Players on this release")
        assert sorted(fleet.reboots) == sorted(device for _, device in wall)
        assert fleet.walls == ["v9.0.0"] and fleet.installs == []  # one PUT for the wall, no Install
        # Undo puts every Player back on the release they all booted before, with no reboot.
        _section(page, "Done").get_by_role("button", name=f"Undo: put release {OLD} back…", exact=True).click()
        undo = page.get_by_role("dialog", name=f"Put release {OLD} back on every Player?")
        expect(undo).to_contain_text(NO_REBOOT)
        undo.get_by_role("button", name=f"Put {OLD} back", exact=True).click()
        expect(undo).to_be_hidden()
        assert fleet.walls == ["v9.0.0", OLD] and fleet.installs == []
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
        expect(keep.get_by_role("status")).to_have_text("Paused · 1 of 3 Players on this release", timeout=10_000)
        expect(keep).to_contain_text("Paused means this page sends no more reboots. Every Player already boots v9.0.0")
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
        expect(keep.get_by_role("status")).to_have_text("Paused · 0 of 3 Players on this release", timeout=10_000)
        expect(keep).to_contain_text("has not come back on a new boot after 10 minutes")
        _settle(page, times=4)
        assert len(fleet.reboots) == 1


def test_try_samples_then_stages_then_looks_and_back_out_reboots_only_the_tried_player(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    tried, tried_device = wall[1]
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall], accepted=False)
        fleet.target_app = release.app_environment.environment_sha256
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
        assert fleet.reboots == [tried_device] and fleet.walls == [] and fleet.stages == [tried_device]


def test_a_base_changing_target_withdraws_try_with_its_words(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        fleet.old_base = "v0.9.0"  # every Player boots a release on another base
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
        assert fleet.walls == ["v9.0.0"]
        for _ in range(2):
            _settle(page)
            assert len(fleet.reboots) >= 1
            fleet.reboot(fleet.reboots[-1])
            _settle(page, times=2)
        expect(_section(page, "Done")).to_contain_text("Done · 2 of 3 Players on this release", timeout=10_000)
        assert len(fleet.reboots) == 2 and skipped_device not in fleet.reboots


def test_after_try_put_reboots_the_tried_player_first_its_stage_is_not_its_version(page, registry, monkeypatch):
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
        expect(_section(page, "Done")).to_contain_text("Done · 3 of 3 Players on this release", timeout=10_000)
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
        expect(keep.get_by_role("status")).to_have_text("Paused · 1 of 3 Players on this release", timeout=10_000)
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
        expect(_section(page, "Done")).to_contain_text("Done · 2 of 3 Players on this release", timeout=10_000)
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
        expect(keep.get_by_role("status")).to_have_text("Paused · 0 of 3 Players on this release", timeout=10_000)
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
        run_page_clock(page, 30_000)  # the next release read
        _settle(page, times=2)
        assert len(fleet.reboots) == 1


def test_a_lost_put_answer_starts_rolling_only_once_a_read_shows_every_player_on_it(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        fleet.wall_answer = (502, None)  # Central records it; the answer never reaches the page
        dialog = _put(page)
        expect(dialog.get_by_role("status")).to_contain_text("Outcome unknown: Central did not answer.")
        dialog.get_by_role("button", name="Close", exact=True).click()
        # The read after the lost answer shows every Player on the target: rolling starts, one reboot.
        _rolling(page)
        _settle(page)
        assert len(fleet.reboots) == 1


def test_a_refused_put_starts_no_rolling(page, registry, monkeypatch):
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        fleet.wall_answer = (409, "not_deployable")
        dialog = _put(page)
        expect(dialog.get_by_role("alert")).to_have_text(
            "Central cannot put this release on a Player: its upload was refused.")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        _settle(page, times=4)
        expect(_section(page, "Choose")).to_be_visible()
        # Another page puts it on the wall meanwhile: this page shows Paused and sends no reboot,
        # because no Put of its own landed (no confirmation here started a rollout).
        fleet.put_all("v9.0.0")
        run_page_clock(page, 30_000)  # the next release read
        keep = _section(page, "Put on the wall")
        expect(keep.get_by_role("status")).to_have_text("Paused · 0 of 3 Players on this release", timeout=10_000)
        _settle(page, times=4)
        assert fleet.reboots == []
