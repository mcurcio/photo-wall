"""Stage app, its pure parts (console DDD Part E §25-§28, bead NS1): stage.js (`stageBlocker` on
served facts only, `stageTargets`, `stageRequest` frozen ids and fences, `stageReplaces`, the ONE
send function `sendStage` with this page's held stage, `stageResult` with §26's codes and the
fail-closed default), run under Node as tests/test_console_fleet_commands.py runs the reboot
model. Without Node it skips on a developer machine, but FAILS where the checks are meant to run
in full. The browser half is in tests/browser/test_player_page_browser.py.
"""

import json
import re
import subprocess
from pathlib import Path

from tests.test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const stage = await import(process.argv[1]);
const out = {};

const open = { effective_state: "open", state: "open", generation: 7 };
const closed = { effective_state: "closed", state: "closed", reason: "never_certified", generation: 6, changed_at: 900 };
const broker = (extra = {}) => ({ session_id: "s-broker", current: true, scope: "app_effect",
  producer: { owner: "app_effect_broker" }, ...extra });
const host = { session_id: "s-host", current: true, scope: "operator_reboot", producer: { owner: "host_core" } };
const op = (state, extra = {}) => ({ operation_id: `o-${state}-0001`, command_id: "c-1",
  operator_audit_ref: "console/2025-10-02", state, command_response: null, latest_effect: null, ...extra });
let latestDevice = null;
const device = (sessions = [host, broker()], operations = [], extra = {}) => ({
  read: { device_generation: 3, sessions }, operations: { read_at: 1759363200, operations },
  readAt: 1759363200, error: null, ...extra });

// --- stageBlocker: served facts only.
out.blockers = {
  ok: stage.stageBlocker(device(), device().operations, open, { retired_at: null }),
  retired: stage.stageBlocker(device(), device().operations, open, { retired_at: 5 }),
  notRead: stage.stageBlocker({ read: null, operations: null, readAt: null, error: null }, null, open, null),
  gateClosed: stage.stageBlocker(device(), device().operations, closed, null),
  gateUnread: stage.stageBlocker(device(), device().operations, null, null),
  noBroker: stage.stageBlocker(device([host]), device().operations, open, null),
  twoBrokers: stage.stageBlocker(device([broker(), broker({ session_id: "s-2" })]), device().operations, open, null),
  switching: stage.stageBlocker(device(undefined, [op("switching")]), device(undefined, [op("switching")]).operations, open, null),
  // A stranded stage, Staged with no response, does not block: a newer stage replaces it.
  staged: stage.stageBlocker(device(undefined, [op("staged")]), device(undefined, [op("staged")]).operations, open, null),
  effectUnknown: stage.stageBlocker(device(undefined, [op("effect_unknown")]),
    device(undefined, [op("effect_unknown")]).operations, open, null),
};

// --- stageTargets: deployments carrying an app, no verdict.
const releaseRead = { read_at: 1000, selection: { revision: 2, deployment_id: "d-app-1", changed_at: 900 },
  releases: [], deployments: [
    { deployment_id: "d-app-1", published_at: 800, base_tag: "v0.15.0", app_environment_sha256: "9f8e7d".padEnd(64, "0") },
    { deployment_id: "d-noapp", published_at: 700, base_tag: "v0.15.0", app_environment_sha256: null },
    { deployment_id: "d-app-2", published_at: 600, base_tag: "v0.14.0", app_environment_sha256: "aa".repeat(32) }] };
out.targets = stage.stageTargets(releaseRead).map((row) => ({ id: row.deploymentId, contents: row.contents, selected: row.selected }));
out.noTargets = stage.stageTargets(null);
out.scope = [stage.stageScope(releaseRead.selection), stage.stageScope({ revision: 0, deployment_id: null })];

// --- stageReplaces: what sending replaces when the latest is Staged.
out.replaces = [
  stage.stageReplaces(device(undefined, [op("staged")]).operations),
  stage.stageReplaces(device(undefined, [op("staged", { command_response: { decision: "accepted", received_at: 1 } })]).operations),
  stage.stageReplaces(device(undefined, [op("target_running")]).operations),
];

// --- stageRequest: fences from the reads at open, ids fixed, the same inputs build the same body.
const ids = { operationId: "o-new", commandId: "c-new" };
const atOpen = device(undefined, [op("staged")]);
const built = stage.stageRequest(atOpen, open, "d-app-1", ids);
out.request = built;
out.frozen = Object.isFrozen(built) && Object.isFrozen(built.body);
out.sameBody = JSON.stringify(stage.stageRequest(atOpen, open, "d-app-1", ids).body) === JSON.stringify(built.body);
out.refusedRequest = [stage.stageRequest(atOpen, closed, "d-app-1", ids), stage.stageRequest(atOpen, open, "", ids)];

// --- sendStage: judged on node.latest(), control.latest() and the held stage; refuses without a POST.
const posts = [];
let answer = () => new Response(JSON.stringify({ command: {}, duplicate: false }), { status: 200 });
globalThis.fetch = async (url, init) => {
  posts.push({ url, body: JSON.parse(init.body) });
  return answer();
};
const heldBox = () => {
  let value = null;
  return { get: () => value, set: (request, state) => { value = state === null ? null : { request, state }; } };
};
const hooks = (dev, gate = open) => ({ node: { latest: () => dev }, control: { latest: () => ({ state: "on", gate }) } });
const send = async (request, dev, gate, held = heldBox()) => {
  const before = posts.length;
  const result = await stage.sendStage("d-1", request, hooks(dev, gate), held);
  return { ...result, posts: posts.length - before, held: held.get()?.state ?? null };
};
out.send = {
  // The latest operation turned switching after the dialog froze: no POST.
  switching: await send(built, device(undefined, [op("switching")])),
  gateClosed: await send(built, atOpen, closed),
  // The gate reopened at a new generation: the frozen request is out of date.
  gateMoved: await send(built, atOpen, { ...open, generation: 8 }),
  sessionChanged: await send(built, device([host, broker({ session_id: "s-later" })], [op("staged")])),
  ok: await send(built, atOpen),
};
// A lost answer is held unknown; only that very request may be sent again, byte-identical.
const held = heldBox();
answer = () => { throw new TypeError("network"); };
out.lost = await send(built, atOpen, open, held);
const other = stage.stageRequest(atOpen, open, "d-app-2", { operationId: "o-other", commandId: "c-other" });
out.otherWhileHeld = await send(other, atOpen, open, held);
answer = () => new Response(JSON.stringify({ command: {}, duplicate: true }), { status: 200 });
out.again = await send(built, atOpen, open, held);
out.whileRecorded = await send(built, atOpen, open, held);
// Once the read lists the operation, Central's record is the authority: no resend.
out.listed = await send(built, device(undefined, [op("staged", { operation_id: "o-new" })]), open, held);
out.sentBodies = posts.map((post) => JSON.stringify(post.body));
out.sentUrl = posts[0]?.url;

// --- stageResult: §26's codes, keyed on the code; unlisted codes refused.
const result = (status, error) => stage.stageResult({ ok: false, status, error, data: null }, closed);
out.results = Object.fromEntries([
  ["node_app_current_process_unlinked", 409], ["node_app_target_base_mismatch", 409],
  ["node_app_qualified_fallback_required", 409], ["node_app_existing_drain", 409],
  ["node_app_operator_target_changed", 403], ["node_session_unavailable", 403], ["node_session_superseded", 403],
  ["node_deployment_unknown", 404], ["node_environment_unknown", 404], ["node_app_operation_identity_conflict", 409],
  ["rollout_gate_closed", 503], ["rollout_serving_verifier_unavailable", 503], ["rollout_measured_evidence_expired", 503],
  ["bound_switch_policy_unselected", 409], ["node_control_disabled", 503], ["node_app_stage_invalid", 422],
].map(([code, status]) => [code, result(status, code)]));
out.results.gateClosedWhileShellOpen = stage.stageResult({ ok: false, status: 503, error: "rollout_gate_closed" }, open);
out.results.gateway = stage.stageResult({ ok: false, status: 502, error: null }, open);
out.results.lost = stage.stageResult(null, open);
out.results.done = stage.stageResult({ ok: true, status: 200, data: { duplicate: false } }, open);
// --- boundRuleLines: the rule, then its caveat; neither text is exported alone.
out.boundRuleLines = stage.boundRuleLines();
out.boundExports = ["BOUND_RULE", "BOUND_PROVEN"].filter((name) => name in stage);
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "stage.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


GATE_CLOSED = "Effect gate closed · Central's reason: no deployment certification has opened it"


def test_stage_is_blocked_only_on_served_facts_with_the_gate_flagged():
    blockers = _run()["blockers"]
    assert blockers["ok"] is None
    assert blockers["retired"] == {"reason": "this Player is retired"}
    assert blockers["notRead"] == {"reason": "not read yet"}
    assert blockers["gateClosed"]["gate"] is True and blockers["gateClosed"]["reason"].startswith(GATE_CLOSED)
    assert blockers["gateUnread"] == {"reason": "Central's effect gate is not readable", "gate": True}
    assert blockers["noBroker"] == {"reason": "App Effect Broker has no current session on this boot"}
    assert blockers["twoBrokers"] == {"reason": "Central holds more than one current App Effect Broker session"}
    assert blockers["switching"] == {"reason": "A switch is in progress; wait for it to finish"}
    # A stranded Staged or an Effect unknown is not fenced: a newer stage is how it recovers.
    assert blockers["staged"] is None and blockers["effectUnknown"] is None


def test_stage_targets_are_the_deployments_carrying_an_app_and_the_scope_names_the_selection():
    out = _run()
    assert out["targets"] == [
        {"id": "d-app-1", "contents": "Base v0.15.0 · app 9f8e7d…", "selected": True},
        {"id": "d-app-2", "contents": "Base v0.14.0 · app aaaaaa…", "selected": False}]
    assert out["noTargets"] == []
    assert out["scope"] == [
        "Applies to this boot only. Any later boot, including an unplanned one, is offered the boot selection "
        "(deployment d-ap…).",
        "Applies to this boot only. Any later boot, including an unplanned one, is offered the boot selection "
        "(none: Central refuses every boot)."]


def test_a_newer_stage_states_what_it_replaces_and_never_calls_it_a_retry():
    assert _run()["replaces"] == [
        "Sends a newer stage. It replaces stage o-st…, which App Effect Broker has not responded to.",
        "Sends a newer stage. It replaces stage o-st…, which App Effect Broker accepted.",
        None]


def test_the_stage_request_binds_centrals_fences_and_fixed_ids():
    out = _run()
    assert out["request"] == {
        "body": {"operation_id": "o-new", "command_id": "c-new", "session_id": "s-broker", "device_generation": 3,
                 "deployment_id": "d-app-1", "rollout_generation": 7, "operator_audit_ref": "console/2025-10-02"},
        "replaces": "Sends a newer stage. It replaces stage o-st…, which App Effect Broker has not responded to."}
    assert out["frozen"] is True and out["sameBody"] is True
    assert out["refusedRequest"][0]["refused"].startswith(GATE_CLOSED)
    assert out["refusedRequest"][1] == {"refused": "choose a deployment"}


def test_send_stage_judges_the_newest_reads_and_refuses_without_a_post():
    send = _run()["send"]
    assert send["switching"] == {"outcome": "changed", "posts": 0, "held": None,
                                 "message": "A switch is in progress; wait for it to finish."}
    assert send["gateClosed"]["posts"] == 0
    assert send["gateClosed"]["message"].startswith("This request is out of date; close and reopen (" + GATE_CLOSED)
    assert send["gateMoved"] == {"outcome": "changed", "posts": 0, "held": None,
                                 "message": "This request is out of date; close and reopen."}
    assert send["sessionChanged"]["posts"] == 0
    assert send["ok"] == {"outcome": "done", "posts": 1, "held": "recorded", "code": None,
                          "message": "Stage recorded; App Effect Broker has not responded yet."}


def test_a_lost_stage_answer_is_held_and_only_that_request_is_sent_again_byte_identical():
    out = _run()
    assert out["lost"]["outcome"] == "unknown" and out["lost"]["posts"] == 1 and out["lost"]["held"] == "unknown"
    assert out["otherWhileHeld"] == {
        "outcome": "changed", "posts": 0, "held": "unknown",
        "message": "This page already sent a stage for this Player; the next read settles it."}
    assert out["again"]["outcome"] == "already" and out["again"]["posts"] == 1
    assert out["whileRecorded"]["posts"] == 0  # held recorded: no second send from this page
    assert out["listed"] == {"outcome": "changed", "posts": 0, "held": "recorded",
                             "message": "Central already recorded this stage."}
    # ok, lost, again: three POSTs; the lost one and its resend carry the identical body.
    bodies = out["sentBodies"]
    assert len(bodies) == 3 and bodies[1] == bodies[2] == bodies[0]
    assert out["sentUrl"] == "/v1/operator/node/devices/d-1/app-stages"


def test_every_stage_code_maps_to_its_words_and_an_unlisted_one_is_refused():
    results = _run()["results"]
    refused = {code: value["message"] for code, value in results.items() if value["outcome"] == "refused"}
    assert refused["node_app_current_process_unlinked"] == (
        "The Player app has not linked its current process on this boot.")
    assert refused["node_app_target_base_mismatch"] == (
        "This deployment's base differs from the base this Player booted, or it has no app.")
    assert refused["node_app_qualified_fallback_required"] == (
        "No qualified fallback for this Player's current Outputs and base: qualify the running app first.")
    assert refused["node_app_existing_drain"] == "An equipment drain is active on this Player."
    assert refused["node_deployment_unknown"] == "Central has no such deployment."
    assert refused["node_environment_unknown"] == "Central does not know this app environment."
    assert refused["node_app_operation_identity_conflict"] == "This request id was already used for another stage."
    for code in ("node_app_operator_target_changed", "node_session_unavailable", "node_session_superseded"):
        assert results[code] == {"outcome": "changed", "code": code,
                                 "message": "App Effect Broker's session changed; close and reopen."}
    assert refused["rollout_gate_closed"] == (
        "Effect gate closed: no deployment certification has opened it (see Releases › Effect gate).")
    # Central re-checks its serving evidence while the gate row reads open: never "gate closed".
    assert refused["rollout_serving_verifier_unavailable"] == (
        "Central refused the effect: rollout_serving_verifier_unavailable.")
    assert refused["rollout_measured_evidence_expired"] == (
        "Central refused the effect: rollout_measured_evidence_expired.")
    # Unlisted, including the deleted G6 code and node_control_disabled: the default, refused.
    for code in ("bound_switch_policy_unselected", "node_control_disabled", "node_app_stage_invalid"):
        assert refused[code] == f"Central refused: {code}."
    assert results["gateClosedWhileShellOpen"]["message"] == (
        "Effect gate closed: its reason is not readable here (see Releases › Effect gate).")
    assert results["gateway"]["outcome"] == results["lost"]["outcome"] == "unknown"
    assert results["done"]["outcome"] == "done"


def test_send_stage_is_the_only_caller_of_the_app_stages_route():
    # R13 guarantee strength: test-level. Every console source naming the app-stages route is
    # stage.js, once, inside sendStage, after its call-time check on the newest reads.
    route = re.compile(r"/app-stages[`'\"]")
    naming = {module.name: module.read_text() for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
              if route.search(module.read_text())}
    assert list(naming) == ["stage.js"]
    source = naming["stage.js"]
    assert len(route.findall(source)) == 1
    start = source.index("export async function sendStage(")
    at = route.search(source).start()
    assert start < at < source.index("\n}\n", start)
    assert "stageRefusal(request, node.latest(), gate, held.get())" in source[start:at]
    assert "const gate = control.latest().gate;" in source[start:at]


# Fragments of the bound rule (§25, D16) and its caveat: any one names the text.
_BOUND_TEXT = re.compile(r"shows the base page while the app switches|proven on Central only")


def test_the_bound_rule_has_one_home_and_is_shown_only_with_its_caveat():
    """R1: stage.js `boundRuleLines` is the only export of the rule and its caveat (as releases.js
    `selectionConfirmation` is Select's), and no other console module names either text."""
    out = _run()
    assert out["boundRuleLines"] == [
        "Each Frame this Player drives shows the base page while the app switches, then rejoins its Run at the "
        "current point (missed content is not replayed), as on Reboot.",
        "A switch on a Frame-bound Player is proven on Central only; the Player's side of it is not yet qualified."]
    assert out["boundExports"] == []
    naming = sorted(module.name for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
                    if _BOUND_TEXT.search(module.read_text()))
    assert naming == ["stage.js"]
    assert "BOUND_RULE" not in (SRC / "StageApp.jsx").read_text()
