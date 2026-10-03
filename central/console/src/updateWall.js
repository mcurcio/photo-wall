import { fact } from "./facts.js";
import { rebootCommandState, rebootOffer, rebootTarget } from "./fleetCommands.js";
import { frameHealth, outputStates } from "./health.js";
import { playersByDevice } from "./players.js";
import { readinessRecoveryForFrame } from "./readinessRecovery.js";
import { appHandle, deploymentIdFor, publishOffer } from "./releases.js";
import { STAGE_REFUSAL } from "./stage.js";

/**
 * Update the wall (console DDD Part E §25a, bead NU1): the guided journey "put this release on
 * the wall, safely, and be able to back out", as pure functions from Central's reads and the
 * operator's choices to the step shown, the Keep plan and each Player's row.
 *
 * NO CENTRAL STATE, NO VERB. The journey is a client of the verbs' homes: every write goes
 * through its verb's one send function (`sendPublish`, `sendBegin` with the sampler,
 * `sendStage`, `sendSelection`, `sendReboot`), each judging its own rule on its hook's newest
 * read. This module and the page name no route (a source-scan test holds it). Its one rule of
 * its own is `nextReboot`: one reboot in flight, the previous Player Rejoined or Skipped.
 *
 * PROGRESS IS DERIVED. The URL holds only the operator's choices (target release, tried
 * Player, skipped Players); the step is re-derived from the reads on every render, so a reload
 * or a second tab shows the same step. Page memory holds only what Central cannot serve: this
 * page's sampling, its held stage, the Back out press, whether rolling is running, and the
 * FROZEN ROLLOUT: the Players a confirmation named, in order (`freezeRollout`). Rolling reboots
 * only those; a Player that appears later is Not in this rollout and never rebooted, and no
 * rolling starts without a confirmation that named its Players (a reload forgets the frozen
 * list, so Resume names them again).
 *
 * WHAT THE READS CANNOT SAY (stated, not papered over). Central serves neither which boot
 * selection a boot was offered (§29 G7, an owner choice not taken) nor which deployment an app
 * operation staged. So a stage is the target's when this page holds it, or when it reads
 * `target_running` with the Player app linking the target's app; and a Player is on the
 * selection when this page rebooted it after Select and its Host Management session is on a
 * later boot (two kernel boot ids, never clocks), or, for a Player this page did not reboot,
 * when its linked app is the target's, that app identifies the target (`appIdentifiesTarget`)
 * and no Stage ran on its current boot (a Stage applies to that boot only). Any other Player is
 * rebooted to make sure: a release with no app, or one whose app another release carries on a
 * different base (a base-only release), cannot be recognised by its app. Every wait is the
 * page's own monotonic time.
 *
 * @typedef {"reading"|"no_release"|"get_it"|"choose"|"qualifying"|"staging"|"looking"
 *           |"backing_out"|"keeping"|"paused"|"done"} StepName
 * @typedef {{release: import("./releases.js").Release, withApp: boolean, deploymentId: string,
 *            listed: boolean, app: string|null, appIdentifies: boolean}} Target
 *   `appIdentifies`: `appIdentifiesTarget` on the read the target came from
 * @typedef {{step: StepName, target?: Target, stage?: object|null,
 *            how?: "kept"|"backed_out"}} Step
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
 *            gate?: true, evidence?: import("./facts.js").Fact}} RowState
 *   `evidence`: the page's inference behind an on-the-selection or Not rejoined row, as a
 *   `derived` fact naming its basis (rule 2: an inference never reads as device truth)
 *   `pause`: why rolling must stop on this row (a rejected or unknown reboot, a stall)
 */

/** Rebooting or Rejoining for this long (the page's own monotonic time) reads Not rejoined. */
export const KEEP_WAIT_MS = 10 * 60 * 1000;

/** Try is withdrawn when the target changes the base (§25a); a hint only, Central judges at send. */
export const TRY_BASE_CHANGES = "This release changes the base, so it cannot be tried live; Keep reboots each "
  + "Player onto it";
/** A release with no app has nothing to stage (Central refuses a stage without an app). */
export const TRY_NO_APP = "This release has no app, so there is nothing to try live; Keep reboots each Player "
  + "onto it";

const NONE_HELD = Object.freeze({ get: () => null });
const LOOKING = new Set(["target_running", "fallback_running", "effect_unknown"]);
const IN_PROGRESS = new Set(["staged", "switching"]);
const ENDED = new Set(["interrupted_by_reboot", "ended_by_later_boot"]);
// A bound Output is live when its Frame's health is past every liveness and Output cause;
// calibration is kept across a reboot, so a Frame that needed it before still does. Liveness
// is not readiness: a current readiness failure is read separately (`readinessFailures`).
const LIVE = new Set(["ok", "needs-calibration"]);
// Rows that hold no reboot and need none: the rollout moves past them.
const SETTLED = new Set(["rejoined", "skipped", "not_in_rollout"]);

/**
 * Whether the target's app identifies the target (§25a Keep table): it has an app, and no listed
 * deployment and no catalog release pairs that app environment with a different base. Only then
 * does a Player's linked app say which release it booted; a base-only release (new base, same
 * app) cannot be recognised that way. Pure, on the release read, so a reload derives the same.
 *
 * @param {object|null} read the release read
 * @param {{app: string|null, release: {base_tag: string}}} target
 * @returns {boolean}
 */
export function appIdentifiesTarget(read, target) {
  if (target.app === null) return false;
  const rows = [...(read?.deployments ?? []), ...(read?.releases ?? [])];
  return !rows.some((row) => row.app_environment_sha256 === target.app && row.base_tag !== target.release.base_tag);
}

/**
 * The release the journey puts on the wall, and the deployment this console publishes it as
 * (with its app when it has one), from the release read; null when the catalog does not list
 * the tag. The catalog is newest first, so a re-tagged release resolves to its newest row.
 *
 * @param {object|null} read the release read
 * @param {string} tag
 * @returns {Target|null}
 */
export function journeyTarget(read, tag) {
  const release = (read?.releases ?? []).find((row) => row.tag === tag);
  if (release === undefined) return null;
  const withApp = release.app_environment_sha256 != null;
  const app = release.app_environment_sha256 ?? null;
  return Object.freeze({
    release, withApp,
    deploymentId: deploymentIdFor(release.manifest_sha256, withApp),
    listed: publishOffer(read, release, withApp, NONE_HELD).offer === "published",
    app,
    appIdentifies: appIdentifiesTarget(read, { app, release }),
  });
}

/**
 * What the journey does after a stage send (§25a failures), keyed on the outcome and Central's
 * served code, never on the words: `stay` (a lost answer or a send-rule refusal: stays on
 * Staging with its words), `held` (recorded), `qualify` (a missing qualified fallback, the first
 * time), `withdraw` (a base mismatch: Try is withdrawn for this target, back to Choose) or
 * `choose` (any other refusal, back to Choose with Central's words).
 *
 * @param {import("./releases.js").Outcome} outcome
 * @param {number} refusals missing-fallback refusals already met on this journey
 * @returns {"stay"|"held"|"qualify"|"withdraw"|"choose"}
 */
export function stageFollowUp(outcome, refusals) {
  if (outcome.outcome === "unknown" || outcome.outcome === "changed") return "stay";
  if (outcome.outcome !== "refused") return "held";
  if (outcome.code === STAGE_REFUSAL.fallbackRequired && refusals === 0) return "qualify";
  return outcome.code === STAGE_REFUSAL.baseMismatch ? "withdraw" : "choose";
}

/**
 * Why Try is withdrawn for this target before anything is sent (a hint; Central judges a stage
 * at send), or null.
 *
 * @param {object} read the release read
 * @param {Target} target
 * @returns {string|null}
 */
export function tryWithdrawn(read, target) {
  if (!target.withApp) return TRY_NO_APP;
  const selected = (read.deployments ?? []).find((row) => row.deployment_id === read.selection?.deployment_id);
  return selected !== undefined && selected.base_tag !== target.release.base_tag ? TRY_BASE_CHANGES : null;
}

/** The app the current boot linked (G4), or null. */
export function linkedApp(operations) {
  const sha = operations?.qualification?.linked_app?.environment_sha256;
  return typeof sha === "string" && sha !== "" ? sha : null;
}

/** Whether Central lists an acceptance of the linked app (G4); no verdict on whether it fits. */
function acceptedLinked(operations) {
  const linked = linkedApp(operations);
  const acceptances = operations?.qualification?.acceptances;
  return linked !== null && Array.isArray(acceptances) && acceptances.some((row) => row.environment_sha256 === linked);
}

/**
 * The tried Player's latest stage when it is provably the target's, or null: the stage this
 * page holds, or one reading `target_running` while the Player app links the target's app.
 */
function targetStage(operations, target, stageId) {
  const latest = operations?.operations?.[0];
  if (latest == null) return null;
  if (stageId != null && latest.operation_id === stageId) return latest;
  return latest.state === "target_running" && target.app !== null && linkedApp(operations) === target.app
    ? latest : null;
}

/**
 * The journey's step (§25a step table), from the newest reads, the operator's choices and the
 * little this page holds.
 *
 * @param {{releases: object|null, tried: {operations: object|null}|null, rows: RowState[]|null}} reads
 *   `tried`: the tried Player's node read; `rows`: the Keep rows while the target is selected
 *   (null until the snapshot the plan comes from is read; an empty array is a read plan of none)
 * @param {{tag: string, tried: string|null}} choices
 * @param {{sampling: boolean, qualified: boolean, needQualify: boolean, backingOut: boolean,
 *          stageId: string|null, rolling: boolean}} held
 * @returns {Step}
 */
export function journeyStep(reads, choices, held) {
  const read = reads.releases;
  if (read == null) return { step: "reading" };
  const target = journeyTarget(read, choices.tag);
  if (target === null) return { step: "no_release" };
  if (!target.listed) return { step: "get_it", target };
  if (read.selection?.deployment_id === target.deploymentId) {
    // No rows yet (the snapshot is unread): no completion is claimed from no reads.
    const rows = reads.rows;
    if (rows == null) return { step: "reading", target };
    if (rows.every((row) => SETTLED.has(row.state))) {
      return { step: "done", target, how: "kept" };
    }
    return { step: held.rolling ? "keeping" : "paused", target };
  }
  if (choices.tried == null) return { step: "choose", target };
  const operations = reads.tried?.operations ?? null;
  const listed = operations?.operations ?? [];
  if (held.backingOut) {
    const backed = listed.find((entry) => entry.operation_id === held.stageId) ?? listed[0];
    return ENDED.has(backed?.state) ? { step: "done", target, how: "backed_out" } : { step: "backing_out", target };
  }
  const stage = targetStage(operations, target, held.stageId);
  if (stage !== null && LOOKING.has(stage.state)) return { step: "looking", target, stage };
  if (stage !== null && IN_PROGRESS.has(stage.state)) return { step: "staging", target, stage };
  if (held.sampling) return { step: "qualifying", target };
  if (!held.qualified && (held.needQualify || (linkedApp(operations) !== null && !acceptedLinked(operations)))) {
    return { step: "qualifying", target };
  }
  return { step: "staging", target, stage };
}

/**
 * The Players Keep reboots, in order (§25a): the Players this console knows (not retired,
 * enrolled with a device), the tried Player first (it is proven on this hardware), the
 * others by name.
 *
 * @param {object|null} snapshot
 * @param {{devices?: Map<string, object>}|null} bootFacts
 * @param {string|null} triedPlayerId
 * @returns {PlanEntry[]}
 */
export function keepPlan(snapshot, bootFacts, triedPlayerId) {
  return playersByDevice(snapshot, bootFacts)
    .filter((row) => row.player !== null && row.standing !== "retired")
    .map((row) => ({ playerId: row.player.id, deviceId: row.deviceId, name: row.name, frames: row.frames }))
    .sort((a, b) => (b.playerId === triedPlayerId) - (a.playerId === triedPlayerId)
      || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
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

/** The kernel boot id of the box's current Host Management session, or null. */
function hostBoot(read) {
  const hosts = (read?.sessions ?? []).filter((session) => session.current && session.producer?.owner === "host_core");
  return hosts.length === 1 && typeof hosts[0].producer.kernel_boot_id === "string"
    ? hosts[0].producer.kernel_boot_id : null;
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
 * One Player's Keep row (§25a Keep table), from its newest node read, the snapshot, the
 * target, the shell's gate and the reboot this page sent it in this rollout.
 *
 * @param {{node: import("./nodeRead.js").NodeDevice, snapshot: object|null, playerId: string,
 *          target: Target, gate: import("./nodeControl.js").EffectGate|null,
 *          sent: SentReboot|null, waitedMs: number, skipped: boolean, outside?: boolean}} input
 *   `outside`: the Player is not in the frozen rollout (`rolloutMembers`), so no reboot is
 *   ever judged for it
 *   `waitedMs`: the page's monotonic time since `sent`, or, without one, since rolling began
 *   (0 while not rolling), so a Player that runs the target's app but never reports ready
 *   stalls too
 * @returns {RowState}
 */
export function keepRow({ node, snapshot, playerId, target, gate, sent, waitedMs, skipped, outside = false }) {
  if (skipped) return row(playerId, "skipped", "Skipped");
  if (outside) {
    return row(playerId, "not_in_rollout", "Not in this rollout: no confirmation named it; this page never reboots it");
  }
  if (node?.read == null) {
    if (node?.error == null) return row(playerId, "reading", "Reading its node records…");
    return row(playerId, "cannot_reboot", `Cannot reboot: ${rebootTarget(node, gate).reason}`);
  }
  const linked = linkedApp(node.operations);
  const appLinked = target.app === null || linked === target.app;
  const booted = sent != null && (() => {
    const boot = hostBoot(node.read);
    return boot !== null && boot !== sent.request.kernelBootId;
  })();
  // A stage not yet ended by a later boot ran on the current boot: its app is that boot's only,
  // never evidence of the boot selection (the tried Player after Try reads exactly so).
  const latest = node.operations?.operations?.[0] ?? null;
  const stagedThisBoot = latest != null && !ENDED.has(latest.state);
  const onSelection = sent != null ? booted : target.appIdentifies && !stagedThisBoot && linked === target.app;
  const stalled = waitedMs >= KEEP_WAIT_MS;
  if (onSelection) {
    const basis = sent != null
      ? "its Host Management session is on a later kernel boot than the reboot this page sent"
      : "this page did not reboot it; its linked app identifies this release and no Stage ran on this boot";
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
    if (appLinked && fresh && outputsLive(snapshot, playerId)) {
      return row(playerId, "rejoined", "Rejoined", evidence("On the selection and rejoined",
        "; every bound Output's Frame has a recent accepted report, no interruption and no current readiness "
          + "failure (Central serves no per-Output playback commitment, so this is not proof it is showing its "
          + "assignment)"));
    }
    if (stalled) {
      return row(playerId, "not_rejoined", "Not rejoined after 10 minutes", {
        pause: "This Player has not rejoined after 10 minutes",
        ...evidence("Not rejoined", "; 10 minutes of this page's own time without rejoining") });
    }
    return row(playerId, "rejoining", !appLinked
      ? `Rejoining: the Player app has not linked app ${appHandle(target.app)} yet`
      : !fresh ? "Rejoining: Central's snapshot does not list the Player app's enrollment on the new boot yet"
      : "Rejoining: a bound Output's Frame is not live yet", evidence("On the selection", ""));
  }
  if (sent != null) {
    const record = (node.read.reboot_commands ?? []).find((entry) => entry.command_id === sent.request.body.command_id);
    const state = record === undefined ? null : rebootCommandState(record, node.readAt, { nodeDevice: node, gate });
    if (state?.state === "rejected" || state?.state === "outcome_unknown") {
      return row(playerId, "rebooting", state.label, { pause: state.label });
    }
    if (stalled) {
      return row(playerId, "not_rejoined", "Not rejoined after 10 minutes", {
        pause: "This Player has not come back on a new boot after 10 minutes",
        evidence: fact({ kind: "derived", value: "Not rejoined",
          basis: "no later kernel boot after 10 minutes of this page's own time since its reboot" }) });
    }
    return row(playerId, "rebooting", state?.label ?? "Reboot sent by this page; Central has not listed it yet");
  }
  const reboot = rebootTarget(node, gate);
  if (!reboot.available) {
    return row(playerId, "cannot_reboot", `Cannot reboot: ${reboot.reason}`, reboot.gate ? { gate: true } : {});
  }
  const offer = rebootOffer(reboot, node.read.reboot_commands, node.readAt, null);
  if (offer.offer !== "new") return row(playerId, "rebooting", `Rebooting: ${offer.reason}`);
  if (stagedThisBoot) {
    return row(playerId, "waiting", "Waiting · it runs a Stage, which applies to this boot only; this page reboots it; "
      + "its next boot is offered the selection");
  }
  if (target.app === null) {
    return row(playerId, "waiting", "Waiting · unknown whether it booted the selection (a release with no app cannot "
      + "be recognised); this page reboots it to make sure");
  }
  return row(playerId, "waiting", target.appIdentifies ? "Waiting"
    : "Waiting · unknown whether it booted the selection (another release carries the same app on a different "
      + "base, so it cannot be recognised by its app); this page reboots it to make sure");
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
 * @returns {{playerId: string, reason: string, gate?: true}|null}
 */
export function keepPause(rows) {
  const first = rows.find((entry) => !SETTLED.has(entry.state));
  if (first === undefined) return null;
  if (first.pause !== null) return { playerId: first.playerId, reason: first.pause };
  if (first.state === "cannot_reboot") return { playerId: first.playerId, reason: first.label, ...(first.gate ? { gate: true } : {}) };
  return null;
}

/**
 * "n of m Players on the selection" (§25a Paused): Rejoined rows of every row in the rollout
 * (a Player Not in this rollout is not counted). Each Rejoined row is the page's inference,
 * shown with its Evidence.
 */
export function keepCount(rows) {
  const counted = rows.filter((entry) => entry.state !== "not_in_rollout");
  return `${counted.filter((entry) => entry.state === "rejoined").length} of ${counted.length} Players on the selection`;
}

/** Whether a row is settled (Rejoined, Skipped or Not in this rollout): it holds no reboot. */
export function rowSettled(row) {
  return SETTLED.has(row.state);
}
