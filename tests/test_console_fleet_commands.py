"""The fleet commands' pure parts (console DDD §10-§11, beads B2 and R0): fleetCommands.js
(`rebootTarget`, `rebootOffer`, `rebootRequest`, `sendReboot`, `rebootCommandState`,
`appOperationState`, `rebootResult`),
run under Node as tests/test_console_players.py runs the B1 model. Without Node it skips on a
developer machine, but FAILS where the checks are meant to run in full. The browser half is
tests/browser/test_player_page_browser.py.
"""

import json
import re
import subprocess
from pathlib import Path

from tests.test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const commands = await import(process.argv[1]);
const { factText } = await import(process.argv[2]);
const out = {};
const clock = (seconds) => `t${seconds}`;
const show = (state) => ({ state: state.state, label: state.label, fact: factText(state.fact) });

// --- rebootTarget: the ONE current host_core session's fences, or why not.
const host = (extra = {}) => ({ session_id: "s-host", current: true, scope: "operator_reboot",
  command_eligible: true, command_reason: null, producer: { owner: "host_core", kernel_boot_id: "boot-2" },
  ...extra });
const broker = { session_id: "s-broker", current: true, scope: "app_effect", command_eligible: true,
  producer: { owner: "app_effect_broker", kernel_boot_id: "boot-2" } };
const read = (sessions, extra = {}) => ({ read_at: 1000, device_generation: 3, sessions,
  reboot_commands: [], boot_claims: [], ...extra });
// The effect gate is the shell's (nodeControl.js), passed beside the node read, never inside it.
const open = { state: "open", effective_state: "open", generation: 7, reason: null };
const closed = { state: "closed", effective_state: "closed", generation: 7, reason: "never_certified" };
const node = (r, extra = {}) => ({ read: r, operations: null, readAt: r?.read_at ?? null, error: null, ...extra });
const target = (n, gate = open) => commands.rebootTarget(n, gate);
out.target = [
  target(node(read([host(), broker, host({ session_id: "old", current: false })]))),
  target(node(read([host()])), { ...closed, reason: "operator_closed" }),
  target(node(read([host()])), closed),
  target(node(read([host()])), null),
  target(node(read([broker]))),
  target(node(read([host({ command_eligible: false, command_reason: "node_offer_superseded" })]))),
  target(node(read([host(), host({ session_id: "s-2" })]))),
  target(node(read([host()]), { error: { code: "500", status: 500 } })),
  target({ read: null, operations: null, readAt: null, error: { code: "node_device_unavailable", status: 403 } }),
];

// --- rebootCommandState: one named state per request, Central's clock only.
const command = (extra = {}) => ({ command_id: "c-1", operator_audit_ref: "console/2026-10-02",
  issued_at: 990, expires_at: 1020, command: { producer: { kernel_boot_id: "boot-2" } },
  responses: [], effects: [], ...extra });
const response = (decision, at) => ({ message: { message: { decision } }, received_at: at });
const state = (c, at = 1000) => show(commands.rebootCommandState(c, at, { clock }));
out.states = {
  requested: state(command()),
  unknown: state(command(), 1020),
  lateAccepted: state(command({ responses: [response("received", 1030), response("accepted", 1040)] }), 1050),
  received: state(command({ responses: [response("received", 995)] })),
  rejected: state(command({ responses: [response("received", 995), response("rejected", 996)] })),
  rejectedReason: state(command({ responses: [{ message: { message: { decision: "rejected",
    reason: "reboot_scope_or_expiry" } }, received_at: 996 }] })),
  initiated: state(command({ responses: [response("accepted", 995)],
    effects: [{ state: "reboot_initiated", received_at: 998 }] })),
  noWindow: state(command({ expires_at: null })),
};
// Requested, but Central's poll would not offer it now: the label says so; the state is unchanged.
const targeting = command({ command: { command_session_id: "s-host", producer: { kernel_boot_id: "boot-2" } } });
const notOffered = (n, gate = open) => show(commands.rebootCommandState(targeting, 1000, { clock, nodeDevice: n, gate }));
out.notOffered = {
  offered: notOffered(node(read([host()]))),
  gateClosed: notOffered(node(read([host()])), closed),
  sessionLapsed: notOffered(node(read([host({ current: false })]))),
};

// --- rebootRequest: the whole body frozen at open; no new command id while one is outstanding.
const snapshot = { inventory: { read_at: 1000, players: [{ id: "p-1", device_id: "d-1", retired_at: null }],
  outputs: [{ player_id: "p-1", output_id: "HDMI-A-1", observation: { connected: true } },
            { player_id: "p-1", output_id: "HDMI-A-2", observation: { connected: true } }],
  frames: [{ id: "lobby", player_id: "p-1", output_id: "HDMI-A-1", generation: 2 },
           { id: "hall", player_id: "p-1", output_id: "HDMI-A-2", generation: 1 }] },
  runtime: { current: { runs: [
    { run_id: "r-1", scene_id: "morning", phase: "body", participants: ["frame:lobby", "frame:other"] },
    { run_id: "r-2", scene_id: "ended", phase: "ended", participants: ["frame:hall"] },
  ] } } };
const bound = target(node(read([host()])));
// One served reboot command on a session; `outstanding` is Central's (§16).
const onSession = (id, outstanding, extra = {}) => command({ command_id: id, outstanding,
  command: { command_session_id: "s-host", producer: { kernel_boot_id: "boot-2" } }, ...extra });
const built = commands.rebootRequest(bound, [], snapshot, 1759363200,
  { playerId: "p-1", commandId: "c-new", reason: "stuck", clock });
out.request = built;
out.frozen = Object.isFrozen(built) && Object.isFrozen(built.body) && Object.isFrozen(built.frames[0].runs);
out.whileOutstanding = commands.rebootRequest(bound, [onSession("c-1", true)], snapshot, 1000,
  { playerId: "p-1", commandId: "c-new", clock });
out.badReason = commands.rebootRequest(bound, [], snapshot, 1000,
  { playerId: "p-1", commandId: "c-new", reason: "two words", clock });
out.closed = commands.rebootRequest(target(node(read([host()])), closed),
  [], snapshot, 1000, { playerId: "p-1", commandId: "c-new", clock });
out.afterUnknownSameBoot = commands.rebootRequest(bound, [command({ outstanding: false })], snapshot, 1020,
  { playerId: "p-1", commandId: "c-new", clock }).previous;
out.afterUnknownLaterBoot = commands.rebootRequest(bound,
  [command({ outstanding: false, command: { producer: { kernel_boot_id: "boot-1" } } })], snapshot, 1020,
  { playerId: "p-1", commandId: "c-new", clock }).previous;
out.afterRejected = commands.rebootRequest(bound,
  [command({ outstanding: false, responses: [response("rejected", 995)] })],
  snapshot, 1000, { playerId: "p-1", commandId: "c-new", clock }).previous;

// --- rebootOffer: every command on the target session, judged by Central's `outstanding`.
const sent = commands.rebootRequest(bound, [], snapshot, 1000, { playerId: "p-1", commandId: "c-1", clock });
const offer = (listed, at, held = sent) => commands.rebootOffer(bound, listed, at, held, { clock }).offer;
const result = (outcome, retryable = false) => ({ outcome, retryable, message: "" });
out.held = ["done", "already", "refused", "changed"].map((outcome) => commands.heldReboot(sent, result(outcome)) !== null)
  .concat([commands.heldReboot(sent, result("unknown", true)) !== null,
           commands.heldReboot(sent, result("unknown", false)) !== null]);
out.offers = {
  // A read that predates the send: the held request is not listed yet, so it still counts.
  staleReadNone: offer([], 1002),
  staleReadOlderSettled: offer([onSession("c-0", false)], 1002),
  listedOutstanding: offer([onSession("c-1", true)], 1010),
  listedSettled: offer([onSession("c-1", false)], 1010),
  // Another outstanding request blocks: no retry of the held one, no new id.
  otherOutstanding: offer([onSession("c-other", true)], 1005),
  // An older outstanding entry that is not the newest still blocks.
  olderOutstanding: offer([onSession("c-new2", false), onSession("c-old", true)], 1040, null),
  // Accepted, not initiated: Central still serves it outstanding until it expires.
  accepted: offer([onSession("c-acc", true, { responses: [response("accepted", 995)] })], 1000, null),
  // A command to an earlier session never blocks a new one.
  earlierSession: offer([command({ command_id: "c-early", outstanding: true,
    command: { command_session_id: "s-old", producer: { kernel_boot_id: "boot-1" } } })], 1000, null),
  // The held request is not listed and its frozen window has ended: a new request.
  unlistedAfterWindow: offer([], 1030),
  noHeld: offer([], 1002, null),
};
out.newWhileHeld = commands.rebootRequest(bound, [], snapshot, 1002,
  { playerId: "p-1", commandId: "c-2", held: sent, clock });

// --- sendReboot: the ONE send path; judged on node.latest() at call time, refuses without a POST.
const posts = [];
globalThis.fetch = async (url, init) => {
  posts.push({ url, body: JSON.parse(init.body) });
  return new Response(JSON.stringify({ duplicate: false }), { status: 200 });
};
const frozenAt1000 = commands.rebootRequest(bound, [], snapshot, 1000, { playerId: "p-1", commandId: "c-a", clock });
const hook = (r) => ({ latest: () => node(r) });
const control = (gate = open) => ({ latest: () => ({ state: "on", gate, failed: false }) });
const send = async (request, r, gate = open) => {
  const before = posts.length;
  const outcome = await commands.sendReboot("d-1", request, hook(r), control(gate));
  return { outcome: outcome.outcome, message: outcome.message, posts: posts.length - before };
};
out.send = {
  // The dialog was frozen at read 1000; the newest read, at 1005, lists another tab's request until 1034.
  staleDialog: await send(frozenAt1000, read([host()], { read_at: 1005,
    reboot_commands: [onSession("c-x", true, { expires_at: 1034 })] })),
  outOfDate: await send(frozenAt1000, read([host()], { read_at: 1031 })),
  inWindow: await send(frozenAt1000, read([host()], { read_at: 1005 })),
  // A held retry the read lists as outstanding re-sends the identical body.
  heldRetry: await send(frozenAt1000, read([host()], { read_at: 1040,
    reboot_commands: [onSession("c-a", true, { expires_at: 1045 })] })),
  // The session changed after the dialog opened: the frozen request is out of date.
  sessionChanged: await send(frozenAt1000, read([host({ session_id: "s-later" })], { read_at: 1005 })),
  // The shell's gate closed after the dialog opened: judged on control.latest(), no POST.
  gateClosed: await send(frozenAt1000, read([host()], { read_at: 1005 }), closed),
};
out.sentBodies = posts.map((post) => post.body);
out.sentUrl = posts[0]?.url;
out.frozenBody = frozenAt1000.body;

// --- appOperationState: the broker's response is read, not only Central's `state`.
const operation = (state, extra = {}) => ({ operation_id: "o-1", command_id: "c-1",
  operator_audit_ref: "stage:1", state, command_response: null, latest_effect: null, ...extra });
const app = (o) => show(commands.appOperationState(o, 1000));
out.app = [
  app(operation("staged")),
  app(operation("staged", { command_response: { decision: "accepted", received_at: 990 } })),
  app(operation("staged", { command_response: { decision: "rejected", received_at: 990 } })),
  app(operation("switching", { latest_effect: { phase: "stopping_current", sequence: 2, received_at: 995 } })),
  app(operation("target_running", { latest_effect: { phase: "running", sequence: 3, received_at: 997 } })),
  app(operation("superseded")),
  app(operation("interrupted_by_reboot")),
  app(operation("mystery")),
  app(operation("ended_by_later_boot", { latest_effect: { phase: "running", sequence: 4, received_at: 990 } })),
];
// Superseded and interrupted keep the broker's earlier answer as a second Evidence fact.
const prior = (o) => {
  const value = commands.appOperationState(o, 1000).prior;
  return value === null ? null : factText(value);
};
out.prior = {
  supersededRejected: prior(operation("superseded", { command_response: { decision: "rejected", received_at: 990 } })),
  interruptedSwitching: prior(operation("interrupted_by_reboot", {
    command_response: { decision: "accepted", received_at: 990 },
    latest_effect: { phase: "stopping_current", sequence: 2, received_at: 995 } })),
  supersededNothing: prior(operation("superseded")),
  stagedHasNone: prior(operation("staged", { command_response: { decision: "rejected", received_at: 990 } })),
  // G2: a finished stage a later boot ended keeps what the broker reported.
  endedRunning: prior(operation("ended_by_later_boot", {
    latest_effect: { phase: "fallback_running", sequence: 5, received_at: 995 } })),
};

// --- rebootResult: Central's answer in the equipment outcome vocabulary.
const answer = (ok, status, error = null, data = null) => commands.rebootResult({ ok, status, error, data });
out.results = [
  answer(true, 200, null, { duplicate: false }),
  answer(true, 200, null, { duplicate: true }),
  answer(false, 410, "node_reboot_expired"),
  answer(false, 502),
  answer(false, 503, "rollout_gate_closed"),
  answer(false, 403, "node_reboot_session_unavailable"),
  answer(false, 409, "node_offer_superseded"),
  answer(false, 409, "node_reboot_outstanding"),
];
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "fleetCommands.js").as_uri(),
         (SRC / "facts.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_reboot_binds_the_one_current_host_management_session_or_says_why_not():
    out = _run()
    assert out["target"] == [
        {"available": True, "sessionId": "s-host", "deviceGeneration": 3, "rolloutGeneration": 7,
         "kernelBootId": "boot-2"},
        # A gate reason carries `gate`, so the Player page links it to Releases › Effect gate.
        {"available": False, "reason": "Effect gate closed · Central's reason: operator closed", "gate": True},
        {"available": False, "gate": True,
         "reason": "Effect gate closed · Central's reason: no deployment certification has opened it"},
        {"available": False, "reason": "Central's effect gate is not readable", "gate": True},
        {"available": False, "reason": "no current Host Management session on this box"},
        {"available": False,
         "reason": "Central refuses commands to this Host Management session (node offer superseded)"},
        {"available": False, "reason": "Central holds more than one current Host Management session"},
        {"available": False, "reason": "the last node read failed (500); press Refresh"},
        {"available": False, "reason": "no current node record for this box"},
    ]


def test_each_reboot_request_has_one_named_state_from_centrals_records():
    states = _run()["states"]
    assert states["requested"] == {
        "state": "requested",
        "label": "Requested · delivery unknown · Central offers it to Host Management until t1020",
        "fact": "Recorded by Central · audit console/2026-10-02 · recorded 10 s ago"}
    assert states["unknown"]["state"] == "outcome_unknown"
    assert states["unknown"]["label"] == (
        "Outcome unknown: no response from Host Management; Central stopped offering it at t1020")
    # A late response after the window moves the state on: nothing is terminal while one can arrive.
    assert states["lateAccepted"] == {
        "state": "accepted", "label": "Accepted by Host Management, not yet started",
        "fact": 'Host Management reported a "accepted" response · first received 10 s ago'}
    assert states["received"]["label"] == "Received by Host Management"
    assert states["rejected"]["state"] == "rejected"
    assert states["rejected"]["label"] == "Rejected by Host Management"
    # The served reason token is named in the Evidence fact (R0).
    assert states["rejectedReason"]["fact"] == (
        'Host Management reported a "rejected" response (reboot scope or expiry) · first received 4 s ago')
    assert states["initiated"]["label"] == (
        "Host Management reported the reboot started · completion unknown")
    assert states["initiated"]["fact"] == (
        "Host Management reported a reboot-initiated event naming this request · first received 2 s ago")
    assert states["noWindow"]["state"] == "requested"


def test_the_reboot_request_is_frozen_whole_when_the_dialog_opens():
    out = _run()
    assert out["request"] == {
        "body": {"command_id": "c-new", "session_id": "s-host", "device_generation": 3,
                 "operator_audit_ref": "console/2025-10-02/stuck", "rollout_generation": 7,
                 "valid_for_seconds": 30},
        "kernelBootId": "boot-2", "windowSeconds": 30, "retryUntil": 1759363230,
        # In Output order; an ended Run is not live, and a Run's other targets are not named.
        "frames": [{"frameId": "lobby", "runs": [{"runId": "r-1", "sceneId": "morning", "phase": "body"}]},
                   {"frameId": "hall", "runs": []}],
        "previous": None,
    }
    assert out["frozen"] is True


def test_no_new_command_id_while_a_command_on_the_session_is_outstanding():
    out = _run()
    assert out["whileOutstanding"] == {
        "refused": "a reboot request is outstanding until t1020; only the page that sent it can retry it"}
    assert out["closed"] == {
        "refused": "Effect gate closed · Central's reason: no deployment certification has opened it"}
    assert out["badReason"] == {"refused": "a reason is one word: letters, digits and _ . : / - only"}


def test_every_command_on_the_target_session_is_judged_by_centrals_outstanding():
    out = _run()
    # Held: recorded (done, already) or retryable unknown; not refused, changed or a 410.
    assert out["held"] == [True, True, False, False, True, False]
    assert out["offers"] == {
        "staleReadNone": "retry", "staleReadOlderSettled": "retry",
        "listedOutstanding": "retry", "listedSettled": "new",
        "otherOutstanding": "blocked", "olderOutstanding": "blocked", "accepted": "blocked",
        "earlierSession": "new", "unlistedAfterWindow": "new", "noHeld": "new"}
    assert out["newWhileHeld"] == {"refused": (
        "the reboot request this page sent may still be offered; only that request can be retried")}


def test_send_reboot_judges_the_newest_read_at_call_time_and_refuses_without_a_post():
    send = _run()["send"]
    # The dialog frozen at read 1000 meets a read at 1005 listing another outstanding request.
    assert send["staleDialog"] == {"outcome": "changed", "posts": 0,
                                   "message": "Another reboot request for this Player is outstanding; close this dialog and review it."}
    assert send["outOfDate"] == {"outcome": "changed", "posts": 0,
                                 "message": "This request is out of date; close and reopen."}
    assert send["sessionChanged"]["posts"] == 0
    assert send["gateClosed"]["posts"] == 0
    assert send["inWindow"] == {"outcome": "done", "posts": 1,
                                "message": "Reboot recorded. Requested · delivery unknown."}
    assert send["heldRetry"]["posts"] == 1


def test_a_held_retry_posts_the_identical_frozen_body():
    out = _run()
    assert out["sentBodies"] == [out["frozenBody"], out["frozenBody"]]
    assert out["sentUrl"] == "/v1/operator/node/devices/d-1/reboots"


def test_send_reboot_is_the_only_poster_of_reboots():
    # §10 guarantee strength: test-level. Every console source whose code names the reboots
    # route (a string or template literal ending in /reboots) is fleetCommands.js, once, inside
    # sendReboot, after its call-time check.
    route = re.compile(r"/reboots[`'\"]")
    naming = {module.name: module.read_text() for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
              if route.search(module.read_text())}
    assert list(naming) == ["fleetCommands.js"]
    source = naming["fleetCommands.js"]
    assert len(route.findall(source)) == 1
    start = source.index("export async function sendReboot(")
    at = route.search(source).start()
    assert start < at < source.index("\n}\n", start)
    assert "rebootRefusal(request, node.latest(), control.latest().gate)" in source[start:at]


def test_a_requested_label_says_when_central_is_not_offering_it_now():
    out = _run()["notOffered"]
    assert out["offered"]["label"] == (
        "Requested · delivery unknown · Central offers it to Host Management until t1020")
    assert out["gateClosed"] == {
        "state": "requested", "label": "Requested · Central is not offering it now (effect gate closed)",
        "fact": "Recorded by Central · audit console/2026-10-02 · recorded 10 s ago"}
    assert out["sessionLapsed"]["state"] == "requested"
    assert out["sessionLapsed"]["label"] == (
        "Requested · Central is not offering it now (session no longer current)")


def test_a_new_request_after_the_window_states_the_previous_outcome_and_boot_identity():
    out = _run()
    assert out["afterUnknownSameBoot"] == (
        "The previous request's outcome is unknown. Host Management's current session is the same "
        "boot that request targeted.")
    assert out["afterUnknownLaterBoot"] == (
        "The previous request's outcome is unknown. Host Management's current session is a later "
        "boot than the one that request targeted.")
    assert out["afterRejected"] == "The previous request: Rejected by Host Management."


def test_app_operations_read_the_brokers_response_not_only_staged():
    app = _run()["app"]
    assert [entry["label"] for entry in app] == [
        "Staged; no response from App Effect Broker",
        "Accepted by App Effect Broker; preparing",
        "Rejected by App Effect Broker",
        "App Effect Broker reported switching (stopping current)",
        "App Effect Broker reported the staged app running",
        "Replaced by a later stage",
        "Interrupted",
        "Unknown state",
        "Ended by a later boot",
    ]
    assert app[2]["fact"] == 'App Effect Broker reported a "rejected" response · first received 10 s ago'
    # One phase event, received once: a `first` receipt, never the broker layer's last report.
    assert app[3]["fact"] == "App Effect Broker reported phase stopping current · first received 5 s ago"
    assert not [entry for entry in app if "last reported" in entry["fact"]]
    assert app[6]["fact"] == "Interrupted (Central's inference: a later boot of this Player was admitted)"
    assert app[7]["fact"] == 'Unknown: Central served an unrecognised state "mystery"'
    # G2 (§26): a derived fact, Central's inference from a later admitted boot.
    assert app[8]["state"] == "ended_by_later_boot"
    assert app[8]["fact"] == ("Ended by a later boot (Central's inference: a later boot was admitted; "
                              "Central offers each boot the boot selection)")


def test_superseded_and_interrupted_operations_keep_the_brokers_earlier_answer():
    prior = _run()["prior"]
    assert prior == {
        "supersededRejected": 'App Effect Broker reported a "rejected" response · first received 10 s ago',
        "interruptedSwitching": "App Effect Broker reported phase stopping current · first received 5 s ago",
        "supersededNothing": None,
        "stagedHasNone": None,
        "endedRunning": "App Effect Broker reported phase fallback running · first received 5 s ago",
    }


def test_reboot_answers_use_the_equipment_outcomes_and_410_is_outcome_unknown():
    results = _run()["results"]
    assert [(entry["outcome"], entry["retryable"]) for entry in results] == [
        ("done", False), ("already", False), ("unknown", False), ("unknown", True),
        ("changed", False), ("changed", False), ("refused", False), ("changed", False)]
    assert results[1]["message"] == "Already recorded. Requested · delivery unknown."
    assert results[2]["message"].startswith("Outcome unknown")
    assert results[6]["message"] == "Refused: node offer superseded."
    assert results[7]["message"] == (
        "Another reboot request for this Player is outstanding; close this dialog and review it.")


def test_no_reboot_is_linked_to_a_later_boot_by_timestamps():
    # §10: a later boot does not show what caused it; the old "subsequent boot" correlation is gone.
    offenders = [module.name for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
                 if "subsequent boot" in module.read_text().lower()]
    assert offenders == []
