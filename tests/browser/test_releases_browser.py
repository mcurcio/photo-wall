"""Fleet › Releases: a version per Player (Q2 A, 2026-10-16) in the production app.

Central serves the console, the session and Check GitHub releases now. Its Releases read and
Install (GET /v1/operator/releases, POST /v1/operator/players/{serial}/install) are the central
lane's; until they land here, `Releases` serves them from memory with the contract's shapes
(ReleasesView, VersionChoice, InstallStarted, InstallRefusal). It proves: every release is listed
with its readiness, Rejected and pre-release facts and the Players that boot it next; one row per
Player with its version, "newest ready" (the default) when Central keeps none for it, its previous,
an install's progress and what its host reported running; Install on one Player sends exactly one
POST for that Player's serial with the chosen version and shows the install's progress; each
refusal reads its own sentence; Check GitHub releases now sends one POST while unanswered and
claims no release.

Every assertion is behavioural (role, text, request count). Mutation probes: Install that posts
for every Player -> the one-request case fails; a refusal said with its code -> the refusal case
fails.
"""

import json
import os
import re
import threading
from hashlib import sha256

import pytest
from console_tasks import connect
from operator_harness import RequestGate, operator_server
from playwright.sync_api import expect
from test_node_release_catalog import discover, publication

from central.origins.github import GitHubReleaseOrigin
from contracts.node_release import encode_node_release

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

PAUSED_AT = 1_759_363_200  # the page clock: release reads run only when the test runs it
APP = "9f8e7d" + "0" * 58


def _catalog(registry, monkeypatch):
    """One GitHub release with its app, observed and ingested into its deployment as the release
    sync does it (Try stages that deployment). Returns (release, its manifest digest)."""
    origin, release, _ = publication()
    monkeypatch.setattr(GitHubReleaseOrigin, "from_env", classmethod(lambda cls, env=None: origin))
    # Playwright's event loop runs on this thread, so the worker-side discovery runs on another.
    worker = threading.Thread(target=discover, args=(registry, origin))
    worker.start()
    worker.join(timeout=30)
    return release, sha256(encode_node_release(release)).hexdigest()


def _release(version, **extra):
    return {"version": version, "stable": True, "problem": None, "base_tag": version, "app_environment_sha256": APP,
            "in_window": True, "readiness": "ready", "readiness_reason": None, "missing_bytes": 0,
            "discovered_at": PAUSED_AT - 600, **extra}


def _player(serial, name, **extra):
    return {"serial": serial, "player_id": f"player-{serial}", "name": name, "version": "v9.0.0", "chosen": True,
            "previous": "v8.0.0", "install": None, "running": "v9.0.0", **extra}


class Releases:
    """Central's Releases read and Install, served from memory with the contract's shapes."""

    def __init__(self, page):
        self.view = {
            "read_at": PAUSED_AT, "default": "v9.0.0",
            "releases": [
                _release("v9.1.0-rc.1", stable=False, readiness="not_downloaded", missing_bytes=1_200_000_000),
                _release("v9.0.0"),
                _release("v8.0.0", readiness="downloading", missing_bytes=2_500_000_000),
                _release("v7.5.0", readiness="failed", readiness_reason="cache_disk_full"),
                _release("v7.0.0", problem="node_release_identity_conflict", base_tag=None,
                         app_environment_sha256=None, readiness=None, in_window=False)],
            "players": [_player("SER-HALL", "Hall"),
                        _player("SER-LOBBY", "Lobby", version="v9.0.0", chosen=False, previous=None, running=None),
                        _player("SER-STAIRS", "Stairs", version="v8.0.0", previous="v9.0.0", running="v8.0.0")]}
        self.installs, self.refusal = [], None
        page.route("**/v1/operator/releases", lambda route: self._json(route, self.view))
        page.route("**/v1/operator/players/*/install", self._install)

    @staticmethod
    def _json(route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def _install(self, route):
        serial = route.request.url.split("/players/", 1)[1].split("/", 1)[0]
        version = route.request.post_data_json["version"]
        self.installs.append((serial, version))
        if self.refusal is not None:
            return self._json(route, {"error": self.refusal[1]}, self.refusal[0])
        player = next(each for each in self.view["players"] if each["serial"] == serial)
        player.update(previous=player["version"], version=version, chosen=True, install="downloading")
        self._json(route, {"kind": "started", "serial": serial, "version": version, "install": "downloading"})


def _writes(page, method, fragment):
    """Every request with `method` whose URL contains `fragment`, as the page sends it."""
    sent = []
    page.on("request", lambda request: sent.append(request)
            if request.method == method and fragment in request.url else None)
    return sent


def _listed(page, version):
    return page.get_by_role("list", name="Release catalog", exact=True).get_by_role(
        "listitem", name=f"Release {version}", exact=True)


def _player_row(page, name):
    return page.get_by_role("list", name="Players", exact=True).get_by_role("listitem", name=name, exact=True)


def test_releases_lists_each_release_and_one_row_per_player_with_the_default(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        Releases(page)
        connect(page, origin, "releases", paused_at=PAUSED_AT)
        newest = _listed(page, "v9.0.0")
        expect(newest).to_contain_text("Download: Ready · not proof a Player holds it")
        expect(newest).to_contain_text("Newest ready: a Player with no version of its own boots it")
        expect(newest).to_contain_text("Booted next by Hall, Lobby")
        expect(newest.get_by_role("link", name="Put v9.0.0 on the wall…", exact=True)).to_have_attribute(
            "href", "#/releases/update/v9.0.0")
        expect(_listed(page, "v9.1.0-rc.1")).to_contain_text("Pre-release: listed, not downloaded ahead")
        expect(_listed(page, "v9.1.0-rc.1")).to_contain_text("Download: Not downloaded")
        expect(_listed(page, "v8.0.0")).to_contain_text("Download: Downloading · 2.5 GB left")
        expect(_listed(page, "v7.5.0")).to_contain_text("Download: Failed: cache disk full")
        rejected = _listed(page, "v7.0.0")
        expect(rejected).to_contain_text("Download: Rejected: node release identity conflict")
        expect(rejected.get_by_role("link", name=re.compile("on the wall"))).to_have_count(0)
        # The default Update the wall opens on the newest ready release, past the newer pre-release.
        expect(page.get_by_role("link", name="Update the wall…", exact=True)).to_have_attribute(
            "href", "#/releases/update/v9.0.0")

        expect(page.get_by_role("list", name="Players", exact=True).get_by_role("listitem")).to_have_count(3)
        hall = _player_row(page, "Hall")
        expect(hall).to_contain_text("Hall · boots v9.0.0")
        expect(hall).to_contain_text("Previous: v8.0.0")
        expect(hall).to_contain_text("Running: Base v9.0.0")
        lobby = _player_row(page, "Lobby")
        expect(lobby).to_contain_text("Lobby · boots v9.0.0 · newest ready")
        expect(lobby).to_contain_text("Running: Not reported on this boot")
        expect(lobby).not_to_contain_text("Previous:")
        expect(_player_row(page, "Stairs")).to_contain_text("Stairs · boots v8.0.0")
        # No wall-wide selection is left anywhere on the page.
        expect(page.get_by_role("region", name="Boot selection", exact=True)).to_have_count(0)
        expect(page.get_by_role("region", name="Deployments", exact=True)).to_have_count(0)


def test_install_sends_exactly_one_post_for_that_player_and_shows_its_progress(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        releases = Releases(page)
        posts = _writes(page, "POST", "/install")
        connect(page, origin, "releases", paused_at=PAUSED_AT)
        stairs = _player_row(page, "Stairs")
        stairs.get_by_role("combobox", name="Release for Stairs").select_option("v9.0.0")
        stairs.get_by_role("button", name="Install on Stairs…", exact=True).click()
        dialog = page.get_by_role("dialog", name="Install v9.0.0 on Stairs?")
        expect(dialog).to_contain_text("Only Stairs changes: it restarts onto v9.0.0")
        expect(dialog).to_contain_text("Every other Player keeps its own version.")
        expect(dialog).to_contain_text("Ready: Central has every file of v9.0.0.")
        dialog.get_by_role("button", name="Install and restart", exact=True).dblclick()
        expect(page.get_by_role("status").filter(
            has_text="Stairs: Installing v9.0.0: Central is downloading it.")).to_be_visible()
        expect(_player_row(page, "Stairs")).to_contain_text("Installing v9.0.0: Central is downloading it")
        assert releases.installs == [("SER-STAIRS", "v9.0.0")]
        assert [(post.url.rsplit("/players/", 1)[1], post.post_data_json) for post in posts] == [
            ("SER-STAIRS/install", {"version": "v9.0.0"})]
        expect(_player_row(page, "Hall")).not_to_contain_text("Installing")


@pytest.mark.parametrize(("status", "code", "words"), [
    (404, "unknown_player", "Central does not know this Player."),
    (404, "unknown_version", "Central's list of releases has no such release."),
    (409, "not_deployable", "Central cannot put this release on a Player: its upload was refused."),
])
def test_each_install_refusal_reads_its_own_sentence(page, registry, status, code, words):
    with operator_server(registry.db, registry.clock) as origin:
        releases = Releases(page)
        releases.refusal = (status, code)
        connect(page, origin, "releases", paused_at=PAUSED_AT)
        _player_row(page, "Hall").get_by_role("button", name="Install on Hall…", exact=True).click()
        dialog = page.get_by_role("dialog", name="Install v9.0.0 on Hall?")
        dialog.get_by_role("button", name="Install and restart", exact=True).click()
        expect(dialog.get_by_role("alert")).to_have_text(words)
        assert releases.installs == [("SER-HALL", "v9.0.0")]


CHECK_QUEUED = ("Central queued a check of GitHub releases; new releases appear here when the media worker "
                "records them.")


def test_check_github_releases_now_sends_one_post_while_unanswered_and_claims_no_release(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        releases = Releases(page)
        releases.view["releases"] = []
        checks = _writes(page, "POST", "/v1/operator/app/releases/refresh")
        gate = RequestGate(page, "**/v1/operator/app/releases/refresh")
        gate.holding = True
        connect(page, origin, "releases", paused_at=PAUSED_AT)
        catalog = page.get_by_role("region", name="Release catalog", exact=True)
        expect(catalog).to_contain_text("GitHub releases has reported no node release yet.")
        catalog.get_by_role("button", name="Check GitHub releases now").click()
        gate.wait_held()
        busy = catalog.get_by_role("button", name="Checking GitHub releases…")
        expect(busy).to_be_disabled()
        busy.click(force=True)  # a disabled button sends nothing
        assert len(checks) == 1
        gate.release()
        expect(page.get_by_role("status").filter(has_text=CHECK_QUEUED)).to_be_visible()
        expect(catalog.get_by_role("button", name="Check GitHub releases now")).to_be_enabled()
        expect(catalog).to_contain_text("GitHub releases has reported no node release yet.")
        assert len(checks) == 1


def test_releases_has_no_effect_gate_section(page, registry):
    """Owner answer G-A: no effect gate, so Releases shows none (the removed section's region)."""
    with operator_server(registry.db, registry.clock) as origin:
        Releases(page)
        connect(page, origin, "releases")
        expect(page.get_by_role("region", name="Release catalog", exact=True)).to_be_visible()
        expect(page.get_by_role("region", name="Effect gate", exact=True)).to_have_count(0)
