"""The fleet commands' pure parts (console DDD §10-§11, beads B2 and R0): fleetCommands.js
(`rebootTarget`, `rebootOffer`, `rebootRequest`, `sendReboot`, `rebootCommandState`,
`rebootResult`; the reboot over the bus, decision 0034 S4c),
run under Node as tests/test_console_players.py runs the B1 model. Without Node it skips on a
developer machine, but FAILS where the checks are meant to run in full. The browser half is
tests/browser/test_player_page_browser.py.
"""

import json
import re
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const commands = await import(process.argv[1]);
const { factText } = await import(process.argv[2]);
const out = {};
const clock = (seconds) => `t${seconds}`;
const show = (state) => ({ state: state.state, label: state.label, fact: factText(state.fact) });

// --- rebootTarget: an active device read; the host line's start when served (Update Wall's).
const host = () => ({ epoch: "start-2", born_at: 980, received_at: 990 });
const read = (line, extra = {}) => ({ read_at: 1000, device_generation: 3, host: line,
  reboot_commands: [], boot_claims: [], ...extra });
const node = (r, extra = {}) => ({ read: r, readAt: r?.read_at ?? null, error: null, ...extra });
const target = (n) => commands.rebootTarget(n);
out.target = [
  target(node(read(host()))),
  target(node(read(null))),
  target(node(read(host(), { device_generation: null }))),
  target(node(read(host()), { error: { code: "500", status: 500 } })),
  target({ read: null, readAt: null, error: { code: "node_device_unavailable", status: 403 } }),
];

// --- rebootCommandState: one named state per request, Central's record and clock only.
const command = (extra = {}) => ({ command_id: "c-1", reason: "console/2026-10-02", by: "console",
  requested_at: 990, delivered: null, answered_at: null, outstanding: true, ...extra });
const state = (c, at = 1000) => show(commands.rebootCommandState(c, at, { clock }));
out.states = {
  requested: state(command()),
  asked: state(command({ delivered: true, answered_at: 994, outstanding: false })),
  notDelivered: state(command({ delivered: false, answered_at: 995, outstanding: false })),
  offQueue: state(command({ outstanding: false })),
  install: state(command({ reason: "install:v2.0.0", by: "central", delivered: true, answered_at: 999,
    outstanding: false })),
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
const bound = target(node(read(host())));
const listed = (id, outstanding, extra = {}) => command({ command_id: id, outstanding, ...extra });
const built = commands.rebootRequest(bound, [], snapshot, 1759363200,
  { playerId: "p-1", commandId: "c-new", reason: "stuck", clock });
out.request = built;
out.frozen = Object.isFrozen(built) && Object.isFrozen(built.body) && Object.isFrozen(built.frames[0].runs);
out.whileOutstanding = commands.rebootRequest(bound, [listed("c-1", true)], snapshot, 1000,
  { playerId: "p-1", commandId: "c-new", clock });
out.badReason = commands.rebootRequest(bound, [], snapshot, 1000,
  { playerId: "p-1", commandId: "c-new", reason: "two words", clock });
out.afterNotDelivered = commands.rebootRequest(bound,
  [listed("c-0", false, { delivered: false, answered_at: 995 })], snapshot, 1000,
  { playerId: "p-1", commandId: "c-new", clock }).previous;

// --- rebootOffer: every listed request, judged by Central's `outstanding`.
const sent = commands.rebootRequest(bound, [], snapshot, 1000, { playerId: "p-1", commandId: "c-1", clock });
const offer = (rows, at, held = sent) => commands.rebootOffer(bound, rows, at, held).offer;
const result = (outcome, retryable = false) => ({ outcome, retryable, message: "" });
out.held = ["done", "already", "refused", "changed"].map((outcome) => commands.heldReboot(sent, result(outcome)) !== null)
  .concat([commands.heldReboot(sent, result("unknown", true)) !== null,
           commands.heldReboot(sent, result("unknown", false)) !== null]);
out.offers = {
  // A read that predates the send: the held request is not listed yet, so it still counts.
  staleReadNone: offer([], 1002),
  staleReadOlderSettled: offer([listed("c-0", false)], 1002),
  listedOutstanding: offer([listed("c-1", true)], 1010),
  listedSettled: offer([listed("c-1", false, { delivered: true })], 1010),
  // Another outstanding request blocks: no retry of the held one, no new id.
  otherOutstanding: offer([listed("c-other", true)], 1005),
  // An older outstanding entry that is not the newest still blocks.
  olderOutstanding: offer([listed("c-new2", false), listed("c-old", true)], 1040, null),
  // The held request is not listed and its retry window has ended: a new request.
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
const send = async (request, r) => {
  const before = posts.length;
  const outcome = await commands.sendReboot("d-1", request, hook(r));
  return { outcome: outcome.outcome, message: outcome.message, posts: posts.length - before };
};
out.send = {
  // The dialog was frozen at read 1000; the newest read, at 1005, lists another tab's queued request.
  staleDialog: await send(frozenAt1000, read(host(), { read_at: 1005, reboot_commands: [listed("c-x", true)] })),
  outOfDate: await send(frozenAt1000, read(host(), { read_at: 1031 })),
  inWindow: await send(frozenAt1000, read(host(), { read_at: 1005 })),
  // A held retry the read lists as outstanding re-sends the identical body.
  heldRetry: await send(frozenAt1000, read(host(), { read_at: 1040, reboot_commands: [listed("c-a", true)] })),
  // The device is no longer readable after the dialog opened: the frozen request is out of date.
  deviceGone: await send(frozenAt1000, read(host(), { read_at: 1005, device_generation: null })),
};
out.sentBodies = posts.map((post) => post.body);
out.sentUrl = posts[0]?.url;
out.frozenBody = frozenAt1000.body;

// --- sendReboot's per-device in-flight guard: a second call while a POST for that device is in flight sends nothing.
const gated = [];
let pending = [];
globalThis.fetch = async (url) => {
  gated.push(url);
  return new Promise((resolve, reject) => pending.push({ resolve, reject }));
};
const ok = () => new Response(JSON.stringify({ duplicate: false }), { status: 200 });
// Answer every POST in flight (each with `how`), then let the sends settle.
const answerAll = (how) => { const now = pending; pending = []; now.forEach(how); };
const resolveOk = ({ resolve }) => resolve(ok());
const flight = (device = "d-1") => commands.sendReboot(device, frozenAt1000, hook(read(host(), { read_at: 1005 })));
const words = (r) => ({ outcome: r.outcome, message: r.message });
const first = flight();
const secondCall = flight();
const otherDevice = flight("d-2");
const postsWhileInFlight = gated.length;
answerAll(resolveOk);
out.guard = { second: words(await secondCall), postsWhileInFlight, first: words(await first),
  otherDevice: (await otherDevice).outcome };
// Released after a failed request (the abort budget's rejection), and after an answer.
const afterFirst = flight();
answerAll(({ reject }) => reject(new DOMException("timed out", "TimeoutError")));
out.guard.afterAnswer = (await afterFirst).outcome;
const afterFailure = flight();
answerAll(resolveOk);
out.guard.afterFailure = (await afterFailure).outcome;
out.guard.posts = gated.length;

// --- rebootResult: Central's answer in the equipment outcome vocabulary.
const answer = (ok, status, error = null, data = null) => commands.rebootResult({ ok, status, error, data });
out.results = [
  answer(true, 200, null, { duplicate: false }),
  answer(true, 200, null, { duplicate: true }),
  answer(false, 502),
  answer(false, 403, "node_device_unavailable"),
  answer(false, 422, "invalid_operator_reboot"),
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


def test_reboot_needs_only_an_active_device_read_or_says_why_not():
    out = _run()
    assert out["target"] == [
        {"available": True, "deviceGeneration": 3, "hostEpoch": "start-2"},
        # No host line heard: the reboot goes over the Pi's link all the same.
        {"available": True, "deviceGeneration": 3, "hostEpoch": None},
        {"available": False, "reason": "the device generation is not served"},
        {"available": False, "reason": "the last node read failed (500); press Refresh"},
        {"available": False, "reason": "no current node record for this box"},
    ]


def test_each_reboot_request_has_one_named_state_from_centrals_record():
    states = _run()["states"]
    assert states["requested"] == {
        "state": "requested",
        "label": "Requested · delivery unknown · Central asks Host Management over the Pi's link",
        "fact": "Recorded by Central · reason console/2026-10-02 · by console · recorded 10 s ago"}
    assert states["asked"] == {
        "state": "asked", "label": "Host Management accepted the reboot · completion unknown",
        "fact": "Host Management reported an accepted answer to Central's ask · first received 6 s ago"}
    assert states["notDelivered"]["state"] == "not_delivered"
    assert states["notDelivered"]["label"] == (
        "Not delivered: the Pi had no link to Central or did not answer at t995; its next boot is offered its version")
    assert states["offQueue"] == {
        "state": "outcome_unknown",
        "label": "Outcome unknown: Central's queue no longer holds it and recorded no answer",
        "fact": "Recorded by Central · reason console/2026-10-02 · by console · recorded 10 s ago"}
    assert states["install"]["state"] == "asked"


def test_the_reboot_request_is_frozen_whole_when_the_dialog_opens():
    out = _run()
    assert out["request"] == {
        "body": {"command_id": "c-new", "reason": "console/2025-10-02/stuck"},
        "hostEpoch": "start-2", "retryUntil": 1759363230,
        # In Output order; an ended Run is not live, and a Run's other targets are not named.
        "frames": [{"frameId": "lobby", "runs": [{"runId": "r-1", "sceneId": "morning", "phase": "body"}]},
                   {"frameId": "hall", "runs": []}],
        "previous": None,
    }
    assert out["frozen"] is True


def test_no_new_command_id_while_a_request_is_outstanding():
    out = _run()
    assert out["whileOutstanding"] == {
        "refused": "a reboot request is queued; only the page that sent it can retry it"}
    assert out["badReason"] == {"refused": "a reason is one word: letters, digits and _ . : / - only"}


def test_every_listed_request_is_judged_by_centrals_outstanding():
    out = _run()
    # Held: recorded (done, already) or retryable unknown; not refused or changed.
    assert out["held"] == [True, True, False, False, True, False]
    assert out["offers"] == {
        "staleReadNone": "retry", "staleReadOlderSettled": "retry",
        "listedOutstanding": "retry", "listedSettled": "new",
        "otherOutstanding": "blocked", "olderOutstanding": "blocked",
        "unlistedAfterWindow": "new", "noHeld": "new"}
    assert out["newWhileHeld"] == {"refused": (
        "the reboot request this page sent may still be queued; only that request can be retried")}


def test_send_reboot_judges_the_newest_read_at_call_time_and_refuses_without_a_post():
    send = _run()["send"]
    # The dialog frozen at read 1000 meets a read at 1005 listing another outstanding request.
    assert send["staleDialog"] == {"outcome": "changed", "posts": 0,
                                   "message": "Another reboot request for this Player is outstanding; close this dialog and review it."}
    assert send["outOfDate"] == {"outcome": "changed", "posts": 0,
                                 "message": "This request is out of date; close and reopen."}
    assert send["deviceGone"]["posts"] == 0
    assert send["inWindow"] == {"outcome": "done", "posts": 1,
                                "message": "Reboot recorded. Requested · delivery unknown."}
    assert send["heldRetry"]["posts"] == 1


def test_send_reboot_holds_one_post_in_flight_per_device():
    """The guard lives in the one send path (R1): a second call for a device while its POST is in
    flight answers `changed` with no POST; another device is not held; the hold is released after an
    answer and after a failed request."""
    guard = _run()["guard"]
    assert guard["second"] == {"outcome": "changed", "message": "A reboot for this Player is already being sent."}
    assert guard["postsWhileInFlight"] == 2  # d-1 once, d-2 once; the second d-1 call sent nothing
    assert guard["first"]["outcome"] == "done" and guard["otherDevice"] == "done"
    assert guard["afterAnswer"] == "unknown" and guard["afterFailure"] == "done"
    assert guard["posts"] == 4


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
    assert "rebootRefusal(request, node.latest())" in source[start:at]


def test_the_hardware_pi_page_reboots_only_through_send_reboot():
    # H1 (console by domain § Fleet): Reboot lives on the Pi's Hardware page unchanged. The only
    # callers of `sendReboot` are the reboot dialog (PlayerCommands.jsx), the Update the wall
    # journey and Home's port of writes (homeCommands.js `askReboot`, decision 0036: the same
    # request, frozen from the Pi's newest node read, confirmed first); the Pi pages and the
    # Health section send nothing themselves.
    call = re.compile(r"\bsendReboot\(")
    modules = [*SRC.rglob("*.js"), *SRC.rglob("*.jsx"), *SRC.rglob("*.ts"), *SRC.rglob("*.tsx")]
    callers = sorted(module.name for module in modules
                     if module.name != "fleetCommands.js" and call.search(module.read_text()))
    assert callers == ["PlayerCommands.jsx", "UpdateWallPage.jsx", "homeCommands.js"]
    home = (SRC / "homeCommands.js").read_text()
    assert len(call.findall(home)) == 1 and "rebootRequest(rebootTarget(latest)" in home
    commands = (SRC / "PlayerCommands.jsx").read_text()
    assert len(call.findall(commands)) == 1
    dialog = commands.index("function RebootDialog(")
    assert dialog < call.search(commands).start() < commands.index("\n}\n", dialog)
    for page in ("PlayerPage.jsx", "pages/hardware-pi-page.tsx", "domain/host-health.tsx"):
        source = (SRC / page).read_text()
        assert "fleetCommands.js" not in source and "apiWrite" not in source, page
    assert "<RebootSection " in (SRC / "pages/hardware-pi-page.tsx").read_text()
    assert "RebootSection" not in (SRC / "PlayerPage.jsx").read_text()


def test_a_new_request_states_the_previous_outcome():
    assert _run()["afterNotDelivered"] == (
        "The previous request: Not delivered: the Pi had no link to Central or did not answer at t995; "
        "its next boot is offered its version.")


def test_reboot_answers_use_the_equipment_outcomes():
    results = _run()["results"]
    assert [(entry["outcome"], entry["retryable"]) for entry in results] == [
        ("done", False), ("already", False), ("unknown", True),
        ("changed", False), ("refused", False), ("changed", False)]
    assert results[1]["message"] == "Already recorded. Requested · delivery unknown."
    assert results[4]["message"] == "Refused: invalid operator reboot."
    assert results[5]["message"] == (
        "Another reboot request for this Player is outstanding; close this dialog and review it.")


def test_no_reboot_is_linked_to_a_later_boot_by_timestamps():
    # §10: a later boot does not show what caused it; the old "subsequent boot" correlation is gone.
    offenders = [module.name for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
                 if "subsequent boot" in module.read_text().lower()]
    assert offenders == []
