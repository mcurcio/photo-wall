"""Update the wall, its pure parts (console DDD Part E §25a, beads NU1 and B8): updateWall.js
(`journeyStep` over every row of the step table, the one Put-on-the-wall confirmation in both
gate forms and its download wait, `keepPlan`'s order, `keepRow` over every row of the Keep table,
`nextReboot`'s one-in-flight rule, `keepPause`), run under Node as
tests/test_console_stage.py runs stage.js, and the static rule that the journey's modules name no
route (every write goes through its verb's one send function). Without Node it skips on a
developer machine, but FAILS where the checks are meant to run in full. The browser half is in
tests/browser/test_update_wall_browser.py.
"""

import json
import re
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"
OLD_APP = "11" * 32
NEW_APP = "22" * 32

SCRIPT = r"""
const uw = await import(process.argv[1]);
const [OLD_APP, NEW_APP] = [process.argv[2], process.argv[3]];
const out = {};

const MANIFEST = "ab".repeat(32);
const NOAPP_MANIFEST = "cd".repeat(32);
const BASE_MANIFEST = "ef".repeat(32);
const TARGET = "abababab-abab-8bab-abab-abababababab";
const release = (manifest, tag, base, app, extra = {}) => ({ manifest_sha256: manifest, tag, revision: "a".repeat(40),
  discovered_at: 900, base_tag: base, app_environment_sha256: app, stable: true, problem: null,
  deployment_id: `${manifest.slice(0, 8)}-0000-8000-8000-000000000000`, in_window: true, readiness: "ready",
  readiness_reason: null, missing_bytes: 0, ...extra });
const releases = [release(MANIFEST, "v2.0.0", "v1.0.0", NEW_APP, { deployment_id: TARGET }),
  release(NOAPP_MANIFEST, "v2.1.0", "v1.0.0", null), release(BASE_MANIFEST, "v3.0.0", "v3.0.0", NEW_APP)];
// A tag whose every upload was refused: no deployment.
const REJECTED = release("ff".repeat(32), "v4.0.0", null, null, { manifest_sha256: null, deployment_id: null,
  problem: "node_release_invalid", readiness: null, missing_bytes: null });
const old = { deployment_id: "d0000000-0000-8000-8000-000000000000", published_at: 800, base_tag: "v1.0.0",
  app_environment_sha256: OLD_APP };
const target = { deployment_id: TARGET, published_at: 850, base_tag: "v1.0.0", app_environment_sha256: NEW_APP };
// The target's deployment is beyond the capped Deployments list unless `listed`: its own row names it.
const readOf = ({ listed = true, selected = old.deployment_id, extra = [], rows = releases } = {}) => ({ read_at: 1000,
  selection: { deployment_id: selected, revision: 4, changed_at: 900, previous_deployment_id: null },
  releases: [...rows, REJECTED], deployments: [old, ...(listed ? [target] : []), ...extra] });
const qual = (linked, accepted = []) => ({ linked_app: linked === null ? null : { environment_sha256: linked, admitted_at: 1 },
  acceptances: accepted.map((sha) => ({ environment_sha256: sha, base_content_key: "k", base_tag: "v1.0.0", accepted_at: 2 })) });
const ops = (operations, linked = OLD_APP, accepted = [OLD_APP]) => ({ read_at: 1000, operations, qualification: qual(linked, accepted) });
const op = (id, state) => ({ operation_id: id, command_id: `c-${id}`, state, command_response: null, latest_effect: null });
const held = (extra = {}) => ({ sampling: false, qualified: false, needQualify: false, backingOut: false, stageId: null,
  rolling: false, ...extra });
const step = (reads, tried, h = held()) => {
  const s = uw.journeyStep(reads, { tag: reads.tag ?? "v2.0.0", tried }, h);
  return s.how ? `${s.step}:${s.how}` : s.step;
};
const r = (releasesRead, tried = null, rows = null, tag) => ({ releases: releasesRead, tried, rows, tag });

// --- journeyStep: every row of the step table, and the derivations around it.
out.steps = {
  reading: step(r(null), null),
  noRelease: step({ ...r(readOf()), tag: "v9.9.9" }, null),
  rejected: step({ ...r(readOf()), tag: "v4.0.0" }, null),
  choose: step(r(readOf()), null),
  // Beyond the capped Deployments list: the release row's own deployment is enough.
  chooseUnlisted: step(r(readOf({ listed: false })), null),
  // Tried, no stored acceptance for the linked app: qualify first.
  qualifyingNoAcceptance: step(r(readOf(), { operations: ops([], OLD_APP, []) }), "p-1"),
  qualifyingSampling: step(r(readOf(), { operations: ops([]) }), "p-1", held({ sampling: true })),
  // Central refused the stage for a missing fallback: qualify once although one is listed.
  qualifyingAfterRefusal: step(r(readOf(), { operations: ops([]) }), "p-1", held({ needQualify: true })),
  stagingAccepted: step(r(readOf(), { operations: ops([]) }), "p-1"),
  stagingQualifiedHere: step(r(readOf(), { operations: ops([], OLD_APP, []) }), "p-1", held({ qualified: true })),
  stagingHeldInProgress: step(r(readOf(), { operations: ops([op("o-1", "switching")]) }), "p-1", held({ stageId: "o-1" })),
  // A stage this page does not hold, switching: not provably the target's, so Stage is offered.
  stagingOtherSwitching: step(r(readOf(), { operations: ops([op("o-x", "switching")]) }), "p-1"),
  lookingHeld: step(r(readOf(), { operations: ops([op("o-1", "fallback_running")]) }), "p-1", held({ stageId: "o-1" })),
  lookingUnknown: step(r(readOf(), { operations: ops([op("o-1", "effect_unknown")]) }), "p-1", held({ stageId: "o-1" })),
  // After a reload: target_running with the target's app linked is the target's stage.
  lookingRederived: step(r(readOf(), { operations: ops([op("o-2", "target_running")], NEW_APP) }), "p-1"),
  // target_running with another app linked is not this target's stage.
  notLookingOtherApp: step(r(readOf(), { operations: ops([op("o-2", "target_running")], OLD_APP) }), "p-1"),
  backingOut: step(r(readOf(), { operations: ops([op("o-1", "target_running")], NEW_APP) }), "p-1",
    held({ stageId: "o-1", backingOut: true })),
  backedOutInterrupted: step(r(readOf(), { operations: ops([op("o-1", "interrupted_by_reboot")]) }), "p-1",
    held({ stageId: "o-1", backingOut: true })),
  backedOutEnded: step(r(readOf(), { operations: ops([op("o-1", "ended_by_later_boot")]) }), "p-1",
    held({ stageId: "o-1", backingOut: true })),
  pausedOnOpen: step(r(readOf({ selected: TARGET }), null, [{ state: "rejoined" }, { state: "waiting" }]), null),
  keeping: step(r(readOf({ selected: TARGET }), null, [{ state: "rejoined" }, { state: "waiting" }]), null,
    held({ rolling: true })),
  doneKept: step(r(readOf({ selected: TARGET }), null, [{ state: "rejoined" }, { state: "skipped" }]), null,
    held({ rolling: true })),
  // A selection the target lost (another page selected) goes back to Choose.
  selectionLost: step(r(readOf({ selected: old.deployment_id })), null, held({ rolling: true })),
};

// --- Try's pre-check: a hint only.
const targetOf = (tag, read = readOf()) => uw.journeyTarget(read, tag);
out.withdrawn = {
  sameBase: uw.tryWithdrawn(readOf(), targetOf("v2.0.0")),
  baseChanges: uw.tryWithdrawn(readOf(), targetOf("v3.0.0")),
  noApp: uw.tryWithdrawn(readOf(), targetOf("v2.1.0")),
};
out.target = targetOf("v2.0.0");

// --- Put vX on the wall: ONE confirmation, in its two gate forms, with the readiness line.
const request = { contents: "Base v1.0.0 · app 222222…", noApp: false, body: { deployment_id: TARGET, expected_revision: 4 } };
const downloading = targetOf("v2.0.0", readOf({ rows: [release(MANIFEST, "v2.0.0", "v1.0.0", NEW_APP,
  { deployment_id: TARGET, readiness: "downloading", missing_bytes: 1500000000 })] }));
out.put = {
  closed: uw.putConfirmation(request, targetOf("v2.0.0"), false),
  open: uw.putConfirmation(request, targetOf("v2.0.0"), true),
  openDownloading: uw.putConfirmation(request, downloading, true),
  wait: [uw.downloadWait(targetOf("v2.0.0")), uw.downloadWait(downloading),
    uw.downloadWait({ release: { tag: "v2.0.0", readiness: "failed", readiness_reason: "cache_disk_full" } }),
    uw.downloadWait({ release: { tag: "v2.0.0", readiness: null } })],
  rejectedTarget: targetOf("v4.0.0"),
};

// --- keepPlan: known, not retired Players; the tried one first, then by name.
const player = (id, serial, extra = {}) => ({ id, device_id: `dev-${id}`, registered_at: 1, retired_at: null,
  last_seen: 990, last_report_at: 995, authority_epoch: 1, ...extra });
const snapshotOf = ({ interrupted = [], silent = [], failing = [] } = {}) => ({
  readAt: 1000,
  inventory: {
    read_at: 1000, silent_after_seconds: 30, report_interval_seconds: 5,
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
out.plan = uw.keepPlan(snapshotOf(), { devices: boot }, "p-c").map((entry) => entry.playerId);
out.planUntried = uw.keepPlan(snapshotOf(), { devices: boot }, null).map((entry) => entry.playerId);

// --- keepRow: every row of the Keep table.
const open = { effective_state: "open", state: "open", generation: 7 };
const closed = { effective_state: "closed", state: "closed", reason: "never_certified", generation: 6, changed_at: 900 };
const host = (bootId) => ({ session_id: `s-${bootId}`, current: true, scope: "operator_reboot", command_eligible: true,
  producer: { owner: "host_core", kernel_boot_id: bootId } });
const command = (id, extra = {}) => ({ command_id: id, issued_at: 990, expires_at: 1020, outstanding: true,
  command: { command_session_id: "s-boot-1", producer: { kernel_boot_id: "boot-1" } }, responses: [], effects: [], ...extra });
const node = ({ bootId = "boot-1", linked = OLD_APP, commands = [] } = {}) => ({
  read: { device_generation: 3, read_at: 1000, sessions: [host(bootId)], reboot_commands: commands, boot_claims: [] },
  operations: ops([], linked), readAt: 1000, error: null });
// Sent on a snapshot listing authority epoch 0; snapshotOf lists epoch 1 (the new boot's enrollment).
const sent = (atMs = 0, epoch = 0) => ({ request: { body: { command_id: "cmd-1" }, kernelBootId: "boot-1" }, atMs, epoch });
// v2.0.0's app is carried by no other release on another base, so it identifies v2.0.0; in the
// full catalog v3.0.0 pairs the same app with base v3.0.0, a base-only release of it.
const IDENTIFIED_READ = { ...readOf(), releases: releases.slice(0, 2) };
// A pre-release pairing the same app with another base is not a release any Player runs.
const PRE = release(BASE_MANIFEST, "v3.0.0-rc.1", "v3.0.0", NEW_APP, { stable: false });
const PRE_READ = { ...readOf(), releases: [...releases.slice(0, 2), PRE],
  deployments: [old, target, { deployment_id: PRE.deployment_id, published_at: 870, base_tag: "v3.0.0",
    app_environment_sha256: NEW_APP }] };
// ...unless it is the previous selection: a Player not rebooted since may still run it.
const PRE_PREVIOUS = { ...PRE_READ, selection: { ...PRE_READ.selection, previous_deployment_id: PRE.deployment_id } };
const T = targetOf("v2.0.0", IDENTIFIED_READ);
const SHARED = targetOf("v2.0.0");
const BASE_ONLY = targetOf("v3.0.0");
const NOAPP = targetOf("v2.1.0");
out.identifies = { identified: T.appIdentifies, sharedApp: SHARED.appIdentifies, baseOnly: BASE_ONLY.appIdentifies,
  noApp: NOAPP.appIdentifies, pure: uw.appIdentifiesTarget(IDENTIFIED_READ, T),
  prerelease: uw.appIdentifiesTarget(PRE_READ, T), prereleasePrevious: uw.appIdentifiesTarget(PRE_PREVIOUS, T) };
const keepValue = (input) => uw.keepRow({ node: node(), snapshot: snapshotOf(), playerId: "p-a", target: T, gate: open,
  sent: null, waitedMs: 0, skipped: false, ...input });
const keep = (input) => {
  const value = keepValue(input);
  return { state: value.state, label: value.label, pause: value.pause };
};
out.rows = {
  reading: keep({ node: { read: null, operations: null, readAt: null, error: null } }),
  readFailed: keep({ node: { read: null, operations: null, readAt: null, error: { code: "node_device_unavailable", status: 404 } } }),
  waiting: keep({}),
  waitingNoApp: keep({ target: NOAPP }),
  rebootingHeldUnlisted: keep({ sent: sent() }),
  rebootingListed: keep({ sent: sent(), node: node({ commands: [command("cmd-1")] }) }),
  rebootingRejected: keep({ sent: sent(), node: node({ commands: [command("cmd-1", { responses: [
    { received_at: 995, message: { message: { decision: "rejected", reason: "busy" } } }] })] }) }),
  rebootingOutcomeUnknown: keep({ sent: sent(), node: node({ commands: [command("cmd-1", { expires_at: 999 })] }) }),
  // Another page's reboot that Central lists as outstanding.
  rebootingOutstanding: keep({ node: node({ commands: [command("cmd-other")] }) }),
  rejoiningApp: keep({ sent: sent(), node: node({ bootId: "boot-2", linked: OLD_APP }) }),
  rejoiningOutput: keep({ sent: sent(), node: node({ bootId: "boot-2", linked: NEW_APP }), snapshot: snapshotOf({ interrupted: ["p-a"] }) }),
  rejoiningSilent: keep({ sent: sent(), node: node({ bootId: "boot-2", linked: NEW_APP }), snapshot: snapshotOf({ silent: ["p-a"] }) }),
  rejoined: keep({ sent: sent(), node: node({ bootId: "boot-2", linked: NEW_APP }) }),
  // A later boot, but a snapshot read before its Player app enrolled: the old boot's liveness.
  rejoiningStaleSnapshot: keep({ sent: sent(0, 1), node: node({ bootId: "boot-2", linked: NEW_APP }) }),
  // Not rebooted by this page, its linked app the target's (the no-G7 inference).
  rejoinedInferred: keep({ node: node({ linked: NEW_APP }) }),
  // A release with no app: rejoined only once this page's reboot reached a later boot.
  rejoinedNoApp: keep({ target: NOAPP, sent: sent(), node: node({ bootId: "boot-2" }) }),
  notRejoinedRebooting: keep({ sent: sent(), waitedMs: 600000 }),
  notRejoinedRejoining: keep({ sent: sent(), node: node({ bootId: "boot-2", linked: OLD_APP }), waitedMs: 600000 }),
  stillRejoiningAt599: keep({ sent: sent(), node: node({ bootId: "boot-2", linked: OLD_APP }), waitedMs: 599999 }),
  cannotGate: keep({ gate: closed }),
  cannotNoHost: keep({ node: { ...node(), read: { ...node().read, sessions: [] } } }),
  skipped: keep({ skipped: true }),
  // A base-only release: its app is the old release's too, so a Player not rebooted by this page
  // whose linked app is that app is NOT on the selection (it may still run the old base).
  baseOnlyNotRebooted: keep({ target: BASE_ONLY, node: node({ linked: NEW_APP }) }),
  sharedAppNotRebooted: keep({ target: SHARED, node: node({ linked: NEW_APP }) }),
  // ...and once this page's reboot reached a later boot, it is.
  baseOnlyRebooted: keep({ target: BASE_ONLY, sent: sent(), node: node({ bootId: "boot-2", linked: NEW_APP }) }),
  // The tried Player after Try: the target's app runs as a Stage on the boot it had before Select.
  triedStageRunning: keep({ node: { ...node({ linked: NEW_APP }),
    operations: ops([op("o-1", "target_running")], NEW_APP) } }),
  // A stage a later boot ended: the boot's own app is linked again, so it counts.
  stageEndedByLaterBoot: keep({ node: { ...node({ linked: NEW_APP }),
    operations: ops([op("o-1", "ended_by_later_boot")], NEW_APP) } }),
};
const evidenceOf = (input) => keepValue(input).evidence ?? null;
out.evidence = {
  rejoinedRebooted: evidenceOf({ sent: sent(), node: node({ bootId: "boot-2", linked: NEW_APP }) }),
  rejoinedInferred: evidenceOf({ node: node({ linked: NEW_APP }) }),
  waiting: evidenceOf({}),
};

// --- The same session, a base-only release selected: no Player reads Rejoined, nothing is Done,
// and the first Player is rebooted (the bug: every row read Rejoined, Done with zero reboots).
const baseOnlyRows = ["p-a", "p-b", "p-c"].map((id) => uw.keepRow({ node: node({ linked: NEW_APP }),
  snapshot: snapshotOf(), playerId: id, target: BASE_ONLY, gate: open, sent: null, waitedMs: 0, skipped: false }));
const BASE_ONLY_READ = readOf({ selected: BASE_ONLY.deploymentId,
  extra: [{ deployment_id: BASE_ONLY.deploymentId, published_at: 860, base_tag: "v3.0.0", app_environment_sha256: NEW_APP }] });
out.baseOnlySession = {
  states: baseOnlyRows.map((value) => value.state),
  step: uw.journeyStep({ releases: BASE_ONLY_READ, tried: null, rows: baseOnlyRows }, { tag: "v3.0.0", tried: null },
    held({ rolling: true })).step,
  next: uw.nextReboot(baseOnlyRows),
  count: uw.keepCount(baseOnlyRows),
};
// The tried Player first in the plan, running a Stage: it is the next reboot, not skipped as done.
const triedRows = [uw.keepRow({ node: { ...node({ linked: NEW_APP }), operations: ops([op("o-1", "target_running")], NEW_APP) },
  snapshot: snapshotOf(), playerId: "p-c", target: T, gate: open, sent: null, waitedMs: 0, skipped: false }),
uw.keepRow({ node: node({ linked: NEW_APP }), snapshot: snapshotOf(), playerId: "p-a", target: T, gate: open, sent: null,
  waitedMs: 0, skipped: false })];
out.triedFirst = { states: triedRows.map((value) => value.state), next: uw.nextReboot(triedRows) };

// --- A selected target whose snapshot is unread: no Done from no reads.
out.unreadPlan = uw.journeyStep({ releases: readOf({ selected: TARGET }), tried: null, rows: null },
  { tag: "v2.0.0", tried: null }, held({ rolling: true })).step;

// --- stageFollowUp keys on Central's code: rewording a refusal keeps the journey's behaviour.
const outcome = (kind, code, message = "any words.") => ({ outcome: kind, message, code });
out.followUp = {
  done: uw.stageFollowUp(outcome("done", null), 0),
  already: uw.stageFollowUp(outcome("already", null), 0),
  unknown: uw.stageFollowUp(outcome("unknown", null), 0),
  changed: uw.stageFollowUp({ outcome: "changed", message: "stale." }, 0),
  fallback: uw.stageFollowUp(outcome("refused", "node_app_qualified_fallback_required"), 0),
  fallbackAgain: uw.stageFollowUp(outcome("refused", "node_app_qualified_fallback_required"), 1),
  baseMismatch: uw.stageFollowUp(outcome("refused", "node_app_target_base_mismatch"), 0),
  other: uw.stageFollowUp(outcome("refused", "node_deployment_unknown"), 0),
  // The old words with another code, and the code with no words: the code decides.
  wordsWithoutCode: uw.stageFollowUp(outcome("refused", "node_deployment_unknown",
    "No qualified fallback for this Player's current Outputs and base: qualify the running app first."), 0),
  codeReworded: uw.stageFollowUp(outcome("refused", "node_app_target_base_mismatch", ""), 0),
};
const { stageResult } = await import(new URL("./stage.js", process.argv[1]).href);
out.stageCodes = {
  fallback: stageResult({ ok: false, status: 409, error: "node_app_qualified_fallback_required", data: null }, open).code,
  done: stageResult({ ok: true, status: 201, error: null, data: { duplicate: false } }, open).code,
  lost: stageResult(null, open).code,
};
out.gateFlag = uw.keepRow({ node: node(), snapshot: snapshotOf(), playerId: "p-a", target: T, gate: closed, sent: null,
  waitedMs: 0, skipped: false }).gate === true;

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
  cannot: uw.keepPause([{ playerId: "p-0", state: "cannot_reboot", label: "Cannot reboot: gate", pause: null, gate: true }]),
  none: uw.keepPause(rows("rejoined", "waiting")),
};
out.count = uw.keepCount(rows("rejoined", "waiting", "rejoining"));

// --- Readiness, not liveness: a rebooted Player on a later boot, linking the target's app, whose
// bound Frame has a current readiness failure, is Not rejoined and holds the rollout.
const failingInput = { sent: sent(), node: node({ bootId: "boot-2", linked: NEW_APP }), snapshot: snapshotOf({ failing: ["p-a"] }) };
const failingRow = keepValue(failingInput);
const failingInferred = keepValue({ node: node({ linked: NEW_APP }), snapshot: snapshotOf({ failing: ["p-a"] }) });
const otherFailing = keepValue({ ...failingInput, snapshot: snapshotOf({ failing: ["p-b"] }) });
// The old boot's failure in a snapshot read before the new enrollment: not judged yet.
const staleFailing = keepValue({ ...failingInput, sent: sent(0, 1) });
out.readiness = {
  state: failingRow.state, label: failingRow.label, pause: failingRow.pause, basis: failingRow.evidence.basis,
  inferred: failingInferred.state,
  anotherPlayersFailure: otherFailing.state,
  staleFailing: staleFailing.state,
  next: uw.nextReboot([failingRow, { playerId: "p-b", state: "waiting", label: "Waiting", pause: null }]),
  paused: uw.keepPause([failingRow]),
  rejoinedBasis: evidenceOf({ sent: sent(), node: node({ bootId: "boot-2", linked: NEW_APP }) }).basis,
};

// --- The frozen rollout: only the Players a confirmation named are ever rebooted.
const entry = (id) => ({ playerId: id, deviceId: `dev-${id}`, name: id, frames: [] });
const livePlan = [entry("p-a"), entry("p-b"), entry("p-c")];
const frozen = uw.freezeRollout([entry("p-b"), entry("p-c"), entry("p-x")], new Set(["p-x"]));
const members = (skipped) => uw.rolloutMembers(livePlan, frozen, new Set(skipped))
  .map(({ entry: e, member }) => `${e.playerId}:${member}`);
const memberRows = uw.rolloutMembers(livePlan, frozen, new Set()).map(({ entry: e, member }) => (member === "planned"
  ? { playerId: e.playerId, state: "rejoined", label: "Rejoined", pause: null }
  : uw.keepRow({ node: node(), snapshot: snapshotOf(), playerId: e.playerId, target: T, gate: open, sent: null,
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
  step: uw.journeyStep({ releases: readOf({ selected: TARGET }), tried: null, rows: memberRows },
    { tag: "v2.0.0", tried: null }, held({ rolling: true })).step,
};
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "updateWall.js").as_uri(), OLD_APP, NEW_APP],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_every_row_of_the_step_table_derives_from_the_reads():
    steps = _run()["steps"]
    assert steps == {
        "reading": "reading", "noRelease": "no_release", "rejected": "rejected", "choose": "choose",
        "chooseUnlisted": "choose",
        "qualifyingNoAcceptance": "qualifying", "qualifyingSampling": "qualifying",
        "qualifyingAfterRefusal": "qualifying", "stagingAccepted": "staging", "stagingQualifiedHere": "staging",
        "stagingHeldInProgress": "staging", "stagingOtherSwitching": "staging", "lookingHeld": "looking",
        "lookingUnknown": "looking", "lookingRederived": "looking", "notLookingOtherApp": "staging",
        "backingOut": "backing_out", "backedOutInterrupted": "done:backed_out", "backedOutEnded": "done:backed_out",
        "pausedOnOpen": "paused", "keeping": "keeping", "doneKept": "done:kept", "selectionLost": "choose",
    }
    assert len(steps) == 23


def test_try_is_withdrawn_for_a_base_change_or_no_app_as_a_hint():
    out = _run()
    assert out["withdrawn"] == {
        "sameBase": None,
        "baseChanges": ("This release changes the base, so it cannot be tried live; putting it on the wall "
                        "reboots each Player onto it"),
        "noApp": ("This release has no app, so there is nothing to try live; putting it on the wall reboots each "
                  "Player onto it"),
    }
    assert out["target"]["withApp"] is True and out["target"]["deploymentId"] == "abababab-abab-8bab-abab-abababababab"
    assert out["target"]["app"] == NEW_APP


SCOPE = ("Every Player that boots by node path from now on is offered this deployment, including Players Central "
         "has not seen. Central cannot list which Players will boot.")


def test_put_on_the_wall_is_one_confirmation_naming_the_reboot_cost_in_both_gate_forms():
    put = _run()["put"]
    assert put["closed"] == {
        "title": "Put release v2.0.0 on the wall?",
        "lines": ["Base v1.0.0 · app 222222…", SCOPE, "Selects v2.0.0 for every boot.",
                  "No Player is rebooted; each gets it at its next boot."],
        "after": ["Ready: Central has every file of v2.0.0."],
        "confirmLabel": "Select for every boot",
    }
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
    assert put["rejectedTarget"]["deploymentId"] is None


def test_the_keep_plan_puts_the_tried_player_first_then_the_rest_by_name_and_skips_retired():
    out = _run()
    assert out["plan"] == ["p-c", "p-a", "p-b"]
    assert out["planUntried"] == ["p-a", "p-b", "p-c"]


def test_every_row_of_the_keep_table_derives_from_the_reads():
    rows = _run()["rows"]
    states = {name: row["state"] for name, row in rows.items()}
    assert states == {
        "reading": "reading", "readFailed": "cannot_reboot", "waiting": "waiting", "waitingNoApp": "waiting",
        "rebootingHeldUnlisted": "rebooting", "rebootingListed": "rebooting", "rebootingRejected": "rebooting",
        "rebootingOutcomeUnknown": "rebooting", "rebootingOutstanding": "rebooting", "rejoiningApp": "rejoining",
        "rejoiningOutput": "rejoining", "rejoiningSilent": "rejoining", "rejoined": "rejoined",
        "rejoiningStaleSnapshot": "rejoining",
        "rejoinedInferred": "rejoined", "rejoinedNoApp": "rejoined", "notRejoinedRebooting": "not_rejoined",
        "notRejoinedRejoining": "not_rejoined", "stillRejoiningAt599": "rejoining", "cannotGate": "cannot_reboot",
        "cannotNoHost": "cannot_reboot", "skipped": "skipped", "baseOnlyNotRebooted": "waiting",
        "sharedAppNotRebooted": "waiting", "baseOnlyRebooted": "rejoined", "triedStageRunning": "waiting",
        "stageEndedByLaterBoot": "rejoined",
    }
    assert len(states) == 27
    assert rows["rejoiningStaleSnapshot"]["label"] == (
        "Rejoining: Central's snapshot does not list the Player app's enrollment on the new boot yet")
    assert rows["waiting"]["label"] == "Waiting"
    assert rows["baseOnlyNotRebooted"]["label"] == (
        "Waiting · unknown whether it booted the selection (another release carries the same app on a different "
        "base, so it cannot be recognised by its app); this page reboots it to make sure")
    assert rows["sharedAppNotRebooted"]["label"] == rows["baseOnlyNotRebooted"]["label"]
    assert rows["triedStageRunning"]["label"] == (
        "Waiting · it runs a Stage, which applies to this boot only; this page reboots it; its next boot is offered the selection")
    assert rows["waitingNoApp"]["label"].startswith("Waiting · unknown whether it booted the selection")
    assert rows["rebootingHeldUnlisted"]["label"] == "Reboot sent by this page; Central has not listed it yet"
    assert rows["rebootingListed"]["label"].startswith("Requested · delivery unknown")
    assert rows["rebootingRejected"]["pause"] == "Rejected by Host Management"
    assert rows["rebootingOutcomeUnknown"]["pause"].startswith("Outcome unknown")
    assert rows["rebootingListed"]["pause"] is None
    assert rows["rejoiningApp"]["label"] == f"Rejoining: the Player app has not linked app {NEW_APP[:6]}… yet"
    assert rows["rejoiningOutput"]["label"] == "Rejoining: a bound Output's Frame is not live yet"
    assert rows["notRejoinedRejoining"]["pause"] == "This Player has not rejoined after 10 minutes"
    assert rows["cannotGate"]["label"].startswith("Cannot reboot: Effect gate closed")
    assert _run()["gateFlag"] is True


def test_the_linked_app_counts_only_when_it_identifies_the_target():
    out = _run()
    assert out["identifies"] == {"identified": True, "sharedApp": False, "baseOnly": False, "noApp": False,
                                 "pure": True, "prerelease": True, "prereleasePrevious": False}


def test_a_base_only_release_reboots_every_player_in_the_same_session_and_is_not_done():
    session = _run()["baseOnlySession"]
    assert session == {"states": ["waiting", "waiting", "waiting"], "step": "keeping", "next": "p-a",
                       "count": "0 of 3 Players on the selection"}


def test_the_tried_player_running_a_stage_is_rebooted_first_not_counted_as_on_the_selection():
    tried = _run()["triedFirst"]
    assert tried == {"states": ["waiting", "rejoined"], "next": "p-c"}


def test_rows_on_the_selection_carry_a_derived_fact_naming_their_basis():
    evidence = _run()["evidence"]
    assert evidence["rejoinedRebooted"]["kind"] == "derived"
    assert "later kernel boot than the reboot this page sent" in evidence["rejoinedRebooted"]["basis"]
    assert evidence["rejoinedInferred"]["kind"] == "derived"
    assert "linked app identifies this release" in evidence["rejoinedInferred"]["basis"]
    assert evidence["waiting"] is None


def test_a_selected_target_with_an_unread_plan_reads_reading_never_done():
    assert _run()["unreadPlan"] == "reading"


def test_the_stage_follow_up_keys_on_centrals_code_never_its_words():
    out = _run()
    assert out["followUp"] == {
        "done": "held", "already": "held", "unknown": "stay", "changed": "stay", "fallback": "qualify",
        "fallbackAgain": "choose", "baseMismatch": "withdraw", "other": "choose", "wordsWithoutCode": "choose",
        "codeReworded": "withdraw",
    }
    assert out["stageCodes"] == {"fallback": "node_app_qualified_fallback_required", "done": None, "lost": None}


def test_next_reboot_holds_while_any_earlier_row_is_unsettled():
    out = _run()
    assert out["next"] == {
        "first": "p-0", "afterRejoined": "p-1", "afterSkipped": "p-2", "heldByRebooting": None,
        "heldByRejoining": None, "heldByNotRejoined": None, "heldByCannot": None, "heldByReading": None,
        "none": None,
    }
    assert out["pause"] == {"stall": {"playerId": "p-1", "reason": "stalled"},
                            "cannot": {"playerId": "p-0", "reason": "Cannot reboot: gate", "gate": True},
                            "none": None}
    assert out["count"] == "1 of 3 Players on the selection"


# Any Central route, or any way of reaching one: the journey writes only through the verbs'
# send functions, which stay the only callers of their routes (their own source-scan tests).
_ROUTE = re.compile(r"/v[12]/|apiWrite|fetch\(|/reboots|/app-stages|/app-qualifications|/boot-policy|/deployments")


def test_the_journey_modules_name_no_route():
    for name in ("updateWall.js", "UpdateWallPage.jsx"):
        source = (SRC / name).read_text()
        assert _ROUTE.search(source) is None, f"{name} names a route: {_ROUTE.search(source).group()}"
    # Positive control: the pattern does find the verbs' own routes in their homes.
    assert _ROUTE.search((SRC / "fleetCommands.js").read_text()) is not None
    page = (SRC / "UpdateWallPage.jsx").read_text()
    for send in ("sendStage(", "sendSelection(", "sendReboot("):
        assert send in page, send
    assert "<QualifiedFallback" in page  # sendBegin and the sampler, through their one component


DECODE = "The Player could not decode this assignment. Check that the media is supported, or choose another item."


def test_a_current_readiness_failure_is_not_rejoined_and_holds_the_rollout():
    """Liveness is not readiness: a later boot linking the target's app whose bound Frame has a
    current readiness failure is Not rejoined, pauses with the failure's recovery words, and the
    next Player is not rebooted."""
    readiness = _run()["readiness"]
    assert readiness["state"] == "not_rejoined" and readiness["inferred"] == "not_rejoined"
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
    assert rollout["count"] == "2 of 2 Players on the selection"
    assert rollout["step"] == "done"
