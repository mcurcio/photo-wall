"""Fleet › Releases, its pure parts (console DDD Part E §25-§28, beads NR1 and NR2): releases.js
(`deploymentIdFor`, `releaseHome`, the Select and Publish offers, requests and their ONE send
functions, Send again, Check GitHub releases now, `releaseResult` with each verb's codes), run under Node as tests/test_console_fleet_commands.py runs the
reboot model. Without Node it skips on a developer machine, but FAILS where the checks are meant
to run in full. The browser half is tests/browser/test_releases_browser.py.
"""

import json
import re
import subprocess
from pathlib import Path
from uuid import UUID

from tests.test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const releases = await import(process.argv[1]);
const { factText } = await import(process.argv[2]);
const out = {};

const SHA = "ab".repeat(32);
const OTHER = "cd".repeat(32);
out.ids = {
  withApp: releases.deploymentIdFor(SHA, true),
  withoutApp: releases.deploymentIdFor(SHA, false),
  again: releases.deploymentIdFor(SHA, true),
  other: releases.deploymentIdFor(OTHER, true),
  refused: (() => { try { releases.deploymentIdFor("not-a-digest", true); return "built"; } catch { return "refused"; } })(),
};

// --- The read model: "Published from" needs the derived id AND equal contents.
const release = (sha, extra = {}) => ({ manifest_sha256: sha, tag: "v0.15.0", revision: "1a2b3c4d".repeat(5),
  discovered_at: 700, verified_at: null, base_tag: "v0.15.0", app_environment_sha256: "9f8e7d".padEnd(64, "0"),
  download_bytes: 1200000000, ...extra });
const deployment = (id, extra = {}) => ({ deployment_id: id, published_at: 900, base_tag: "v0.15.0",
  app_environment_sha256: "9f8e7d".padEnd(64, "0"), ...extra });
const read = (extra = {}) => ({ read_at: 1000, selection: { revision: 0, deployment_id: null, changed_at: null },
  deployments: [], releases: [release(SHA)], ...extra });
const home = (r) => {
  const value = releases.releaseHome(r);
  return { selection: factText(value.selection),
    deployments: value.deployments.map((row) => ({ id: row.deploymentId, deployment: factText(row.deployment),
      contents: row.contents, from: row.from === null ? null : factText(row.from), selected: row.selected })),
    releases: value.releases.map((row) => ({ catalog: factText(row.catalog), contents: factText(row.contents),
      verified: row.verified === null ? null : factText(row.verified), deploymentId: row.deploymentId })) };
};
const derived = releases.deploymentIdFor(SHA, true);
out.home = {
  empty: home(read({ releases: [] })),
  published: home(read({ releases: [release(SHA, { verified_at: 800 })],
    selection: { revision: 5, deployment_id: derived, changed_at: 400 }, deployments: [deployment(derived)] })),
  // The derived id, listed with other contents (published by hand under it): no inference.
  conflict: home(read({ deployments: [deployment(derived, { app_environment_sha256: null })] })),
  // Equal contents under another id: not this console's publish.
  otherId: home(read({ deployments: [deployment("11111111-2222-8333-8444-555555555555")] })),
};

// --- Select: offers and the frozen request.
const listed = read({ deployments: [deployment(derived), deployment("d-2", { app_environment_sha256: null })] });
out.selectOffers = [
  releases.selectionOffer(null, derived),
  releases.selectionOffer(listed, derived),
  releases.selectionOffer(read({ ...listed, selection: { revision: 3, deployment_id: derived, changed_at: 1 } }), derived),
  releases.selectionOffer(listed, "d-unlisted"),
];
const frozenSelect = releases.selectionRequest(listed, derived);
out.selectRequest = { request: frozenSelect, frozen: Object.isFrozen(frozenSelect) && Object.isFrozen(frozenSelect.body) };
out.noApp = releases.selectionRequest(listed, "d-2").noApp;
// Select's confirmation words (R17), one home for every page that sends a selection.
out.selectWords = { withApp: releases.selectionConfirmation(frozenSelect),
  noApp: releases.selectionConfirmation(releases.selectionRequest(listed, "d-2")),
  scope: releases.SELECT_SCOPE, none: releases.SELECT_NO_APP };

// --- sendSelection: judged on releases.latest() at call time; refuses without a PUT.
const sent = [];
const timeouts = [];
const realTimeout = AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout = (ms) => { timeouts.push(ms); return realTimeout(ms); };
let answer = () => new Response(JSON.stringify({ revision: 1, deployment_id: derived }), { status: 200 });
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
  alreadySelected: await select(read({ ...listed, selection: { revision: 0, deployment_id: derived, changed_at: 1 } })),
  unlisted: await select(read({ deployments: [] })),
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
out.selectBodies = sent.map((entry) => ({ url: entry.url, method: entry.method, body: entry.body }));
out.settled = {
  done: releases.selectionSettled(frozenSelect, read({ selection: { revision: 1, deployment_id: derived, changed_at: 1 } })),
  other: releases.selectionSettled(frozenSelect, read({ selection: { revision: 1, deployment_id: "d-2", changed_at: 1 } })),
  unchanged: releases.selectionSettled(frozenSelect, read()),
};

// --- Publish: the held request, one POST, the long budget.
const store = () => {
  const map = new Map();
  return { get: (id) => map.get(id)?.state ?? null, frozen: (id) => map.get(id)?.request ?? null,
    set: (id, state, request = null) => { if (state === null) map.delete(id); else map.set(id, { state, request }); }, map };
};
const fresh = read();
const held = store();
out.publishOffers = {
  offered: releases.publishOffer(fresh, release(SHA), true, held),
  noApp: releases.publishOffer(fresh, release(SHA, { app_environment_sha256: null }), true, held),
  published: releases.publishOffer(read({ deployments: [deployment(derived)] }), release(SHA), true, held),
  conflict: releases.publishOffer(read({ deployments: [deployment(derived, { base_tag: "v0.14.0" })] }), release(SHA), true, held),
  unlisted: releases.publishOffer(read({ releases: [] }), release(SHA), true, held),
};
const frozenPublish = releases.publishRequest(fresh, release(SHA), true, held);
out.publishRequest = frozenPublish;
const before = sent.length;
const timeoutsBefore = timeouts.length;
let finish;
answer = () => new Promise((resolve) => { finish = resolve; });
const first = releases.sendPublish(frozenPublish, hook(fresh), held);
// While the first is unanswered: the row offers "in flight", and a second send POSTs nothing.
out.whileInFlight = { offer: releases.publishOffer(fresh, release(SHA), true, held).offer,
  second: await releases.sendPublish(frozenPublish, hook(fresh), held),
  request: releases.publishRequest(fresh, release(SHA), true, held) };
finish(new Response(JSON.stringify({ published: true, duplicate: false }), { status: 200 }));
out.firstOutcome = await first;
out.afterDone = releases.publishOffer(fresh, release(SHA), true, held).offer;
out.publishPosts = sent.slice(before).map((entry) => ({ url: entry.url, method: entry.method, body: entry.body }));
out.publishTimeouts = timeouts.slice(timeoutsBefore);
// A lost answer is held as unknown: no second POST until a read lists the id.
const lostHeld = store();
answer = () => { throw new DOMException("timed out", "TimeoutError"); };
const lostBefore = sent.length;
out.lost = await releases.sendPublish(frozenPublish, hook(fresh), lostHeld);
out.lostOffer = releases.publishOffer(fresh, release(SHA), true, lostHeld).offer;
out.lostAgain = await releases.sendPublish(frozenPublish, hook(fresh), lostHeld);
out.lostListed = releases.publishOffer(read({ deployments: [deployment(derived)] }), release(SHA), true, lostHeld).offer;
out.lostPosts = sent.length - lostBefore;
// A refusal releases the hold; an unlisted code is refused.
const refusedHeld = store();
answer = () => new Response(JSON.stringify({ error: "node_release_mystery" }), { status: 422 });
out.refused = await releases.sendPublish(frozenPublish, hook(fresh), refusedHeld);
out.refusedOffer = releases.publishOffer(fresh, release(SHA), true, refusedHeld).offer;
out.sizes = [releases.downloadSize(1200000000), releases.downloadSize(5000)];

// --- NR2: Publish without its app (its own derived id), each verb's codes, Send again.
const noAppRelease = release(SHA, { app_environment_sha256: null });
const withoutHeld = store();
const withoutRequest = releases.publishRequest(read({ releases: [noAppRelease] }), noAppRelease, false, withoutHeld);
out.without = {
  withAppOffer: releases.publishOffer(read({ releases: [noAppRelease] }), noAppRelease, true, withoutHeld),
  offer: releases.publishOffer(read({ releases: [noAppRelease] }), noAppRelease, false, withoutHeld),
  body: withoutRequest.body,
  // A release with an app also offers "without": a second derived id.
  bothChoices: [true, false].map((withApp) => releases.publishOffer(fresh, release(SHA), withApp, store()).deploymentId),
};
const publishWith = async (response) => {
  answer = response;
  const outcome = await releases.sendPublish(frozenPublish, hook(fresh), store());
  return outcome;
};
const coded = (code, status) => () => new Response(JSON.stringify({ error: code }), { status });
out.publishCodes = {};
for (const [code, status] of [["node_release_unknown", 404], ["node_release_app_unconfigured", 422],
  ["node_release_app_selection_invalid", 422], ["node_release_publication_invalid", 422],
  ["node_release_locator_mismatch", 422], ["node_release_verification_storage_unavailable", 503],
  ["node_base_release_provenance_unavailable", 409], ["node_environment_release_provenance_unavailable", 409],
  ["node_base_manager_pins_immutable", 422], ["node_deployment_identity_conflict", 422],
  ["node_environment_identity_conflict", 422], ["origin_unreachable", 503], ["download_corrupt", 503],
  ["rate_limited", 503], ["download_not_found", 422], ["download_too_large", 422],
  ["node_control_disabled", 503], ["node_boot_policy_conflict", 409], ["brand_new_code", 422]]) {
  out.publishCodes[code] = await publishWith(coded(code, status));
}
// Codes are per verb: a Publish code served to Select takes Select's default.
answer = coded("origin_unreachable", 503);
out.selectGetsPublishCode = await releases.sendSelection(frozenSelect, hook(listed));
answer = coded("node_control_disabled", 503);
out.selectNodeOff = await releases.sendSelection(frozenSelect, hook(listed));

// Send again: only the held request, with the identical body, only while held unknown.
const againHeld = store();
const againBefore = sent.length;
answer = coded("origin_unreachable", 503);
out.againFirst = await releases.sendPublish(frozenPublish, hook(fresh), againHeld);
const heldRequest = againHeld.frozen(frozenPublish.body.deployment_id);
out.againHeldIsSent = heldRequest === frozenPublish;
out.againWords = releases.sendAgainWords(heldRequest);
// Not explicit: nothing is sent.
out.againImplicit = await releases.sendPublish(heldRequest, hook(fresh), againHeld);
// Explicit, but another body for the same id: nothing is sent.
const otherBody = releases.publishRequest(fresh, release(SHA), true, store());
out.againOtherBody = await releases.sendPublish(otherBody, hook(fresh), againHeld, { again: true });
// Explicit, but Central no longer lists the release: nothing is sent.
out.againUnlisted = await releases.sendPublish(heldRequest, hook(read({ releases: [] })), againHeld, { again: true });
answer = () => new Response(JSON.stringify({ published: true, duplicate: true }), { status: 200 });
out.againSent = await releases.sendPublish(heldRequest, hook(fresh), againHeld, { again: true });
out.againAfter = releases.publishOffer(fresh, release(SHA), true, againHeld).offer;
// Recorded, not unknown: Send again sends nothing.
out.againRecorded = await releases.sendPublish(heldRequest, hook(fresh), againHeld, { again: true });
out.againPosts = sent.slice(againBefore).map((entry) => entry.body);

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


def test_the_deployment_id_is_derived_from_the_release_content_as_a_uuid_v8():
    ids = _run()["ids"]
    with_app, without_app = UUID(ids["withApp"]), UUID(ids["withoutApp"])
    assert with_app.version == without_app.version == 8
    assert with_app.variant == without_app.variant == "specified in RFC 4122"
    assert ids["withApp"] == ids["again"] == str(with_app)  # stable and canonical, as Central lists it
    assert ids["withApp"] != ids["withoutApp"] and ids["withApp"] != ids["other"]
    assert ids["withApp"].replace("-", "")[:12] == "ab" * 6  # laid out from the digest
    assert ids["refused"] == "refused"


def test_published_from_needs_the_derived_id_and_equal_contents():
    home = _run()["home"]
    assert home["empty"]["selection"] == "No boot selection · Central refuses every boot"
    published = home["published"]
    [row] = published["deployments"]
    assert row["from"] == ("Published from release v0.15.0 (Central's inference: the deployment id is the one "
                           "this console derives from that release, and its contents match)")
    assert row["deployment"] == f"Deployment {row['id'][:4]}… published · recorded 1 min ago"
    assert row["contents"] == "Base v0.15.0 · app 9f8e7d…" and row["selected"]
    assert published["selection"] == (f"Selected for every boot from now on: deployment {row['id'][:4]}… "
                                      "(revision 5) · recorded 10 min ago")
    [release] = published["releases"]
    assert release["catalog"] == "GitHub releases reported release v0.15.0 (rev 1a2b3c4) · first received 5 min ago"
    assert release["verified"] == "Bytes verified by Central at publish · recorded 3 min ago"
    assert release["contents"] == "Base v0.15.0 · app 9f8e7d…"
    assert release["deploymentId"] == row["id"]
    for case in ("conflict", "otherId"):
        assert home[case]["deployments"][0]["from"] is None
        assert home[case]["releases"][0]["deploymentId"] is None


def test_select_offers_and_freezes_the_newest_revision_zero_with_no_selection():
    out = _run()
    assert [offer["offer"] for offer in out["selectOffers"]] == ["blocked", "select", "selected", "blocked"]
    request = out["selectRequest"]["request"]
    assert request["body"]["expected_revision"] == 0 and out["selectRequest"]["frozen"]
    assert request["contents"] == "Base v0.15.0 · app 9f8e7d…" and request["noApp"] is False
    assert out["noApp"] is True


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
    assert select["gateway"]["outcome"] == select["lost"]["outcome"] == "unknown"
    assert {(entry["url"], entry["method"]) for entry in out["selectBodies"]} == {
        ("/v1/operator/node/boot-policy", "PUT")}
    assert all(entry["body"] == out["selectRequest"]["request"]["body"] for entry in out["selectBodies"])


def test_a_lost_selection_answer_settles_on_the_next_read():
    settled = _run()["settled"]
    assert settled["done"] == {"outcome": "done", "message": "Selected for every boot from now on at revision 1."}
    assert settled["other"]["outcome"] == settled["unchanged"]["outcome"] == "changed"


def test_publish_holds_its_one_request_with_the_long_budget():
    out = _run()
    offers = out["publishOffers"]
    assert offers["offered"]["offer"] == "publish"
    assert offers["noApp"] == {"offer": "blocked", "reason": "This release has no app"}
    assert offers["published"]["offer"] == "published"
    assert offers["conflict"] == {"offer": "blocked",
                                  "reason": "A different deployment already uses this release's id (published by hand)"}
    assert offers["unlisted"] == {"offer": "blocked", "reason": "Central no longer lists this release"}
    request = out["publishRequest"]
    assert request["body"] == {"deployment_id": offers["offered"]["deploymentId"], "select_app": True,
                               "operator_audit_ref": "console/1970-01-01"}
    assert request["size"] == "1.2 GB"
    assert request["permanence"].startswith("Permanent: Central cannot remove a deployment.")
    assert out["whileInFlight"]["offer"] == "in_flight"
    assert out["whileInFlight"]["second"]["outcome"] == "changed"
    assert out["whileInFlight"]["request"] == {"refused": "already sent from this page"}
    assert out["firstOutcome"]["outcome"] == "done"
    assert out["afterDone"] == "recorded"
    assert out["publishPosts"] == [{"url": "/v1/operator/node/releases/" + "ab" * 32 + "/deployments",
                                    "method": "POST", "body": request["body"]}]
    assert out["publishTimeouts"] == [30 * 60 * 1000]


def test_a_lost_publish_answer_is_held_unknown_until_a_read_lists_the_deployment():
    out = _run()
    assert out["lost"]["outcome"] == "unknown"
    assert out["lostOffer"] == "unknown"
    assert out["lostAgain"]["outcome"] == "changed"
    assert out["lostPosts"] == 1
    assert out["lostListed"] == "published"
    assert out["refused"] == {"outcome": "refused", "message": "Central refused: node_release_mystery.",
                             "code": "node_release_mystery"}
    assert out["refusedOffer"] == "publish"
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


def test_send_publish_is_the_only_caller_of_the_release_publish_route():
    _only_caller(r"/deployments[`'\"]", "sendPublish",
                 "const read = releases.latest().read;\n"
                 "  const offer = publishOffer(read, request.release, request.withApp, held);")


def test_publish_without_its_app_has_its_own_derived_id_and_a_no_app_release_offers_only_it():
    out = _run()
    without = out["without"]
    assert without["withAppOffer"] == {"offer": "blocked", "reason": "This release has no app"}
    assert without["offer"]["offer"] == "publish"
    assert without["body"] == {"deployment_id": without["offer"]["deploymentId"], "select_app": False,
                               "operator_audit_ref": "console/1970-01-01"}
    with_app, without_app = without["bothChoices"]
    assert with_app != without_app and without_app == without["offer"]["deploymentId"]


PUBLISH_WORDS = {
    "node_release_unknown": ("refused", "Central no longer lists this release."),
    "node_release_app_unconfigured": ("refused", "This release has no app."),
    "node_release_app_selection_invalid": ("refused", "Central could not read this request."),
    "node_release_publication_invalid": ("refused", "Central could not read this request."),
    "node_release_locator_mismatch": ("refused", "This release's asset locations disagree with its manifest."),
    "node_release_verification_storage_unavailable": (
        "refused", "Central lacks scratch space to verify this release (another publish may be running)."),
    "node_base_release_provenance_unavailable": ("refused", "Central cannot match this release's bytes to its catalog."),
    "node_environment_release_provenance_unavailable": (
        "refused", "Central cannot match this release's bytes to its catalog."),
    "node_base_manager_pins_immutable": (
        "refused", "This base already has different App Manager pins; this release can never be published."),
    "node_deployment_identity_conflict": (
        "refused", "A different deployment already uses this release's id (published by hand)."),
    "node_environment_identity_conflict": ("refused", "Central already describes this app environment differently."),
    "origin_unreachable": ("unknown", "GitHub releases did not answer; send again."),
    "download_corrupt": ("unknown", "GitHub releases did not answer; send again."),
    "rate_limited": ("unknown", "GitHub releases did not answer; send again."),
    "download_not_found": ("refused", "GitHub releases refused the download: download not found."),
    "download_too_large": ("refused", "GitHub releases refused the download: download too large."),
    # Unlisted for Publish, each takes the default: node control off, another verb's code, a new code.
    "node_control_disabled": ("refused", "Central refused: node_control_disabled."),
    "node_boot_policy_conflict": ("refused", "Central refused: node_boot_policy_conflict."),
    "brand_new_code": ("refused", "Central refused: brand_new_code."),
}


def test_every_publish_code_maps_to_its_words_and_an_unlisted_one_is_refused():
    out = _run()
    assert {code: (outcome["outcome"], outcome["message"]) for code, outcome in out["publishCodes"].items()} \
        == PUBLISH_WORDS
    assert out["selectGetsPublishCode"] == {"outcome": "refused", "message": "Central refused: origin_unreachable.",
                                             "code": "origin_unreachable"}
    assert out["selectNodeOff"] == {"outcome": "refused", "message": "Central refused: node_control_disabled.",
                                     "code": "node_control_disabled"}


def test_send_again_resends_only_the_held_body_and_states_the_redownload():
    out = _run()
    assert out["againFirst"]["outcome"] == "unknown" and out["againHeldIsSent"]
    assert out["againWords"] == ("Sends the identical request again. Central downloads and verifies 1.2 GB from "
                                 "GitHub releases again, even if its first download is still running.")
    for refused in ("againImplicit", "againOtherBody", "againUnlisted", "againRecorded"):
        assert out[refused]["outcome"] == "changed", refused
    assert out["againSent"]["outcome"] == "already"
    assert out["againAfter"] == "recorded"
    body = out["publishRequest"]["body"]
    assert out["againPosts"] == [body, body]  # the first send and the one explicit Send again


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
    Reboot, any code for Publish. Mutation probe: make `answerUnknown` ignore `centralRefusal`
    and the Reboot and Publish rows fail."""
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
  publish: [releases.releaseResult(coded("release_unavailable"), () => "", {}).outcome,
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
    assert out["publish"] == ["refused", "unknown", "unknown"]
    assert out["shared"] == [True, True, True]  # one home for the words, not the equipment module
