"""Fleet › Releases, its pure parts (console DDD Part E §25-§28, beads NR1, NR2 and B7):
releases.js (`releaseHome` with each release's readiness, the Select offers, requests and their
ONE send function, Check GitHub releases now, `releaseResult` with each verb's codes), run under
Node as tests/test_console_fleet_commands.py runs the reboot model. Central ingests every
release itself, so no console code derives a deployment id or calls the deleted publish route
(a source scan holds it). Without Node it skips on a developer machine, but FAILS where the
checks are meant to run in full. The browser half is tests/browser/test_releases_browser.py.
"""

import json
import re
import subprocess
from pathlib import Path

from tests.test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const releases = await import(process.argv[1]);
const { factText } = await import(process.argv[2]);
const out = {};

const D1 = "11111111-2222-8333-8444-555555555555";
const D2 = "22222222-2222-8333-8444-555555555555";
const HAND = "33333333-2222-8333-8444-555555555555";
const APP = "9f8e7d".padEnd(64, "0");
// --- The read model: one row per observed tag, its own deployment and readiness.
const release = (tag, extra = {}) => ({ tag, stable: true, problem: null, manifest_sha256: "ab".repeat(32),
  deployment_id: D1, revision: "1a2b3c4d".repeat(5), discovered_at: 700, base_tag: tag,
  app_environment_sha256: APP, in_window: true, readiness: "ready", readiness_reason: null, missing_bytes: 0,
  ...extra });
const rejected = (tag, problem) => release(tag, { manifest_sha256: null, deployment_id: null, revision: null,
  discovered_at: null, base_tag: null, app_environment_sha256: null, problem, in_window: false, readiness: null,
  missing_bytes: null });
const deployment = (id, extra = {}) => ({ deployment_id: id, published_at: 900, base_tag: "v0.15.0",
  app_environment_sha256: APP, ...extra });
const read = (extra = {}) => ({ read_at: 1000, selection: { revision: 0, deployment_id: null,
  previous_deployment_id: null, changed_at: null, auto: null }, deployments: [], releases: [release("v0.15.0")],
  ...extra });
const home = (r) => {
  const value = releases.releaseHome(r);
  return { selection: factText(value.selection), previous: value.previous === null ? null : factText(value.previous),
    deployments: value.deployments.map((row) => ({ id: row.deploymentId, deployment: factText(row.deployment),
      contents: row.contents, from: row.from === null ? null : factText(row.from), selected: row.selected,
      previous: row.previous })),
    releases: value.releases.map((row) => ({ catalog: factText(row.catalog),
      contents: row.contents === null ? null : factText(row.contents), readiness: factText(row.readiness),
      newerRejected: row.newerRejected, prerelease: row.prerelease, label: row.label,
      deploymentId: row.deploymentId })) };
};
const selectedRead = read({
  selection: { revision: 5, deployment_id: D1, previous_deployment_id: D2, changed_at: 400, auto: null },
  deployments: [deployment(D1), deployment(D2, { base_tag: "v0.14.0" }), deployment(HAND)],
  releases: [
    release("v0.16.0", { deployment_id: "44444444-2222-8333-8444-555555555555", stable: false,
      readiness: "not_downloaded", missing_bytes: 1200000000 }),
    release("v0.15.0", { problem: "node_release_invalid" }),
    release("v0.14.0", { deployment_id: D2, readiness: "downloading", missing_bytes: 2500000000 }),
    release("v0.13.0", { deployment_id: "55555555-2222-8333-8444-555555555555", readiness: "failed",
      readiness_reason: "cache_disk_full", missing_bytes: 5 }),
    release("v0.12.0", { deployment_id: "66666666-2222-8333-8444-555555555555", readiness: null }),
    rejected("v0.11.0", "node_release_identity_conflict"),
  ] });
out.home = {
  empty: home(read({ releases: [] })),
  selected: home(selectedRead),
};
const auto = (readiness, extra = {}) => home(read({ selection: { revision: 0, deployment_id: null,
  previous_deployment_id: null, changed_at: null, auto: { tag: "v0.15.0", deployment_id: D1, readiness,
    readiness_reason: null, missing_bytes: 3000000000, ...extra } } })).selection;
out.auto = { downloading: auto("downloading"), ready: auto("ready", { missing_bytes: 0 }),
  failed: auto("failed", { readiness_reason: "all_references_rejected" }) };
out.newest = [releases.newestStable(selectedRead)?.tag, releases.newestStable(read({ releases: [] }))];
out.lines = Object.fromEntries(selectedRead.releases.map((row) => [row.tag, releases.readinessLine(row)]));

// --- Select: offers and the frozen request, from a Deployments row or a release row.
const listed = read({ deployments: [deployment(D1), deployment("d-2", { app_environment_sha256: null })] });
// A release beyond the capped Deployments list: its row names its deployment.
const beyond = read({ releases: [release("v0.15.0", { deployment_id: D2 })] });
out.selectOffers = [
  releases.selectionOffer(null, D1),
  releases.selectionOffer(listed, D1),
  releases.selectionOffer(read({ ...listed, selection: { revision: 3, deployment_id: D1, changed_at: 1 } }), D1),
  releases.selectionOffer(listed, "d-unlisted"),
  releases.selectionOffer(beyond, D2),
];
const frozenSelect = releases.selectionRequest(listed, D1);
out.selectRequest = { request: frozenSelect, frozen: Object.isFrozen(frozenSelect) && Object.isFrozen(frozenSelect.body) };
out.beyondRequest = releases.selectionRequest(beyond, D2);
out.noApp = releases.selectionRequest(listed, "d-2").noApp;
// Select's confirmation words (R17), one home for every page that sends a selection.
out.selectWords = { withApp: releases.selectionConfirmation(frozenSelect),
  noApp: releases.selectionConfirmation(releases.selectionRequest(listed, "d-2")),
  scope: releases.SELECT_SCOPE, none: releases.SELECT_NO_APP };

// --- sendSelection: judged on releases.latest() at call time; refuses without a PUT.
const sent = [];
let answer = () => new Response(JSON.stringify({ revision: 1, deployment_id: D1 }), { status: 200 });
globalThis.fetch = async (url, init) => {
  sent.push({ url, method: init.method, body: init.body === undefined ? null : JSON.parse(init.body) });
  return answer();
};
const hook = (r) => ({ latest: () => ({ read: r, readAt: r?.read_at ?? null, error: null, seq: 1 }) });
const select = async (r) => {
  const before = sent.length;
  const outcome = await releases.sendSelection(frozenSelect, hook(r));
  return { ...outcome, puts: sent.length - before };
};
const movedOn = read({ ...listed, selection: { revision: 1, deployment_id: "d-2", changed_at: 1 } });
out.select = {
  // The dialog froze revision 0; the newest read has another page's selection at revision 1.
  staleDialog: await select(movedOn),
  alreadySelected: await select(read({ ...listed, selection: { revision: 0, deployment_id: D1, changed_at: 1 } })),
  unlisted: await select(read({ deployments: [], releases: [] })),
  current: await select(listed),
};
answer = () => new Response(JSON.stringify({ error: "node_boot_policy_conflict" }), { status: 409 });
out.select.conflict = await select(listed);
answer = () => new Response(JSON.stringify({ error: "mystery_code" }), { status: 503 });
out.select.unlisted503 = await select(listed);
answer = () => new Response("bad gateway", { status: 502 });
out.select.gateway = await select(listed);
answer = () => { throw new TypeError("network"); };
out.select.lost = await select(listed);
const coded = (code, status) => () => new Response(JSON.stringify({ error: code }), { status });
answer = coded("node_control_disabled", 503);
out.select.nodeOff = await select(listed);
out.selectBodies = sent.map((entry) => ({ url: entry.url, method: entry.method, body: entry.body }));
out.settled = {
  done: releases.selectionSettled(frozenSelect, read({ selection: { revision: 1, deployment_id: D1, changed_at: 1 } })),
  other: releases.selectionSettled(frozenSelect, read({ selection: { revision: 1, deployment_id: "d-2", changed_at: 1 } })),
  unchanged: releases.selectionSettled(frozenSelect, read()),
};
out.sizes = [releases.downloadSize(1200000000), releases.downloadSize(5000)];

// --- Check GitHub releases now: one POST while unanswered, never "a release arrived".
let checking = false;
const heldCheck = { get: () => checking, set: (value) => { checking = value; } };
const checkBefore = sent.length;
let finishCheck;
answer = () => new Promise((resolve) => { finishCheck = resolve; });
const firstCheck = releases.sendCatalogCheck(heldCheck);
out.check = { whileUnanswered: await releases.sendCatalogCheck(heldCheck), heldWhile: checking };
finishCheck(new Response(JSON.stringify({ status: "polling" }), { status: 202 }));
out.check.first = await firstCheck;
out.check.heldAfter = checking;
answer = () => { throw new TypeError("network"); };
out.check.lost = await releases.sendCatalogCheck(heldCheck);
answer = coded("content_unavailable", 503);
out.check.refused = await releases.sendCatalogCheck(heldCheck);
out.check.posts = sent.slice(checkBefore).map((entry) => ({ url: entry.url, method: entry.method, body: entry.body }));
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "releases.js").as_uri(),
         (SRC / "facts.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_each_release_row_shows_its_own_deployment_readiness_and_label():
    home = _run()["home"]
    assert home["empty"]["selection"] == "No boot selection · Central refuses every boot"
    assert home["empty"]["previous"] is None and home["empty"]["releases"] == []
    selected = home["selected"]
    assert selected["selection"] == ("Selected for every boot from now on: deployment 1111… (revision 5) "
                                     "· recorded 10 min ago")
    assert selected["previous"] == "Previous selection: deployment 2222… (release v0.14.0)"
    rows = {row["catalog"].split(" (rev")[0].removeprefix("GitHub releases reported "): row
            for row in selected["releases"]}
    assert [row["readiness"] for row in selected["releases"]] == [
        "Not downloaded", "Ready", "Downloading · 2.5 GB left", "Failed: cache disk full",
        "Unknown: Central did not serve whether it is downloaded",
        "Rejected: node release identity conflict"]
    assert rows["release v0.16.0"]["prerelease"] is True and rows["release v0.15.0"]["prerelease"] is False
    assert rows["release v0.15.0"]["newerRejected"] == "Newer upload rejected: node release invalid"
    assert rows["release v0.14.0"]["newerRejected"] is None
    assert rows["release v0.15.0"]["label"] == "Selected for every boot"
    assert rows["release v0.14.0"]["label"] == "Previous selection"
    assert rows["release v0.16.0"]["label"] is None
    assert rows["release v0.15.0"]["catalog"] == (
        "GitHub releases reported release v0.15.0 (rev 1a2b3c4) · first received 5 min ago")
    assert rows["release v0.15.0"]["contents"] == "Base v0.15.0 · app 9f8e7d…"
    # A Rejected tag has no deployment, no contents and no discovery time to claim.
    [rejected] = [row for row in selected["releases"] if row["deploymentId"] is None]
    assert rejected["catalog"] == "release v0.11.0" and rejected["contents"] is None and rejected["label"] is None
    deployments = {row["id"]: row for row in selected["deployments"]}
    assert deployments["11111111-2222-8333-8444-555555555555"]["from"] == "From release v0.15.0"
    assert deployments["11111111-2222-8333-8444-555555555555"]["selected"] is True
    assert deployments["22222222-2222-8333-8444-555555555555"]["previous"] is True
    assert deployments["33333333-2222-8333-8444-555555555555"]["from"] is None  # published by hand
    assert deployments["33333333-2222-8333-8444-555555555555"]["deployment"].startswith("Deployment 3333… recorded")


def test_with_no_selection_ever_the_selection_names_what_central_will_select_by_itself():
    auto = _run()["auto"]
    assert auto["downloading"] == ("No selection yet · Central selects v0.15.0 when its download finishes "
                                   "(3 GB left)")
    assert auto["ready"] == "No selection yet · Central selects v0.15.0 at its next release check"
    assert auto["failed"] == ("No boot selection · Central refuses every boot. Central cannot select v0.15.0 by "
                              "itself: Failed: all references rejected · choose a release")


def test_update_the_wall_defaults_to_the_newest_stable_release_with_a_deployment():
    assert _run()["newest"] == ["v0.15.0", None]  # v0.16.0 is a pre-release


def test_the_readiness_line_says_what_a_booting_player_meets():
    lines = _run()["lines"]
    assert lines["v0.15.0"] == "Ready: Central has every file of v0.15.0."
    assert lines["v0.14.0"] == ("Downloading: Central still has 2.5 GB of v0.14.0 to download; a Player that boots "
                                "it before then waits for the download.")
    assert lines["v0.16.0"] == "Not downloaded: a Player that boots v0.16.0 waits while Central downloads it."
    assert lines["v0.13.0"] == ("Failed: Central could not download v0.13.0 (cache disk full); a Player that boots "
                                "it waits while Central tries again.")
    assert lines["v0.12.0"] == "Central did not serve whether v0.12.0 is downloaded."


def test_select_offers_and_freezes_the_newest_revision_zero_with_no_selection():
    out = _run()
    assert [offer["offer"] for offer in out["selectOffers"]] == ["blocked", "select", "selected", "blocked", "select"]
    request = out["selectRequest"]["request"]
    assert request["body"]["expected_revision"] == 0 and out["selectRequest"]["frozen"]
    assert request["contents"] == "Base v0.15.0 · app 9f8e7d…" and request["noApp"] is False
    assert out["noApp"] is True
    # A release row beyond the capped Deployments list is selectable from its own row.
    assert out["beyondRequest"]["body"] == {"deployment_id": "22222222-2222-8333-8444-555555555555",
                                            "expected_revision": 0}
    assert out["beyondRequest"]["contents"] == "Base v0.15.0 · app 9f8e7d…"


def test_select_confirmation_states_its_fleet_wide_scope_and_the_no_app_offer():
    words = _run()["selectWords"]
    assert words["scope"].startswith("Every Player that boots by node path from now on is offered this deployment")
    assert "Central cannot list which Players will boot" in words["scope"]
    assert words["none"] == "This deployment has no app: every boot from now on is offered no app."
    assert words["withApp"] == ["Base v0.15.0 · app 9f8e7d…", words["scope"]]
    assert words["noApp"] == ["Base v0.15.0 · no app", words["scope"], words["none"]]


def test_send_selection_judges_the_newest_read_and_refuses_without_a_put():
    out = _run()
    select = out["select"]
    changed = {"outcome": "changed", "message": "The boot selection changed meanwhile; review it.", "puts": 0}
    assert select["staleDialog"] == changed
    assert select["alreadySelected"] == changed
    assert select["unlisted"] == changed
    assert select["current"] == {"outcome": "done", "puts": 1, "code": None,
                                 "message": "Selected for every boot from now on at revision 1."}
    assert select["conflict"] == {**changed, "puts": 1, "code": "node_boot_policy_conflict"}
    # Fail closed on the code, never the status: an unlisted code is refused, even at 503.
    assert select["unlisted503"] == {"outcome": "refused", "message": "Central refused: mystery_code.", "puts": 1,
                                     "code": "mystery_code"}
    assert select["nodeOff"] == {"outcome": "refused", "message": "Central refused: node_control_disabled.",
                                 "puts": 1, "code": "node_control_disabled"}
    assert select["gateway"]["outcome"] == select["lost"]["outcome"] == "unknown"
    assert {(entry["url"], entry["method"]) for entry in out["selectBodies"]} == {
        ("/v1/operator/node/boot-policy", "PUT")}
    assert all(entry["body"] == out["selectRequest"]["request"]["body"] for entry in out["selectBodies"])


def test_a_lost_selection_answer_settles_on_the_next_read():
    out = _run()
    settled = out["settled"]
    assert settled["done"] == {"outcome": "done", "message": "Selected for every boot from now on at revision 1."}
    assert settled["other"]["outcome"] == settled["unchanged"]["outcome"] == "changed"
    assert out["sizes"] == ["1.2 GB", "less than 0.1 GB"]


def _only_caller(pattern, function, check):
    """Every console source naming `pattern` is releases.js, once, inside `function`, after `check`."""
    route = re.compile(pattern)
    naming = {module.name: module.read_text() for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
              if route.search(module.read_text())}
    assert list(naming) == ["releases.js"]
    source = naming["releases.js"]
    assert len(route.findall(source)) == 1
    start = source.index(f"export async function {function}(")
    at = route.search(source).start()
    assert start < at < source.index("\n}\n", start)
    assert check in source[start:at]


def test_send_selection_is_the_only_caller_of_the_boot_policy_route():
    _only_caller(r"/boot-policy[`'\"]", "sendSelection", "selectionRefusal(request, releases.latest().read)")


def test_no_console_code_publishes_or_derives_a_deployment_id():
    """Central ingests every release (B7): the publish route is deleted, so no console source
    names it, and the console derives no deployment id (each release row serves its own).
    Mutation probe: restore `sendPublish`'s POST, or a `deploymentIdFor`, and this fails."""
    route = re.compile(r"/releases/[^\s`'\"]*/deployments|/deployments[`'\"]")
    for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]:
        source = module.read_text()
        assert route.search(source) is None, f"{module.name} names a publish route"
        for name in ("deploymentIdFor", "sendPublish", "publishOffer"):
            assert name not in source, f"{module.name} still has {name}"


def test_check_github_releases_now_sends_one_post_while_unanswered_and_never_claims_a_release():
    check = _run()["check"]
    assert check["whileUnanswered"]["outcome"] == "changed" and check["heldWhile"] is True
    assert check["first"] == {"outcome": "done", "message": (
        "Central queued a check of GitHub releases; new releases appear here when the media worker records them."),
        "code": None}
    assert check["heldAfter"] is False
    assert check["lost"]["outcome"] == "unknown"
    assert check["refused"] == {"outcome": "refused", "message": "Central refused: content_unavailable.",
                                "code": "content_unavailable"}
    assert check["posts"] == [{"url": "/v1/operator/app/releases/refresh", "method": "POST", "body": None}] * 3


def test_send_catalog_check_is_the_only_caller_of_the_release_refresh_route():
    _only_caller(r"/app/releases/refresh[`'\"]", "sendCatalogCheck", "if (held.get())")


def test_one_rule_says_when_an_answer_leaves_the_outcome_unknown():
    """sendOutcome.js `answerUnknown` is the one 5xx rule; each verb names only which served
    codes are its owner's own refusals: none for the equipment writes, `node_`/`rollout_` for
    Reboot, any code for the release verbs. Mutation probe: make `answerUnknown` ignore
    `centralRefusal` and the Reboot and release rows fail."""
    _require_node()
    script = r"""
const outcome = await import(process.argv[1]);
const equipment = await import(process.argv[2]);
const fleet = await import(process.argv[3]);
const releases = await import(process.argv[4]);
const gateway = { ok: false, status: 502, error: null, data: null };
const coded = (error) => ({ ok: false, status: 503, error, data: null });
console.log(JSON.stringify({
  none: outcome.answerUnknown(null),
  gateway: outcome.answerUnknown(gateway),
  ok: outcome.answerUnknown({ ok: true, status: 200 }),
  refused: outcome.answerUnknown({ ok: false, status: 409, error: "x" }),
  reboot: [fleet.rebootResult(coded("node_reboot_outstanding")).outcome, fleet.rebootResult(coded("other")).outcome,
           fleet.rebootResult(gateway).outcome],
  release: [releases.releaseResult(coded("release_unavailable"), () => "", {}).outcome,
            releases.releaseResult(gateway, () => "", {}).outcome, releases.releaseResult(null, () => "", {}).outcome],
  shared: [fleet.rebootResult(gateway).message === outcome.UNKNOWN_MESSAGE,
           releases.releaseResult(gateway, () => "", {}).message === outcome.UNKNOWN_MESSAGE,
           !("UNKNOWN_MESSAGE" in equipment)],
}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--",
         *[(SRC / name).as_uri() for name in ("sendOutcome.js", "equipmentApi.js", "fleetCommands.js",
                                              "releases.js")]],
        capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(result.stdout)
    assert (out["none"], out["gateway"], out["ok"], out["refused"]) == (True, True, False, False)
    assert out["reboot"] == ["changed", "unknown", "unknown"]
    assert out["release"] == ["refused", "unknown", "unknown"]
    assert out["shared"] == [True, True, True]  # one home for the words, not the equipment module
