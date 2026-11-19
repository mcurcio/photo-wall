"""Update the wall, its pure parts (console DDD Part E §25a, beads NU1 and B8): updateWall.js
(`journeyStep` over every row of the step table, the one Put-on-the-wall confirmation and its
download wait, `keepPlan`'s order, `keepRow` over every row of the Keep table,
`nextReboot`'s one-in-flight rule, `keepPause`) on Central's ReleasesView (a version per Player,
Q2 A 2026-10-16), bundled by the console's own esbuild and run under Node, and the static rule
that the journey's modules name no route (every write goes through its verb's one send function)
and never send an Install. The journey has no app steps (Try, Qualify, Look, Back out): they left
with the Pi's old path (S4c-2a-iii). Without Node it skips on a
developer machine, but FAILS where the checks are meant to run in full. The browser half is in
tests/browser/test_update_wall_browser.py.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path

from test_console_flow import _require_node
from test_console_routes_r4 import ESBUILD, _require_esbuild

SRC = Path(__file__).parents[1] / "central/console/src"
OLD_APP = "11" * 32
NEW_APP = "22" * 32

SCRIPT = r"""
const { uw } = await import(process.argv[1]);
const [OLD_APP, NEW_APP] = [process.argv[2], process.argv[3]];
const out = {};

const release = (version, base, app, extra = {}) => ({ version, discovered_at: 900, base_tag: base,
  app_environment_sha256: app, stable: true, problem: null, in_window: true, readiness: "ready",
  readiness_reason: null, missing_bytes: 0, ...extra });
const releases = [release("v2.0.0", "v1.0.0", NEW_APP), release("v2.1.0", "v1.0.0", null),
  release("v3.0.0", "v3.0.0", NEW_APP)];
// The release every Player boots before the journey.
const OLD = release("v1.0.0", "v1.0.0", OLD_APP);
// A release whose upload Central refused.
const REJECTED = release("v4.0.0", null, null, { problem: "node_release_invalid", readiness: null });
const version = (serial, on, previous) => ({ serial, player_id: `p-${serial}`, name: serial, version: on, chosen: true,
  previous, install: null, running: null });
// `on`: the release every Player boots next (the target once Put on the wall gave it to them).
const readOf = ({ on = "v1.0.0", previous = null, rows = releases } = {}) => ({ read_at: 1000, default: "v2.0.0",
  releases: [...rows, OLD, REJECTED], players: [version("S1", on, previous), version("S2", on, previous)] });
const held = (extra = {}) => ({ rolling: false, ...extra });
const step = (reads, h = held()) => uw.journeyStep(reads, { tag: reads.tag ?? "v2.0.0" }, h).step;
const r = (releasesRead, rows = null, tag) => ({ releases: releasesRead, rows, tag });

// --- journeyStep: every row of the step table, and the derivations around it.
out.steps = {
  reading: step(r(null)),
  noRelease: step({ ...r(readOf()), tag: "v9.9.9" }),
  rejected: step({ ...r(readOf()), tag: "v4.0.0" }),
  choose: step(r(readOf())),
  // One Player already on the target is not the wall on it.
  chooseOnePlayerOn: step(r({ ...readOf(), players: [version("S1", "v2.0.0", null), version("S2", "v1.0.0", null)] })),
  pausedOnOpen: step(r(readOf({ on: "v2.0.0" }), [{ state: "rejoined" }, { state: "waiting" }])),
  keeping: step(r(readOf({ on: "v2.0.0" }), [{ state: "rejoined" }, { state: "waiting" }]), held({ rolling: true })),
  doneKept: step(r(readOf({ on: "v2.0.0" }), [{ state: "rejoined" }, { state: "skipped" }]), held({ rolling: true })),
  // A wall the target lost (another page put another release on) goes back to Choose.
  selectionLost: step(r(readOf()), held({ rolling: true })),
};

const targetOf = (tag, read = readOf()) => uw.journeyTarget(read, tag);
out.target = targetOf("v2.0.0");

// --- Put vX on the wall: ONE confirmation naming the reboot cost, with the readiness line.
const downloading = targetOf("v2.0.0", readOf({ rows: [release("v2.0.0", "v1.0.0", NEW_APP,
  { readiness: "downloading", missing_bytes: 1500000000 })] }));
out.put = {
  open: uw.putConfirmation(targetOf("v2.0.0")),
  openDownloading: uw.putConfirmation(downloading),
  wait: [uw.downloadWait(targetOf("v2.0.0")), uw.downloadWait(downloading),
    uw.downloadWait({ release: { version: "v2.0.0", readiness: "failed", readiness_reason: "cache_disk_full" } }),
    uw.downloadWait({ release: { version: "v2.0.0", readiness: null } })],
  rejectedTarget: targetOf("v4.0.0"),
};

// --- keepPlan: known, not retired Players, by name.
const player = (id, serial, extra = {}) => ({ id, device_id: `dev-${id}`, registered_at: 1, retired_at: null,
  last_seen: 990, last_report_at: 995, authority_epoch: 1, ...extra });
const snapshotOf = ({ interrupted = [], silent = [], failing = [] } = {}) => ({
  readAt: 1000,
  inventory: {
    read_at: 1000, silent_after_seconds: 30, socket_keepalive_seconds: 10,
    players: [player("p-c"), player("p-a"), player("p-b"), player("p-gone", null, { retired_at: 5 })]
      .map((p) => (silent.includes(p.id) ? { ...p, last_report_at: 100 } : p)),
    outputs: ["p-a", "p-b", "p-c", "p-gone"].map((id) => ({ player_id: id, output_id: "HDMI-A-1", observation: { connected: true } })),
    frames: ["p-a", "p-b", "p-c"].map((id) => ({ id: `f-${id}`, player_id: id, output_id: "HDMI-A-1", readiness: "ready" })),
  },
  outputInterruptions: interrupted.map((id) => ({ frame_id: `f-${id}`, cause_layer: "app_effect_broker", interrupted_at: 990 })),
  readinessDiagnostics: failing.map((id) => ({ player_id: id, frame_id: `f-${id}`, output_id: "HDMI-A-1",
    failure_code: "decode", received_at: 995 })),
  runtime: null,
});
const boot = new Map([["dev-p-a", { serial: "SER-AAA" }], ["dev-p-b", { serial: "SER-BBB" }], ["dev-p-c", { serial: "SER-CCC" }]]);
out.plan = uw.keepPlan(snapshotOf(), { devices: boot }).map((entry) => entry.playerId);

// --- keepRow: every row of the Keep table. The host line's start tells a later boot (decision 0034).
const command = (id, extra = {}) => ({ command_id: id, reason: "console/2026-10-02", by: "console", requested_at: 990,
  delivered: null, answered_at: null, outstanding: true, ...extra });
const node = ({ start = "start-1", commands = [] } = {}) => ({
  read: { device_generation: 3, read_at: 1000, host: start === null ? null : { epoch: start, born_at: 900, received_at: 990 },
    reboot_commands: commands, boot_claims: [] },
  readAt: 1000, error: null });
// Sent on a snapshot listing authority epoch 0; snapshotOf lists epoch 1 (the new boot's enrollment).
const sent = (atMs = 0, epoch = 0) => ({ request: { body: { command_id: "cmd-1" }, hostEpoch: "start-1" }, atMs, epoch });
// v3.0.0's base is carried by no other release, so it identifies v3.0.0; v2.0.0 shares base v1.0.0
// with v2.1.0 and the release every Player boots before.
const T = targetOf("v3.0.0");
const SHARED = targetOf("v2.0.0");
// A pre-release on the same base is not a release any Player runs...
const PRE = release("v3.1.0-rc.1", "v3.0.0", NEW_APP, { stable: false });
const PRE_READ = readOf({ rows: [...releases, PRE] });
// ...unless a Player booted it before: a Player not rebooted since may still run it.
const PRE_PREVIOUS = readOf({ rows: [...releases, PRE], previous: PRE.version });
out.identifies = { identified: T.baseIdentifies, shared: SHARED.baseIdentifies,
  noBase: uw.baseIdentifiesTarget(readOf(), { release: { version: "v9.0.0", base_tag: null } }),
  prerelease: uw.baseIdentifiesTarget(PRE_READ, T), prereleasePrevious: uw.baseIdentifiesTarget(PRE_PREVIOUS, T),
  running: [uw.runningBase({ players: [{ player_id: "p-a", running: "v3.0.0" }] }, "p-a"),
    uw.runningBase({ players: [{ player_id: "p-a", running: null }] }, "p-a"), uw.runningBase(null, "p-a")] };
const keepValue = (input) => uw.keepRow({ node: node(), snapshot: snapshotOf(), playerId: "p-a", target: T,
  sent: null, waitedMs: 0, skipped: false, ...input });
const keep = (input) => {
  const value = keepValue(input);
  return { state: value.state, label: value.label, pause: value.pause };
};
out.rows = {
  reading: keep({ node: { read: null, readAt: null, error: null } }),
  readFailed: keep({ node: { read: null, readAt: null, error: { code: "node_device_unavailable", status: 404 } } }),
  waiting: keep({}),
  waitingShared: keep({ target: SHARED }),
  // Not rebooted by this page, its host running the target's base (it identifies the target).
  recognised: keep({ running: "v3.0.0" }),
  recognisedOtherBase: keep({ running: "v1.0.0" }),
  // A shared base says nothing: the Player is rebooted to make sure.
  sharedBaseRunning: keep({ target: SHARED, running: "v1.0.0" }),
  recognisedStalled: keep({ running: "v3.0.0", snapshot: snapshotOf({ interrupted: ["p-a"] }), waitedMs: 600000 }),
  rebootingHeldUnlisted: keep({ sent: sent() }),
  rebootingListed: keep({ sent: sent(), node: node({ commands: [command("cmd-1")] }) }),
  rebootingNotDelivered: keep({ sent: sent(), node: node({ commands: [command("cmd-1", { delivered: false,
    answered_at: 995, outstanding: false })] }) }),
  rebootingOutcomeUnknown: keep({ sent: sent(), node: node({ commands: [command("cmd-1", { outstanding: false })] }) }),
  // Another page's reboot that Central lists as outstanding.
  rebootingOutstanding: keep({ node: node({ commands: [command("cmd-other")] }) }),
  rejoiningOutput: keep({ sent: sent(), node: node({ start: "start-2" }), snapshot: snapshotOf({ interrupted: ["p-a"] }) }),
  rejoiningSilent: keep({ sent: sent(), node: node({ start: "start-2" }), snapshot: snapshotOf({ silent: ["p-a"] }) }),
  rejoined: keep({ sent: sent(), node: node({ start: "start-2" }) }),
  // A later start, but a snapshot read before its Player app enrolled: the old boot's liveness.
  rejoiningStaleSnapshot: keep({ sent: sent(0, 1), node: node({ start: "start-2" }) }),
  // A host line not heard since the reboot: no later start is known yet.
  rebootingNoLine: keep({ sent: sent(), node: node({ start: null }) }),
  notRejoinedRebooting: keep({ sent: sent(), waitedMs: 600000 }),
  notRejoinedRejoining: keep({ sent: sent(), node: node({ start: "start-2" }), snapshot: snapshotOf({ interrupted: ["p-a"] }),
    waitedMs: 600000 }),
  stillRejoiningAt599: keep({ sent: sent(), node: node({ start: "start-2" }), snapshot: snapshotOf({ interrupted: ["p-a"] }),
    waitedMs: 599999 }),
  // No host line heard: the reboot goes over the Pi's link all the same.
  waitingNoHost: keep({ node: node({ start: null }) }),
  skipped: keep({ skipped: true }),
};
const evidenceOf = (input) => keepValue(input).evidence ?? null;
out.evidence = {
  rejoinedRebooted: evidenceOf({ sent: sent(), node: node({ start: "start-2" }) }),
  recognised: evidenceOf({ running: "v3.0.0" }),
  waiting: evidenceOf({}),
};

// --- A selected target whose snapshot is unread: no Done from no reads.
out.unreadPlan = uw.journeyStep({ releases: readOf({ on: "v2.0.0" }), rows: null }, { tag: "v2.0.0" },
  held({ rolling: true })).step;

// --- nextReboot: at most one in flight.
const rows = (...states) => states.map((state, index) => ({ playerId: `p-${index}`, state, label: state, pause: null }));
out.next = {
  first: uw.nextReboot(rows("waiting", "waiting", "waiting")),
  afterRejoined: uw.nextReboot(rows("rejoined", "waiting", "waiting")),
  afterSkipped: uw.nextReboot(rows("skipped", "rejoined", "waiting")),
  heldByRebooting: uw.nextReboot(rows("rejoined", "rebooting", "waiting")),
  heldByRejoining: uw.nextReboot(rows("rejoining", "waiting", "waiting")),
  heldByNotRejoined: uw.nextReboot(rows("not_rejoined", "waiting")),
  heldByCannot: uw.nextReboot(rows("cannot_reboot", "waiting")),
  heldByReading: uw.nextReboot(rows("reading", "waiting")),
  none: uw.nextReboot(rows("rejoined", "skipped")),
};
out.pause = {
  stall: uw.keepPause([{ playerId: "p-0", state: "rejoined", pause: null },
    { playerId: "p-1", state: "not_rejoined", label: "x", pause: "stalled" }]),
  cannot: uw.keepPause([{ playerId: "p-0", state: "cannot_reboot", label: "Cannot reboot: no session", pause: null }]),
  none: uw.keepPause(rows("rejoined", "waiting")),
};
out.count = uw.keepCount(rows("rejoined", "waiting", "rejoining"));

// --- Readiness, not liveness: a rebooted Player on a later boot, linking the target's app, whose
// bound Frame has a current readiness failure, is Not rejoined and holds the rollout.
const failingInput = { sent: sent(), node: node({ start: "start-2" }), snapshot: snapshotOf({ failing: ["p-a"] }) };
const failingRow = keepValue(failingInput);
const otherFailing = keepValue({ ...failingInput, snapshot: snapshotOf({ failing: ["p-b"] }) });
// The old boot's failure in a snapshot read before the new enrollment: not judged yet.
const staleFailing = keepValue({ ...failingInput, sent: sent(0, 1) });
out.readiness = {
  state: failingRow.state, label: failingRow.label, pause: failingRow.pause, basis: failingRow.evidence.basis,
  anotherPlayersFailure: otherFailing.state,
  staleFailing: staleFailing.state,
  next: uw.nextReboot([failingRow, { playerId: "p-b", state: "waiting", label: "Waiting", pause: null }]),
  paused: uw.keepPause([failingRow]),
  rejoinedBasis: evidenceOf({ sent: sent(), node: node({ start: "start-2" }) }).basis,
};

// --- The frozen rollout: only the Players a confirmation named are ever rebooted.
const entry = (id) => ({ playerId: id, deviceId: `dev-${id}`, name: id, frames: [] });
const livePlan = [entry("p-a"), entry("p-b"), entry("p-c")];
const frozen = uw.freezeRollout([entry("p-b"), entry("p-c"), entry("p-x")], new Set(["p-x"]));
const members = (skipped) => uw.rolloutMembers(livePlan, frozen, new Set(skipped))
  .map(({ entry: e, member }) => `${e.playerId}:${member}`);
const memberRows = uw.rolloutMembers(livePlan, frozen, new Set()).map(({ entry: e, member }) => (member === "planned"
  ? { playerId: e.playerId, state: "rejoined", label: "Rejoined", pause: null }
  : uw.keepRow({ node: node(), snapshot: snapshotOf(), playerId: e.playerId, target: T, sent: null,
    waitedMs: 0, skipped: member === "skipped", outside: member === "outside" })));
out.rollout = {
  frozen: frozen.map((e) => e.playerId),
  isFrozen: Object.isFrozen(frozen) && Object.isFrozen(frozen[0]),
  unfrozen: uw.rolloutMembers(livePlan, null, new Set(["p-b"])).map(({ entry: e, member }) => `${e.playerId}:${member}`),
  // p-a enrolled after the confirmation: outside. Skipped later: skipped.
  members: members([]),
  skippedLater: members(["p-c"]),
  outsideSkipped: members(["p-a"]),
  states: memberRows.map((value) => value.state),
  outsideLabel: memberRows[2].label,
  next: uw.nextReboot(memberRows),
  count: uw.keepCount(memberRows),
  step: uw.journeyStep({ releases: readOf({ on: "v2.0.0" }), rows: memberRows },
    { tag: "v2.0.0" }, held({ rolling: true })).step,
};
console.log(JSON.stringify(out));
"""


_BUNDLED = {}


def _run():
    """The script's answer, on updateWall.js bundled once by the console's esbuild."""
    if "out" not in _BUNDLED:
        _require_node()
        _require_esbuild()
        folder = Path(tempfile.mkdtemp(prefix="update-wall-"))
        entry = folder / "entry.js"
        entry.write_text(f'export * as uw from "{SRC / "updateWall.js"}";\n')
        bundle = folder / "entry.mjs"
        subprocess.run([str(ESBUILD), str(entry), "--bundle", "--format=esm", "--platform=node", f"--outfile={bundle}",
                        "--log-level=error"], check=True, timeout=60)
        result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, "--", bundle.as_uri(), OLD_APP, NEW_APP],
                                capture_output=True, text=True, timeout=30, check=True)
        _BUNDLED["out"] = json.loads(result.stdout)
    return _BUNDLED["out"]


def test_every_row_of_the_step_table_derives_from_the_reads():
    """Mutation probe: bring back a Try step (a tried Player reads `staging`) -> the table differs."""
    steps = _run()["steps"]
    assert steps == {
        "reading": "reading", "noRelease": "no_release", "rejected": "rejected", "choose": "choose",
        "chooseOnePlayerOn": "choose", "pausedOnOpen": "paused", "keeping": "keeping", "doneKept": "done",
        "selectionLost": "choose",
    }
    target = _run()["target"]
    assert target == {**target, "deployable": True, "app": NEW_APP, "baseIdentifies": False}
    assert set(target) == {"release", "deployable", "app", "baseIdentifies"}


SCOPE = ("Every Player Central knows boots this release from its next boot on. A Player that enrols later boots "
         "the newest ready release until it is given one.")


def test_put_on_the_wall_is_one_confirmation_naming_the_reboot_cost():
    put = _run()["put"]
    assert put["open"]["title"] == "Put release v2.0.0 on the wall?"
    assert put["open"]["lines"][:2] == ["Base v1.0.0 · app 222222…", SCOPE]
    assert put["open"]["lines"][-1] == "These are the Players this console knows; this page reboots them in this order:"
    assert put["open"]["after"] == [
        "Then this page reboots these Players one at a time, each after the previous one rejoins. Each Frame a "
        "Player drives is blank while it reboots. Keep this tab open: hiding or closing it pauses the rollout.",
        "Ready: Central has every file of v2.0.0."]
    assert put["open"]["confirmLabel"] == "Put it on the wall and reboot"
    assert put["openDownloading"]["after"][1:] == [
        "Downloading: Central still has 1.5 GB of v2.0.0 to download; a Player that boots it before then waits "
        "for the download.", "The first reboot waits until Central has downloaded it."]
    assert put["wait"] == [None, "Waiting for Central to download v2.0.0: 1.5 GB left",
                           "Waiting: Central could not download v2.0.0 (cache disk full)", None]
    assert put["rejectedTarget"]["deployable"] is False


def test_the_keep_plan_lists_the_players_by_name_and_skips_retired():
    assert _run()["plan"] == ["p-a", "p-b", "p-c"]


WAITING = ("Waiting · unknown whether it booted this release (another release a Player may run has the same base, "
           "so it cannot be recognised by its base); this page reboots it to make sure")


def test_every_row_of_the_keep_table_derives_from_the_reads():
    rows = _run()["rows"]
    states = {name: row["state"] for name, row in rows.items()}
    assert states == {
        "reading": "reading", "readFailed": "cannot_reboot", "waiting": "waiting", "waitingShared": "waiting",
        "recognised": "rejoined", "recognisedOtherBase": "waiting", "sharedBaseRunning": "waiting",
        "recognisedStalled": "not_rejoined",
        "rebootingHeldUnlisted": "rebooting", "rebootingListed": "rebooting", "rebootingNotDelivered": "rebooting",
        "rebootingOutcomeUnknown": "rebooting", "rebootingOutstanding": "rebooting",
        "rejoiningOutput": "rejoining", "rejoiningSilent": "rejoining", "rejoined": "rejoined",
        "rejoiningStaleSnapshot": "rejoining", "rebootingNoLine": "rebooting",
        "notRejoinedRebooting": "not_rejoined", "notRejoinedRejoining": "not_rejoined",
        "stillRejoiningAt599": "rejoining", "waitingNoHost": "waiting", "skipped": "skipped",
    }
    assert rows["rejoiningStaleSnapshot"]["label"] == (
        "Rejoining: Central's snapshot does not list the Player app's enrollment on the new boot yet")
    # The app operations' linked app is gone: a Player is recognised by the base its host reports
    # running, only when that base identifies the target; else it is rebooted to make sure.
    assert rows["waiting"]["label"] == "Waiting"
    assert rows["waitingShared"]["label"] == WAITING
    assert rows["sharedBaseRunning"]["label"] == WAITING
    assert rows["waitingNoHost"]["label"] == "Waiting"
    assert rows["recognisedStalled"]["pause"] == "This Player has not rejoined after 10 minutes"
    assert rows["rebootingHeldUnlisted"]["label"] == "Reboot sent by this page; Central has not listed it yet"
    assert rows["rebootingListed"]["label"].startswith("Requested · delivery unknown")
    assert rows["rebootingNotDelivered"]["pause"].startswith("Not delivered: the Pi had no link to Central")
    assert rows["rebootingOutcomeUnknown"]["pause"].startswith("Outcome unknown")
    assert rows["rebootingListed"]["pause"] is None
    assert rows["rejoiningOutput"]["label"] == "Rejoining: a bound Output's Frame is not live yet"
    assert rows["notRejoinedRejoining"]["pause"] == "This Player has not rejoined after 10 minutes"


def test_rows_on_the_release_carry_a_derived_fact_naming_their_basis():
    evidence = _run()["evidence"]
    assert evidence["rejoinedRebooted"]["kind"] == "derived"
    assert "host line is on a later start than the reboot this page sent" in evidence["rejoinedRebooted"]["basis"]
    assert evidence["recognised"]["kind"] == "derived"
    assert "the base its host reports running identifies this release" in evidence["recognised"]["basis"]
    assert evidence["waiting"] is None


def test_the_running_base_counts_only_when_it_identifies_the_target():
    """Mutation probe: drop the shared-base check -> `shared` reads True, fails."""
    assert _run()["identifies"] == {"identified": True, "shared": False, "noBase": False, "prerelease": True,
                                    "prereleasePrevious": False, "running": ["v3.0.0", None, None]}


def test_a_selected_target_with_an_unread_plan_reads_reading_never_done():
    assert _run()["unreadPlan"] == "reading"


def test_next_reboot_holds_while_any_earlier_row_is_unsettled():
    out = _run()
    assert out["next"] == {
        "first": "p-0", "afterRejoined": "p-1", "afterSkipped": "p-2", "heldByRebooting": None,
        "heldByRejoining": None, "heldByNotRejoined": None, "heldByCannot": None, "heldByReading": None,
        "none": None,
    }
    assert out["pause"] == {"stall": {"playerId": "p-1", "reason": "stalled"},
                            "cannot": {"playerId": "p-0", "reason": "Cannot reboot: no session"},
                            "none": None}
    assert out["count"] == "1 of 3 Players on this release"


# Any Central route, or any way of reaching one: the journey writes only through the verbs'
# send functions, which stay the only callers of their routes (their own source-scan tests).
_ROUTE = re.compile(r"/v[12]/|apiWrite|fetch\(|/reboots|/boot-policy|/deployments"
                    r"|/install|installOnPlayer|setWallVersion")


def test_the_journey_modules_name_no_route():
    for name in ("updateWall.js", "UpdateWallPage.jsx"):
        source = (SRC / name).read_text()
        assert _ROUTE.search(source) is None, f"{name} names a route: {_ROUTE.search(source).group()}"
    # Positive control: the pattern does find the verbs' own routes in their homes.
    assert _ROUTE.search((SRC / "fleetCommands.js").read_text()) is not None
    page = (SRC / "UpdateWallPage.jsx").read_text()
    for send in ("sendWallVersion(", "sendReboot("):
        assert send in page, send
    # The app steps are gone (S4c-2a-iii): no Stage, no qualification, no Try.
    for gone in ("sendStage", "QualifiedFallback", "StageApp", "Try on one Frame", "Back out"):
        assert gone not in page, gone


DECODE = "The Player could not decode this assignment. Check that the media is supported, or choose another item."


def test_a_current_readiness_failure_is_not_rejoined_and_holds_the_rollout():
    """Liveness is not readiness: a later boot whose bound Frame has a current readiness failure is Not rejoined, pauses with the failure's recovery words, and the
    next Player is not rebooted."""
    readiness = _run()["readiness"]
    assert readiness["state"] == "not_rejoined"
    assert readiness["anotherPlayersFailure"] == "rejoined" and readiness["staleFailing"] == "rejoining"
    assert readiness["label"] == f"Not rejoined: it reports a readiness failure. {DECODE}"
    assert readiness["pause"] == f"it reports a readiness failure on a bound Output: {DECODE[:-1]}"
    assert "current readiness failure" in readiness["basis"]
    assert readiness["next"] is None
    assert readiness["paused"] == {"playerId": "p-a", "reason": readiness["pause"]}
    # The Rejoined basis names what was checked, and says what it does not prove.
    assert "no current readiness failure" in readiness["rejoinedBasis"]
    assert "not proof it is showing its assignment" in readiness["rejoinedBasis"]
    assert "reports ready" not in readiness["rejoinedBasis"]


def test_only_the_frozen_rollout_is_rebooted_a_later_player_is_not_in_it():
    rollout = _run()["rollout"]
    assert rollout["frozen"] == ["p-b", "p-c"] and rollout["isFrozen"] is True
    assert rollout["unfrozen"] == ["p-a:planned", "p-b:skipped", "p-c:planned"]
    assert rollout["members"] == ["p-b:planned", "p-c:planned", "p-a:outside"]
    assert rollout["skippedLater"] == ["p-b:planned", "p-c:skipped", "p-a:outside"]
    assert rollout["outsideSkipped"] == ["p-b:planned", "p-c:planned", "p-a:skipped"]
    assert rollout["states"] == ["rejoined", "rejoined", "not_in_rollout"]
    assert rollout["outsideLabel"] == "Not in this rollout: no confirmation named it; this page never reboots it"
    assert rollout["next"] is None
    assert rollout["count"] == "2 of 2 Players on this release"
    assert rollout["step"] == "done"
