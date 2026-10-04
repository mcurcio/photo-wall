"""Fleet › Releases against Central's real release read and writes (console DDD Part E §25-§28,
beads NR1, NR2 and B7).

The production app on the loopback harness with node control on. The release catalog is filled
the way the media worker fills it (GitHub releases, through a mock transport, ingested into its
deployment by Central itself), and Select runs Central's real compare-and-set. It proves: an
observed release is listed with its own deployment ("From release"), with no Publish anywhere,
and its row's one verb opens Update the wall for it; each row's readiness, Rejected, pre-release
and "Newer upload rejected" facts, and the first-run empty state, read as the design says;
that Select's one send rule judges the newest read (a dialog frozen before another page's
selection sends no PUT), that Central's 409 and a lost answer read as the design says; that an
unlisted code is refused and nothing is re-sent; and (NV1) that a Central without node control
shows one "not shown" line here and is sent no release read. Check GitHub releases now sends one
POST while unanswered and claims no release; and the Effect gate section shows Central's reason
and an open gate's generation.

Every assertion is behavioural (role, text, request count).
"""

import os
import re
import threading
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from console_tasks import connect, go
from operator_harness import RequestGate, operator_server
from playwright.sync_api import expect
from test_fleet_attempts import BOOT_ID, SERIAL
from test_fleet_rollout_gate import _certificate, _gate, _LocalImageVerifier
from test_node_boot import cold_setup, publish_deployment
from test_node_release_catalog import discover, publication

from central.fleet.node_boot import NodeBootService
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.origins.github import GitHubReleaseOrigin
from contracts.node_boot import NodeBootRequestV2
from contracts.node_release import encode_node_release

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

PAUSED_AT = 1_759_363_200  # the page clock: release reads run only when the test runs it
RELEASE_READ = "/v1/operator/node/releases"
CHANGED = "The boot selection changed meanwhile; review it."
NOT_SHOWN = "Node records are not shown: node management is off (see the banner)."


def _writes(page, method, fragment):
    """Every request with `method` whose URL contains `fragment`, as the page sends it."""
    sent = []
    page.on("request", lambda request: sent.append(request)
            if request.method == method and fragment in request.url else None)
    return sent


def _catalog(registry, monkeypatch):
    """One GitHub release with its app, observed and ingested into its deployment as the release
    sync does it. Returns (release, its manifest digest)."""
    origin, release, _ = publication()
    monkeypatch.setattr(GitHubReleaseOrigin, "from_env", classmethod(lambda cls, env=None: origin))
    # Playwright's event loop runs on this thread, so the worker-side discovery runs on another.
    worker = threading.Thread(target=discover, args=(registry, origin))
    worker.start()
    worker.join(timeout=30)
    return release, sha256(encode_node_release(release)).hexdigest()


def _two_deployments(registry):
    """A selected deployment (revision 1) and a second one, not selected."""
    service, _, selected = cold_setup(registry)
    other = replace(selected, deployment_id=uuid4())
    publish_deployment(registry.db, other, registry.clock)
    return service, selected, other


def _read_once(page, run_ms=30000):
    """Run the page clock one release-read interval and return only once that read is settled.

    The response event fires on status and headers, before the page has parsed the body, so
    the answer arriving is not the page having taken it in. Like drive_poll, wait for the
    Release read status to stop being busy: busy clears only after the read is committed, so
    a send after this returns judges that read (what Select judges).
    """
    status = page.get_by_role("status", name="Release read", exact=True)
    with page.expect_response(lambda response: RELEASE_READ in response.url):
        page.clock.run_for(run_ms)
    expect(status).not_to_have_attribute("aria-busy", "true")


def _deployment(page, deployment_id):
    return page.get_by_role("list", name="Deployments", exact=True).get_by_role(
        "listitem", name=f"Deployment {deployment_id}", exact=True)


def _release(page, tag):
    return page.get_by_role("list", name="Release catalog", exact=True).get_by_role(
        "listitem", name=f"Release {tag}", exact=True)


def _select_dialog(page, deployment_id):
    _deployment(page, deployment_id).get_by_role("button", name="Select for every boot…").click()
    dialog = page.get_by_role("dialog", name=f"Select deployment {deployment_id[:4]}… for every boot?")
    expect(dialog).to_be_visible()
    return dialog


def test_the_tracer_lists_an_ingested_release_and_selects_it_for_every_boot(page, registry, monkeypatch):
    release, _ = _catalog(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        puts = _writes(page, "PUT", "/boot-policy")
        posts = _writes(page, "POST", "/deployments")
        connect(page, origin, "releases")
        # No media worker runs here: the newest stable release stays wanted and not downloaded,
        # so a never-selected wall names what Central will select once it is.
        expect(page.get_by_role("region", name="Boot selection", exact=True)).to_contain_text(
            "Boot selection: No selection yet · Central selects v9.0.0 when its download finishes "
            "(less than 0.1 GB left)")
        row = _release(page, "v9.0.0")
        expect(row).to_contain_text("GitHub releases reported release v9.0.0 (rev aaaaaaa) · first received")
        expect(row).to_contain_text("Contents: Base v9.0.0 · app ")
        expect(row).to_contain_text("Download: Downloading · less than 0.1 GB left · not proof a Player holds it")
        expect(page.get_by_role("button", name=re.compile("Publish"))).to_have_count(0)
        [listed] = page.get_by_role("list", name="Deployments", exact=True).get_by_role("listitem").all()
        expect(listed).to_contain_text("Release: From release v9.0.0")
        deployment_id = listed.get_attribute("aria-label").removeprefix("Deployment ")
        # The default Update the wall and the row's one verb open the journey for this release.
        expect(page.get_by_role("link", name="Update the wall…", exact=True)).to_have_attribute(
            "href", "#/releases/update/v9.0.0")
        expect(row.get_by_role("link", name="Put v9.0.0 on the wall…", exact=True)).to_have_attribute(
            "href", "#/releases/update/v9.0.0")

        dialog = _select_dialog(page, deployment_id)
        expect(dialog).to_contain_text("Every Player that boots by node path from now on is offered this "
                                       "deployment, including Players Central has not seen.")
        dialog.get_by_role("button", name="Select for every boot", exact=True).click()
        expect(dialog).to_be_hidden()
        expect(page.get_by_text("Selected for every boot from now on at revision 1.")).to_be_visible()
        # A fresh Central: the newest read's revision is 0, and Select sends exactly that.
        assert [put.post_data_json for put in puts] == [{"deployment_id": deployment_id, "expected_revision": 0}]
        expect(_release(page, "v9.0.0")).to_contain_text("Selected for every boot")
        # The selected row keeps its way back into a paused rollout (R8: the rollout is tab-driven).
        expect(_release(page, "v9.0.0").get_by_role("link", name="Continue putting v9.0.0 on the wall…",
                                                  exact=True)).to_have_attribute("href", "#/releases/update/v9.0.0")
        expect(page.get_by_role("region", name="Boot selection", exact=True)).to_contain_text(
            f"Selected for every boot from now on: deployment {deployment_id[:4]}… (revision 1)")
        assert posts == []

    # The next node-path boot is offered it.
    boots = NodeBootService(NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test")))
    offer = boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "c" * 64))
    assert (offer.base, offer.app_environment) == (release.base, release.app_environment)


def _served(change):
    """Central's real release read, changed by `change(body)` before the page sees it: the
    readiness this harness cannot produce (it mounts no cache)."""
    def handle(route):
        response = route.fetch()
        body = response.json()
        change(body)
        route.fulfill(response=response, json=body)
    return handle


def test_each_release_row_reads_its_readiness_rejection_and_pre_release(page, registry, monkeypatch):
    _catalog(registry, monkeypatch)

    def readiness(body):
        [ingested] = body["releases"]
        ingested.update(readiness="downloading", missing_bytes=2_500_000_000,
                        problem="node_release_invalid")
        body["releases"] = [
            {**ingested, "tag": "v9.1.0-rc.1", "stable": False, "readiness": "not_downloaded", "problem": None,
             "deployment_id": str(uuid4())},
            ingested,
            {**ingested, "tag": "v8.0.0", "readiness": "failed", "readiness_reason": "cache_disk_full",
             "problem": None, "deployment_id": str(uuid4())},
            {"tag": "v7.0.0", "stable": True, "problem": "node_release_identity_conflict", "manifest_sha256": None,
             "deployment_id": None, "revision": None, "discovered_at": None, "base_tag": None,
             "app_environment_sha256": None, "in_window": False, "readiness": None, "readiness_reason": None,
             "missing_bytes": None},
        ]
        body["selection"]["auto"] = {"tag": "v9.0.0", "deployment_id": ingested["deployment_id"],
                                     "readiness": "downloading", "readiness_reason": None,
                                     "missing_bytes": 2_500_000_000}

    page.route(f"**{RELEASE_READ}", _served(readiness))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "releases")
        expect(page.get_by_role("region", name="Boot selection", exact=True)).to_contain_text(
            "No selection yet · Central selects v9.0.0 when its download finishes (2.5 GB left)")
        newest = _release(page, "v9.0.0")
        expect(newest).to_contain_text("Download: Downloading · 2.5 GB left")
        expect(newest).to_contain_text("Newer upload rejected: node release invalid")
        expect(_release(page, "v9.1.0-rc.1")).to_contain_text("Pre-release: listed, not downloaded ahead")
        expect(_release(page, "v9.1.0-rc.1")).to_contain_text("Download: Not downloaded")
        expect(_release(page, "v8.0.0")).to_contain_text("Download: Failed: cache disk full")
        rejected = _release(page, "v7.0.0")
        expect(rejected).to_contain_text("Download: Rejected: node release identity conflict")
        expect(rejected.get_by_role("link", name=re.compile("on the wall"))).to_have_count(0)
        expect(newest.get_by_role("link", name="Put v9.0.0 on the wall…", exact=True)).to_be_visible()
        # The default Update the wall skips the newer pre-release.
        expect(page.get_by_role("link", name="Update the wall…", exact=True)).to_have_attribute(
            "href", "#/releases/update/v9.0.0")


def test_a_dialog_frozen_before_another_pages_selection_sends_no_put(page, registry):
    service, selected, other = _two_deployments(registry)
    with operator_server(registry.db, registry.clock) as origin:
        puts = _writes(page, "PUT", "/boot-policy")
        connect(page, origin, "releases", paused_at=PAUSED_AT)
        dialog = _select_dialog(page, str(other.deployment_id))
        # Another page selects meanwhile, and this page's next read shows it.
        service.select(other.deployment_id, 1)
        _read_once(page)
        dialog.get_by_role("button", name="Select for every boot", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(CHANGED)
        assert puts == []


def test_centrals_409_reads_changed(page, registry):
    """A send judges the newest SETTLED read; when the page has not seen (or not yet settled)
    a read showing another page's selection, the send goes out and Central's expected_revision
    409 is the fence: one PUT, then CHANGED. The client rule only saves a request it can see."""
    service, selected, other = _two_deployments(registry)
    with operator_server(registry.db, registry.clock) as origin:
        puts = _writes(page, "PUT", "/boot-policy")
        connect(page, origin, "releases", paused_at=PAUSED_AT)
        dialog = _select_dialog(page, str(other.deployment_id))
        service.select(selected.deployment_id, 1)  # this page has not read it
        dialog.get_by_role("button", name="Select for every boot", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(CHANGED)
        assert [put.post_data_json["expected_revision"] for put in puts] == [1]


def test_a_lost_select_answer_resolves_from_the_next_read(page, registry):
    _, _, other = _two_deployments(registry)

    def lost(route):
        route.fetch()  # Central records it; the answer never reaches the page
        route.fulfill(status=502, body="bad gateway")

    page.route("**/v1/operator/node/boot-policy", lost)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "releases", paused_at=PAUSED_AT)
        dialog = _select_dialog(page, str(other.deployment_id))
        dialog.get_by_role("button", name="Select for every boot", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Outcome unknown: Central did not answer. The next read of the boot selection decides.")
        dialog.get_by_role("button", name="Close", exact=True).click()
        expect(page.get_by_text("Selected for every boot from now on at revision 2.")).to_be_visible()
        expect(_deployment(page, str(other.deployment_id))).to_contain_text("Selected")


def test_an_unlisted_code_reads_refused_and_nothing_is_resent(page, registry):
    _, _, other = _two_deployments(registry)
    page.route("**/v1/operator/node/boot-policy", lambda route: route.fulfill(
        status=503, content_type="application/json", body='{"error": "mystery_code"}'))
    with operator_server(registry.db, registry.clock) as origin:
        puts = _writes(page, "PUT", "/boot-policy")
        connect(page, origin, "releases", paused_at=PAUSED_AT)
        dialog = _select_dialog(page, str(other.deployment_id))
        dialog.get_by_role("button", name="Select for every boot", exact=True).click()
        expect(dialog.get_by_role("alert")).to_have_text("Central refused: mystery_code.")
        _read_once(page)
        _read_once(page)
        assert len(puts) == 1


def test_without_node_control_releases_shows_one_line_and_reads_nothing(page, registry):
    with operator_server(registry.db, registry.clock, node_control=None) as origin:
        reads = []
        page.on("request", lambda request: reads.append(request.url) if RELEASE_READ in request.url else None)
        connect(page, origin, "releases")
        expect(page.get_by_text(NOT_SHOWN)).to_have_count(1)
        go(page, "players")
        go(page, "releases")
        expect(page.get_by_text(NOT_SHOWN)).to_have_count(1)
        assert reads == []


# --- NR2: Releases complete.

CHECK_QUEUED = ("Central queued a check of GitHub releases; new releases appear here when the media worker "
                "records them.")


def test_check_github_releases_now_sends_one_post_while_unanswered_and_claims_no_release(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
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


def test_the_effect_gate_section_shows_centrals_reason_and_an_open_gates_generation(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "releases")
        section = page.get_by_role("region", name="Effect gate", exact=True)
        expect(section).to_contain_text(
            "Effect gate: Effect gate closed · Central's reason: no deployment certification has opened it")
        expect(section).to_contain_text("this console cannot open it")
    gate, _ = _gate(registry, _certificate(expires_in=300))
    generation = gate.open(expected_revision=0).generation
    with operator_server(registry.db, registry.clock, node_serving_verifier=_LocalImageVerifier()) as origin:
        connect(page, origin, "releases")
        expect(page.get_by_role("region", name="Effect gate", exact=True)).to_contain_text(
            f"Effect gate open · certified by the deployment · generation {generation} · Central re-checks its "
            "serving evidence on every Reboot and Stage")
