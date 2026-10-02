"""The fleet commands' pure parts (console DDD §10-§11, bead B2): fleetCommands.js
(`rebootTarget`, `rebootRequest`, `rebootCommandState`, `appOperationState`, `rebootResult`),
run under Node as tests/test_console_players.py runs the B1 model. Without Node it skips on a
developer machine, but FAILS where the checks are meant to run in full. The browser half is
tests/browser/test_player_page_browser.py.
"""

import json
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
const open = { transport_enabled: true, effect_gate: { effective_state: "open", generation: 7, reason: null } };
const node = (r, gate = open, extra = {}) => ({ enabled: true, gate, read: r, operations: null,
  readAt: r?.read_at ?? null, error: null, ...extra });
const target = (n) => commands.rebootTarget(n, n.gate);
out.target = [
  target(node(read([host(), broker, host({ session_id: "old", current: false })]))),
  target(node(read([host()]), { transport_enabled: true,
    effect_gate: { effective_state: "closed", generation: 7, reason: "operator_closed" } })),
  target(node(read([host()]), { transport_enabled: true, effect_gate: { effective_state: "closed" } })),
  target({ enabled: false, gate: null, read: null, error: null }),
  target(node(read([broker]))),
  target(node(read([host({ command_eligible: false, command_reason: "node_offer_superseded" })]))),
  target(node(read([host(), host({ session_id: "s-2" })]))),
  target(node(read([host()]), open, { error: { code: "500", status: 500 } })),
  target({ enabled: true, gate: open, read: null, error: { code: "node_device_unavailable", status: 403 } }),
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
  initiated: state(command({ responses: [response("accepted", 995)],
    effects: [{ state: "reboot_initiated", received_at: 998 }] })),
  noWindow: state(command({ expires_at: null })),
};
// Requested, but Central's poll would not offer it now: the label says so; the state is unchanged.
const targeting = command({ command: { command_session_id: "s-host", producer: { kernel_boot_id: "boot-2" } } });
const notOffered = (n) => show(commands.rebootCommandState(targeting, 1000, { clock, nodeDevice: n }));
out.notOffered = {
  offered: notOffered(node(read([host()]))),
  gateClosed: notOffered(node(read([host()]), { effect_gate: { effective_state: "closed" } })),
  sessionLapsed: notOffered(node(read([host({ current: false })]))),
};

// --- rebootRequest: the whole body frozen at open; no new command id while Requested.
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
const built = commands.rebootRequest(bound, null, snapshot, 1759363200,
  { playerId: "p-1", commandId: "c-new", reason: "stuck", clock });
out.request = built;
out.frozen = Object.isFrozen(built) && Object.isFrozen(built.body) && Object.isFrozen(built.frames[0].runs);
out.whileRequested = commands.rebootRequest(bound, command(), snapshot, 1000,
  { playerId: "p-1", commandId: "c-new", clock });
out.badReason = commands.rebootRequest(bound, null, snapshot, 1000,
  { playerId: "p-1", commandId: "c-new", reason: "two words", clock });
out.closed = commands.rebootRequest(target(node(read([host()]), { effect_gate: { effective_state: "closed" } })),
  null, snapshot, 1000, { playerId: "p-1", commandId: "c-new", clock });
out.afterUnknownSameBoot = commands.rebootRequest(bound, command(), snapshot, 1020,
  { playerId: "p-1", commandId: "c-new", clock }).previous;
out.afterUnknownLaterBoot = commands.rebootRequest(bound,
  command({ command: { producer: { kernel_boot_id: "boot-1" } } }), snapshot, 1020,
  { playerId: "p-1", commandId: "c-new", clock }).previous;
out.afterRejected = commands.rebootRequest(bound, command({ responses: [response("rejected", 995)] }),
  snapshot, 1000, { playerId: "p-1", commandId: "c-new", clock }).previous;

// --- rebootOffer: the request this page holds counts as Requested until a read settles it.
const sent = commands.rebootRequest(bound, null, snapshot, 1000, { playerId: "p-1", commandId: "c-1", clock });
const offer = (latest, at, held = sent) => commands.rebootOffer(bound, latest, at, held, { clock }).offer;
const result = (outcome, retryable = false) => ({ outcome, retryable, message: "" });
out.held = ["done", "already", "refused", "changed"].map((outcome) => commands.heldReboot(sent, result(outcome)) !== null)
  .concat([commands.heldReboot(sent, result("unknown", true)) !== null,
           commands.heldReboot(sent, result("unknown", false)) !== null]);
out.offers = {
  // A read that predates the send: the held request is not listed yet.
  staleReadNone: offer(null, 1002),
  staleReadOlderSettled: offer(command({ command_id: "c-0", responses: [response("rejected", 990)] }), 1002),
  listedRequested: offer(command(), 1010),
  listedSettled: offer(command({ responses: [response("rejected", 995)] }), 1010),
  listedUnknown: offer(command(), 1020),
  // Another request is the latest and Requested: no retry of the held one, no new id.
  otherRequested: offer(command({ command_id: "c-other" }), 1005),
  // The held request is not listed and its frozen window has ended: a new request.
  unlistedAfterWindow: offer(null, 1030),
  noHeld: offer(null, 1002, null),
};
out.newWhileHeld = commands.rebootRequest(bound, null, snapshot, 1002,
  { playerId: "p-1", commandId: "c-2", held: sent, clock });

// --- rebootStale: a dialog held open past its frozen window may not send (sent.retryUntil = 1030).
out.stale = {
  inWindow: commands.rebootStale(sent, null, 1029),
  // Opened at 1000, still open at 1045: a send now would reopen the stale-read race.
  heldOpen: commands.rebootStale(sent, null, 1044),
  otherLatest: commands.rebootStale(sent, command({ command_id: "c-0" }), 1031),
  // Central lists this very request: re-sending lands "already" or 410, never a new id.
  listed: commands.rebootStale(sent, command(), 1044),
  noReadTime: commands.rebootStale(sent, null, null),
};

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
];

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
        {"available": False, "reason": "Central's effect gate is closed (operator closed)"},
        {"available": False, "reason": "Central's effect gate is closed"},
        {"available": False, "reason": "node management is off on this Central"},
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


def test_no_new_command_id_while_the_latest_request_is_requested():
    out = _run()
    assert out["whileRequested"] == {
        "refused": "a reboot request is Requested until t1020; only that request can be retried"}
    assert out["closed"] == {"refused": "Central's effect gate is closed"}
    assert out["badReason"] == {"refused": "a reason is one word: letters, digits and _ . : / - only"}


def test_the_request_this_page_sent_blocks_a_new_command_id_until_a_read_settles_it():
    out = _run()
    # Held: recorded (done, already) or retryable unknown; not refused, changed or a 410.
    assert out["held"] == [True, True, False, False, True, False]
    assert out["offers"] == {
        "staleReadNone": "retry", "staleReadOlderSettled": "retry",
        "listedRequested": "retry", "listedSettled": "new", "listedUnknown": "new",
        "otherRequested": "blocked", "unlistedAfterWindow": "new", "noHeld": "new"}
    assert out["newWhileHeld"] == {"refused": (
        "the reboot request this page sent may still be offered; only that request can be retried")}


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


def test_a_frozen_request_is_not_sent_once_a_read_reaches_its_window():
    stale = _run()["stale"]
    out_of_date = "This request is out of date; close and reopen"
    assert stale == {"inWindow": None, "heldOpen": out_of_date, "otherLatest": out_of_date,
                     "listed": None, "noReadTime": None}


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
    ]
    assert app[2]["fact"] == 'App Effect Broker reported a "rejected" response · first received 10 s ago'
    # One phase event, received once: a `first` receipt, never the broker layer's last report.
    assert app[3]["fact"] == "App Effect Broker reported phase stopping current · first received 5 s ago"
    assert not [entry for entry in app if "last reported" in entry["fact"]]
    assert app[6]["fact"] == "Interrupted (Central's inference: a later boot of this Player was admitted)"
    assert app[7]["fact"] == 'Unknown: Central served an unrecognised state "mystery"'


def test_reboot_answers_use_the_equipment_outcomes_and_410_is_outcome_unknown():
    results = _run()["results"]
    assert [(entry["outcome"], entry["retryable"]) for entry in results] == [
        ("done", False), ("already", False), ("unknown", False), ("unknown", True),
        ("changed", False), ("changed", False), ("refused", False)]
    assert results[1]["message"] == "Already recorded. Requested · delivery unknown."
    assert results[2]["message"].startswith("Outcome unknown")
    assert results[6]["message"] == "Refused: node offer superseded."


def test_no_reboot_is_linked_to_a_later_boot_by_timestamps():
    # §10: a later boot does not show what caused it; the old "subsequent boot" correlation is gone.
    offenders = [module.name for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
                 if "subsequent boot" in module.read_text().lower()]
    assert offenders == []
