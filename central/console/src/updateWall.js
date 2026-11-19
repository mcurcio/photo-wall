import { appHandle, fact, words } from "./facts.js";
import { hostEpoch, rebootCommandState, rebootOffer, rebootTarget } from "./fleetCommands.js";
import { frameHealth, outputStates } from "./health.js";
import { playersByDevice } from "./players.js";
import { readinessRecoveryForFrame } from "./readinessRecovery.js";
import { bootsNext, deployable, downloadSize, readinessLine, wallOn } from "./domain/releases/model";
import { WALL_SCOPE } from "./releases.js";

/**
 * Update the wall (console DDD Part E §25a, beads NU1 and B8): the guided journey "put this
 * release on the wall, safely, and be able to back out", as pure functions from Central's reads
 * and the operator's choices to the step shown, the one Put-on-the-wall confirmation, the Keep
 * plan and each Player's row. Central ingests and downloads every release by itself, so there
 * is no "get it" step: a release row already says whether it may be put on and its readiness.
 * Central keeps a version per Player (Q2 A, 2026-10-16): Put on the wall gives every Player the
 * release as its version (one PUT, no reboot), then this page reboots them one at a time; Undo
 * puts the version they all booted before back.
 *
 * NO CENTRAL STATE, NO VERB. The journey is a client of the verbs' homes: every write goes
 * through its verb's one send function (`sendWallVersion`, `sendReboot`), each judging its own
 * rule on its hook's newest read. This module and the page name no route (a source-scan test
 * holds it). Its one rule of its own is `nextReboot`: one reboot in flight, the previous Player
 * Rejoined or Skipped.
 *
 * PROGRESS IS DERIVED. The URL holds only the operator's choices (target release, skipped
 * Players); the step is re-derived from the reads on every render, so a reload or a second tab
 * shows the same step. Page memory holds only what Central cannot serve: whether rolling is
 * running and the FROZEN ROLLOUT: the Players a confirmation named, in order (`freezeRollout`).
 * Rolling reboots only those; a Player that appears later is Not in this rollout and never
 * rebooted, and no rolling starts without a confirmation that named its Players (a reload forgets
 * the frozen list, so Resume names them again).
 *
 * WHAT THE READS CANNOT SAY (stated, not papered over). Central does not serve which release a
 * boot was offered (§29 G7, an owner choice not taken). So a Player is on the release when this
 * page rebooted it after Put on the wall and its host line is on a later start (two line starts,
 * never clocks), or, for a Player this page did not reboot, when the base its host reports running
 * (the release read's `running`) is the target's base and that base identifies the target
 * (`baseIdentifiesTarget`). Any other Player is rebooted to make sure: a release whose base another
 * release a Player may run carries too cannot be recognised by its base. Every wait is the page's
 * own monotonic time.
 *
 * @typedef {"reading"|"no_release"|"rejected"|"choose"|"keeping"|"paused"|"done"} StepName
 * @typedef {{release: import("./generated/contract").ReleaseView, deployable: boolean,
 *            app: string|null, baseIdentifies: boolean}} Target
 *   `deployable`: false when Central refused the release's upload (`release.problem` says why);
 *   `baseIdentifies`: `baseIdentifiesTarget` on the read the target came from
 * @typedef {{step: StepName, target?: Target}} Step
 * @typedef {{playerId: string, deviceId: string, name: string,
 *            frames: Array<{frameId: string, outputId: string}>}} PlanEntry
 * @typedef {{request: import("./fleetCommands.js").FrozenRebootRequest, atMs: number,
 *            epoch: number|null}} SentReboot
 *   a reboot this page sent in this rollout, the page's monotonic time it was sent at, and the
 *   Player's authority epoch in the snapshot it was sent on (`playerEpoch`)
 * @typedef {"reading"|"waiting"|"rebooting"|"rejoining"|"rejoined"|"not_rejoined"
 *           |"cannot_reboot"|"skipped"|"not_in_rollout"} RowName
 * @typedef {{entry: PlanEntry, member: "planned"|"skipped"|"outside"}} RolloutMember
 *   `planned`: the rollout reboots it; `skipped`: the operator skipped it; `outside`: no
 *   confirmation named it (it appeared after the rollout was frozen)
 * @typedef {{playerId: string, state: RowName, label: string, pause: string|null,
 *            evidence?: import("./facts.js").Fact}} RowState
 *   `evidence`: the page's inference behind an on-the-release or Not rejoined row, as a
 *   `derived` fact naming its basis (rule 2: an inference never reads as device truth)
 *   `pause`: why rolling must stop on this row (an undelivered or unknown reboot, a stall)
 */

/** Rebooting or Rejoining for this long (the page's own monotonic time) reads Not rejoined. */
export const KEEP_WAIT_MS = 10 * 60 * 1000;

// A bound Output is live when its Frame's health is past every liveness and Output cause;
// calibration is kept across a reboot, so a Frame that needed it before still does. Liveness
// is not readiness: a current readiness failure is read separately (`readinessFailures`).
const LIVE = new Set(["ok", "display-changed", "needs-calibration"]);
// Rows that hold no reboot and need none: the rollout moves past them.
const SETTLED = new Set(["rejoined", "skipped", "not_in_rollout"]);

/**
 * Whether the target's base identifies the target (§25a Keep table): it has a base, and no other
 * release a Player may run carries the same base. Only then does the base a Player's host reports
 * running say which release it booted. Central lists pre-releases too, so only rows a Player may
 * run count: a deployable stable release, and whatever a Player boots next or booted before. Pure,
 * on the release read, so a reload derives the same.
 *
 * @param {import("./generated/contract").ReleasesView|null} read the release read
 * @param {{release: {version: string, base_tag: string|null}}} target
 * @returns {boolean}
 */
export function baseIdentifiesTarget(read, target) {
  const base = target.release.base_tag;
  if (base == null || read == null) return false;
  const chosen = new Set(read.players.flatMap((player) => [bootsNext(player, read), player.previous])
    .filter((version) => version != null));
  const rows = read.releases.filter((row) => deployable(row) && (row.stable || chosen.has(row.version)));
  return !rows.some((row) => row.base_tag === base && row.version !== target.release.version);
}

/**
 * The base a Player's host reports running, from the release read (`running`), or null.
 *
 * @param {import("./generated/contract").ReleasesView|null} read
 * @param {string} playerId
 * @returns {string|null}
 */
export function runningBase(read, playerId) {
  const running = (read?.players ?? []).find((player) => player.player_id === playerId)?.running;
  return typeof running === "string" ? running : null;
}

/**
 * The release the journey puts on the wall, from the release read; null when Central lists no
 * such version.
 *
 * @param {import("./generated/contract").ReleasesView|null} read the release read
 * @param {string} version
 * @returns {Target|null}
 */
export function journeyTarget(read, version) {
  const release = (read?.releases ?? []).find((row) => row.version === version);
  if (release === undefined) return null;
  return Object.freeze({ release, deployable: deployable(release), app: release.app_environment_sha256 ?? null,
    baseIdentifies: deployable(release) && baseIdentifiesTarget(read, { release }) });
}

/** What a version change alone (Undo) says: no reboot. */
export const SELECT_ALONE = "No Player is rebooted; each gets it at its next boot.";
/** The reboot plan's cost, said before anything is sent. */
export const PUT_REBOOTS = "Then this page reboots these Players one at a time, each after the previous one "
  + "rejoins. Each Frame a Player drives is blank while it reboots. Keep this tab open: hiding or closing it "
  + "pauses the rollout.";

/**
 * Whether the first reboot must wait for Central's download (§6.5): Central serves the target
 * as not Ready. Unserved readiness does not wait (Central's own
 * read-through still serves a boot), and the confirmation says it was not served.
 *
 * @param {Target} target
 * @returns {string|null} the waiting words, or null
 */
export function downloadWait(target) {
  const { readiness, version: tag, missing_bytes: missing, readiness_reason: reason } = target.release;
  if (readiness == null || readiness === "ready") return null;
  if (readiness === "failed") return `Waiting: Central could not download ${tag} (${words(reason)})`;
  return readiness === "downloading" ? `Waiting for Central to download ${tag}: ${downloadSize(missing)} left`
    : `Waiting for Central to download ${tag}`;
}

/**
 * The ONE confirmation of "Put vX on the wall" (§6.5, R7): what the release carries, that every
 * Player boots it from its next boot on (WALL_SCOPE), the Players the page will reboot (the page
 * renders the frozen plan between `lines` and `after`), the reboot cost, and the readiness line.
 *
 * @param {Target} target
 * @returns {{title: string, lines: string[], after: string[], confirmLabel: string}}
 */
export function putConfirmation(target) {
  const { version, base_tag: base } = target.release;
  const wait = downloadWait(target);
  return {
    title: `Put release ${version} on the wall?`,
    lines: [`Base ${base} · ${target.app === null ? "no app" : `app ${appHandle(target.app)}`}`, WALL_SCOPE,
      "These are the Players this console knows; this page reboots them in this order:"],
    after: [PUT_REBOOTS, readinessLine(target.release),
      ...(wait === null ? [] : ["The first reboot waits until Central has downloaded it."])],
    confirmLabel: "Put it on the wall and reboot",
  };
}

/**
 * The journey's step (§25a step table), from the newest reads and the operator's choice.
 *
 * @param {{releases: object|null, rows: RowState[]|null}} reads
 *   `rows`: the Keep rows while the target is selected (null until the snapshot the plan comes
 *   from is read; an empty array is a read plan of none)
 * @param {{tag: string}} choices `tag`: the release's version
 * @param {{rolling: boolean}} held
 * @returns {Step}
 */
export function journeyStep(reads, choices, held) {
  const read = reads.releases;
  if (read == null) return { step: "reading" };
  const target = journeyTarget(read, choices.tag);
  if (target === null) return { step: "no_release" };
  if (!target.deployable) return { step: "rejected", target };
  if (!wallOn(read, target.release.version)) return { step: "choose", target };
  // No rows yet (the snapshot is unread): no completion is claimed from no reads.
  const rows = reads.rows;
  if (rows == null) return { step: "reading", target };
  if (rows.every((row) => SETTLED.has(row.state))) return { step: "done", target };
  return { step: held.rolling ? "keeping" : "paused", target };
}

/**
 * The Players a Put on the wall reboots, in order (§25a): the Players this console knows (not retired,
 * enrolled with a device), by name.
 *
 * @param {object|null} snapshot
 * @param {{devices?: Map<string, object>}|null} bootFacts
 * @returns {PlanEntry[]}
 */
export function keepPlan(snapshot, bootFacts) {
  return playersByDevice(snapshot, bootFacts)
    .filter((row) => row.player !== null && row.standing !== "retired")
    .map((row) => ({ playerId: row.player.id, deviceId: row.deviceId, name: row.name, frames: row.frames }))
    .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

/**
 * The rollout a confirmation names (§25a): the plan's Players the operator did not skip, in
 * plan order. Frozen when the operator confirms; rolling reboots only these.
 *
 * @param {PlanEntry[]} plan
 * @param {Set<string>} skipped
 * @returns {ReadonlyArray<PlanEntry>}
 */
export function freezeRollout(plan, skipped) {
  return Object.freeze(plan.filter((entry) => !skipped.has(entry.playerId)).map((entry) => Object.freeze({ ...entry })));
}

/**
 * Every Player the Keep rows show, in reboot order (§25a). Before any rollout is frozen, the
 * live plan (planned or skipped). Once frozen, the frozen Players first, in their frozen order
 * (skipped if the operator has skipped one since), then every other Player of the live plan:
 * skipped, or outside the rollout, so a Player that enrolled after the confirmation is never
 * rebooted.
 *
 * @param {PlanEntry[]} plan the live plan
 * @param {ReadonlyArray<PlanEntry>|null} frozen
 * @param {Set<string>} skipped
 * @returns {RolloutMember[]}
 */
export function rolloutMembers(plan, frozen, skipped) {
  const member = (entry, inside) => ({ entry, member: skipped.has(entry.playerId) ? "skipped" : inside ? "planned" : "outside" });
  if (frozen === null) return plan.map((entry) => member(entry, true));
  const ids = new Set(frozen.map((entry) => entry.playerId));
  return [...frozen.map((entry) => member(entry, true)),
    ...plan.filter((entry) => !ids.has(entry.playerId)).map((entry) => member(entry, false))];
}

/**
 * The Player's authority epoch in the snapshot, or null. Central's own counter: the Player app
 * enrolls with a new one on every boot, so a later epoch than the one a reboot was sent on
 * proves the snapshot was read after the new boot's enrollment (counters, never clocks).
 */
export function playerEpoch(snapshot, playerId) {
  const epoch = (snapshot?.inventory?.players ?? []).find((player) => player.id === playerId)?.authority_epoch;
  return Number.isInteger(epoch) ? epoch : null;
}

/** The Frames this Player's bound Outputs show, from the snapshot. */
function boundFrames(snapshot, playerId) {
  return outputStates(snapshot, playerId).filter((output) => output.state === "bound").map((output) => output.frameId);
}

/** Whether every bound Output's Frame is live (a recent accepted report, nothing interrupted). */
function outputsLive(snapshot, playerId) {
  return boundFrames(snapshot, playerId).every((frameId) => LIVE.has(frameHealth(snapshot, frameId)?.state));
}

/**
 * The recovery words of every current readiness failure Central serves for this Player's bound
 * Frames (`readinessDiagnostics`, current assignments only), deduplicated; none: empty.
 */
function readinessFailures(snapshot, playerId) {
  return [...new Set(boundFrames(snapshot, playerId).flatMap((frameId) => readinessRecoveryForFrame(snapshot, frameId)))];
}

const row = (playerId, state, label, extra = {}) => Object.freeze({ playerId, state, label, pause: null, ...extra });

/**
 * One Player's Keep row (§25a Keep table), from its newest node read, the snapshot, the target,
 * the base its host reports running and the reboot this page sent it in this rollout.
 *
 * @param {{node: import("./nodeRead.js").NodeDevice, snapshot: object|null, playerId: string,
 *          target: Target, running?: string|null, sent: SentReboot|null, waitedMs: number,
 *          skipped: boolean, outside?: boolean}} input
 *   `running`: `runningBase` of the release read
 *   `outside`: the Player is not in the frozen rollout (`rolloutMembers`), so no reboot is
 *   ever judged for it
 *   `waitedMs`: the page's monotonic time since `sent`, or, without one, since rolling began
 *   (0 while not rolling), so a Player recognised on the release that never reports ready
 *   stalls too
 * @returns {RowState}
 */
export function keepRow({ node, snapshot, playerId, target, running = null, sent, waitedMs, skipped,
  outside = false }) {
  if (skipped) return row(playerId, "skipped", "Skipped");
  if (outside) {
    return row(playerId, "not_in_rollout", "Not in this rollout: no confirmation named it; this page never reboots it");
  }
  if (node?.read == null) {
    if (node?.error == null) return row(playerId, "reading", "Reading its node records…");
    return row(playerId, "cannot_reboot", `Cannot reboot: ${rebootTarget(node).reason}`);
  }
  const booted = sent != null && (() => {
    const start = hostEpoch(node.read);
    return start !== null && sent.request.hostEpoch !== null && start !== sent.request.hostEpoch;
  })();
  const recognised = sent == null && target.baseIdentifies && running === target.release.base_tag;
  const stalled = waitedMs >= KEEP_WAIT_MS;
  if (booted || recognised) {
    const basis = booted ? "its host line is on a later start than the reboot this page sent"
      : "this page did not reboot it; the base its host reports running identifies this release";
    const evidence = (value, more) => ({ evidence: fact({ kind: "derived", value, basis: `${basis}${more}` }) });
    // After this page's reboot, the snapshot judges the new boot only once it lists the Player
    // app's new enrollment: an older snapshot's liveness and readiness describe the old boot.
    const epoch = playerEpoch(snapshot, playerId);
    const fresh = sent == null || (epoch !== null && sent.epoch !== null && epoch > sent.epoch);
    // A current readiness failure is the new boot failing to show its assignment: never
    // Rejoined, and rolling stops here with its recovery words.
    const failures = fresh ? readinessFailures(snapshot, playerId) : [];
    if (failures.length > 0) {
      const words = failures.map((text) => text.replace(/\.$/, "")).join("; ");
      return row(playerId, "not_rejoined", `Not rejoined: it reports a readiness failure. ${failures.join(" ")}`, {
        pause: `it reports a readiness failure on a bound Output: ${words}`,
        ...evidence("Not rejoined", "; Central serves a current readiness failure for a Frame it drives") });
    }
    if (fresh && outputsLive(snapshot, playerId)) {
      return row(playerId, "rejoined", "Rejoined", evidence("On this release and rejoined",
        "; every bound Output's Frame has a recent accepted report, no interruption and no current readiness "
          + "failure (Central serves no per-Output playback commitment, so this is not proof it is showing its "
          + "assignment)"));
    }
    if (stalled) {
      return row(playerId, "not_rejoined", "Not rejoined after 10 minutes", {
        pause: "This Player has not rejoined after 10 minutes",
        ...evidence("Not rejoined", "; 10 minutes of this page's own time without rejoining") });
    }
    return row(playerId, "rejoining", !fresh
      ? "Rejoining: Central's snapshot does not list the Player app's enrollment on the new boot yet"
      : "Rejoining: a bound Output's Frame is not live yet", evidence("On this release", ""));
  }
  if (sent != null) {
    const record = (node.read.reboot_commands ?? []).find((entry) => entry.command_id === sent.request.body.command_id);
    const state = record === undefined ? null : rebootCommandState(record, node.readAt);
    if (state?.state === "not_delivered" || state?.state === "outcome_unknown") {
      return row(playerId, "rebooting", state.label, { pause: state.label });
    }
    if (stalled) {
      return row(playerId, "not_rejoined", "Not rejoined after 10 minutes", {
        pause: "This Player has not come back on a new boot after 10 minutes",
        evidence: fact({ kind: "derived", value: "Not rejoined",
          basis: "no later start of its host line after 10 minutes of this page's own time since its reboot" }) });
    }
    return row(playerId, "rebooting", state?.label ?? "Reboot sent by this page; Central has not listed it yet");
  }
  const reboot = rebootTarget(node);
  if (!reboot.available) return row(playerId, "cannot_reboot", `Cannot reboot: ${reboot.reason}`);
  const offer = rebootOffer(reboot, node.read.reboot_commands, node.readAt, null);
  if (offer.offer !== "new") return row(playerId, "rebooting", `Rebooting: ${offer.reason}`);
  return row(playerId, "waiting", target.baseIdentifies ? "Waiting"
    : "Waiting · unknown whether it booted this release (another release a Player may run has the same base, so "
      + "it cannot be recognised by its base); this page reboots it to make sure");
}

/**
 * The next Player to reboot, or null: the first row not Rejoined, Skipped or Not in this
 * rollout, only when it is Waiting. A row Rebooting, Rejoining or paused ahead of it holds the rollout, so at most one
 * reboot this page sends is ever in flight.
 *
 * @param {RowState[]} rows in plan order
 * @returns {string|null}
 */
export function nextReboot(rows) {
  for (const entry of rows) {
    if (SETTLED.has(entry.state)) continue;
    return entry.state === "waiting" ? entry.playerId : null;
  }
  return null;
}

/**
 * Why rolling must pause on the newest rows, or null: the first unsettled row's own pause, or
 * its reason when it cannot be rebooted.
 *
 * @param {RowState[]} rows
 * @returns {{playerId: string, reason: string}|null}
 */
export function keepPause(rows) {
  const first = rows.find((entry) => !SETTLED.has(entry.state));
  if (first === undefined) return null;
  if (first.pause !== null) return { playerId: first.playerId, reason: first.pause };
  if (first.state === "cannot_reboot") return { playerId: first.playerId, reason: first.label };
  return null;
}

/**
 * "n of m Players on this release" (§25a Paused): Rejoined rows of every row in the rollout
 * (a Player Not in this rollout is not counted). Each Rejoined row is the page's inference,
 * shown with its Evidence.
 */
export function keepCount(rows) {
  const counted = rows.filter((entry) => entry.state !== "not_in_rollout");
  return `${counted.filter((entry) => entry.state === "rejoined").length} of ${counted.length} Players on this release`;
}

/** Whether a row is settled (Rejoined, Skipped or Not in this rollout): it holds no reboot. */
export function rowSettled(row) {
  return SETTLED.has(row.state);
}
