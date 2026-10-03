import { useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { factText, LAYER_NAMES } from "./facts.js";
import { auditRef, frozen } from "./frozenRequest.js";
import { outputStates } from "./health.js";
import { effectGateFact, effectGateReason, GATE_UNREADABLE } from "./nodeControl.js";
import { nodeUnknown } from "./nodeRead.js";
import { deploymentHandle, releaseHome, releaseResult } from "./releases.js";

/**
 * Stage app (console DDD Part E §25, §27-§28, bead NS1): switch this Player's app, on this
 * boot only, to a deployment that carries an app, as pure functions from the node read, the
 * shell's effect gate and the release read to the request the console sends.
 *
 * WHO DECIDES (R14 as amended). Central judges every stage at send, under its own gate-first
 * locks, and a refusal writes nothing. The console disables Stage only on SERVED facts
 * (`stageBlocker`: retired, the gate effectively closed, no single current App Effect Broker
 * session, the latest operation `switching`), never on a copy of Central's admission rules;
 * a base mismatch or a missing qualified fallback is learnt from Central's answer, in its words.
 *
 * WHY "switching" BLOCKS. `stage()` freezes the old process from the app link at send; the
 * broker only accepts a stage whose frozen process is the one running. A switch replaces the
 * running process, so a stage sent during it strands on every later poll (§27).
 *
 * ONE SEND RULE (R13). `sendStage` judges the frozen request on `node.latest()` and
 * `control.latest()` — the hooks' newest reads at the moment of sending — and on this page's
 * held stage, refuses without a request when the rule fails, and is the ONLY console caller of
 * the app-stages route (a source-scan test holds it). Its ids and audit reference are fixed
 * when the dialog opens, so a resend after a lost answer is byte-identical (Central's request
 * hash covers every field).
 *
 * @typedef {{reason: string, gate?: true}} StageBlocker
 *   `gate`: the reason is the effect gate's, so the page links it to Releases › Effect gate
 * @typedef {{operationId: string, commandId: string}} StageIds minted when the dialog opens
 * @typedef {{body: {operation_id: string, command_id: string, session_id: string,
 *            device_generation: number, deployment_id: string, rollout_generation: number,
 *            operator_audit_ref: string}, replaces: string|null}} FrozenStage
 * @typedef {"in_flight"|"recorded"|"unknown"} HeldStageState
 *   sent and unanswered; recorded by Central, not listed yet; or its answer lost
 * @typedef {{request: FrozenStage, state: HeldStageState}} HeldStage
 * @typedef {{get: () => HeldStage|null, set: (request: FrozenStage, state: HeldStageState|null) => void}} HeldStages
 * @typedef {import("./releases.js").Outcome} Outcome
 */

/** The send rule's refusal while a switch runs (§27). */
export const SWITCH_IN_PROGRESS = "A switch is in progress; wait for it to finish";
/** What the dialog says when its frozen fences no longer match the newest reads. */
export const STAGE_STALE = "This request is out of date; close and reopen";
/** Always in the dialog (§25): a stage is desired state for its own boot only. */
export const stageScope = (selection) => "Applies to this boot only. Any later boot, including an unplanned one, "
  + `is offered the boot selection (${selection?.deployment_id == null
    ? "none: Central refuses every boot"
    : `deployment ${deploymentHandle(selection.deployment_id)}`}).`;
/** The bound rule (§25, D16), in the dialog when the Player drives Frames. Module-private: shown only through
 * `boundRuleLines`, so no page can state the rule without its caveat. */
const BOUND_RULE = "Each Frame this Player drives shows the base page while the app switches, then rejoins "
  + "its Run at the current point (missed content is not replayed), as on Reboot.";
/** Beside the bound rule wherever it is shown: G6's node half (the broker's exit evidence, Display
 * Host across the switch) has no CI leg yet. Remove it when the bound PID1 switch leg is green. */
const BOUND_PROVEN = "A switch on a Frame-bound Player is proven on Central only; the Player's side of it is not "
  + "yet qualified.";

/**
 * The bound rule's words (§25, D16), shared by every Stage surface as `selectionConfirmation` is by
 * every Select: the rule, then its qualification caveat. The only export of either text.
 *
 * @returns {string[]}
 */
export function boundRuleLines() {
  return [BOUND_RULE, BOUND_PROVEN];
}

const isGeneration = (value) => Number.isInteger(value) && value >= 1;
const tail = (id) => `${String(id).slice(0, 4)}…`;

/** The one current App Effect Broker session (scope `app_effect`) on the read, or why not. */
function brokerSession(read) {
  const current = (read?.sessions ?? []).filter((session) => session.current && session.scope === "app_effect");
  if (current.length === 0) return { reason: `${LAYER_NAMES.app_effect_broker} has no current session on this boot` };
  if (current.length > 1) return { reason: `Central holds more than one current ${LAYER_NAMES.app_effect_broker} session` };
  return { session: current[0] };
}

/**
 * Why Stage app is unavailable now, on served facts only, or null.
 *
 * @param {import("./nodeRead.js").NodeDevice|null} device the node read
 * @param {object|null} operations the app-attempts read
 * @param {import("./nodeControl.js").EffectGate|null} gate the shell's effect gate
 * @param {{retired_at?: number|null}|null} player the Registry Player (null: not judged here)
 * @returns {StageBlocker|null}
 */
export function stageBlocker(device, operations, gate, player) {
  if (player?.retired_at != null) return { reason: "this Player is retired" };
  const why = nodeUnknown(device);
  if (why !== null) return { reason: why };
  if (device.error != null) return { reason: `the last node read failed (${device.error.code}); press Refresh` };
  if (gate == null) return { reason: GATE_UNREADABLE, gate: true };
  if (gate.effective_state !== "open") return { reason: factText(effectGateFact(gate)), gate: true };
  if (!isGeneration(gate.generation)) return { reason: "the effect gate's generation is not served", gate: true };
  const broker = brokerSession(device.read);
  if (broker.reason) return { reason: broker.reason };
  if (!isGeneration(device.read.device_generation)) return { reason: "the device generation is not served" };
  if (!Array.isArray(operations?.operations)) return { reason: "the app operations read has not answered" };
  if (operations.operations[0]?.state === "switching") return { reason: SWITCH_IN_PROGRESS };
  return null;
}

/**
 * The deployments a stage may name: those carrying an app, as Releases shows them. No
 * verdict: whether one fits this Player's boot is Central's judgement at send.
 *
 * @param {object|null} read the release read
 * @returns {import("./releases.js").DeploymentRow[]}
 */
export function stageTargets(read) {
  if (read == null) return [];
  const withApp = new Set((read.deployments ?? []).filter((row) => row.app_environment_sha256 != null)
    .map((row) => row.deployment_id));
  return releaseHome(read).deployments.filter((row) => withApp.has(row.deploymentId));
}

/** The Frames this Player drives, from the snapshot (the bound rule applies when any). */
export function boundFrames(snapshot, playerId) {
  return playerId == null ? [] : outputStates(snapshot, playerId)
    .filter((output) => output.state === "bound").map((output) => output.frameId);
}

/**
 * What sending replaces, when the latest operation is Staged (§27): a newer stage, never a
 * retry.
 *
 * @param {object|null} operations the app-attempts read
 * @returns {string|null}
 */
export function stageReplaces(operations) {
  const latest = operations?.operations?.[0];
  if (latest?.state !== "staged") return null;
  const decision = latest.command_response?.decision;
  const answered = decision == null ? "has not responded to" : decision === "accepted" ? "accepted"
    : `answered "${decision}" to`;
  return `Sends a newer stage. It replaces stage ${tail(latest.operation_id)}, which `
    + `${LAYER_NAMES.app_effect_broker} ${answered}.`;
}

/**
 * The stage request, from the reads the dialog opened on: the fences Central serves (the one
 * current App Effect Broker session, the device generation, the gate's generation), the ids
 * minted at open and the chosen deployment. The audit reference is dated by Central's read
 * time. The same inputs build the same body, so a resend is byte-identical.
 *
 * @param {import("./nodeRead.js").NodeDevice} device the node read at dialog open
 * @param {import("./nodeControl.js").EffectGate|null} gate the shell's gate at dialog open
 * @param {string} deploymentId
 * @param {StageIds} ids
 * @returns {FrozenStage|{refused: string}}
 */
export function stageRequest(device, gate, deploymentId, ids) {
  const blocked = stageBlocker(device, device?.operations, gate, null);
  if (blocked !== null) return { refused: blocked.reason };
  if (typeof deploymentId !== "string" || deploymentId === "") return { refused: "choose a deployment" };
  if (typeof device.readAt !== "number") return { refused: "Central's read time is not served" };
  const { session } = brokerSession(device.read);
  return frozen({
    body: {
      operation_id: ids.operationId,
      command_id: ids.commandId,
      session_id: session.session_id,
      device_generation: device.read.device_generation,
      deployment_id: deploymentId,
      rollout_generation: gate.generation,
      operator_audit_ref: auditRef(device.readAt),
    },
    replaces: stageReplaces(device.operations),
  });
}

/**
 * The stage this page holds and Central has not listed yet, or null. Once the app-attempts read
 * lists its operation, the read is the authority and the hold ends.
 *
 * @param {HeldStage|null} held
 * @param {object|null} operations the app-attempts read
 * @returns {HeldStage|null}
 */
export function pendingStage(held, operations) {
  if (held == null) return null;
  const listed = (operations?.operations ?? []).some((entry) => entry.operation_id === held.request.body.operation_id);
  return listed ? null : held;
}

/**
 * Why a frozen stage may not be sent now, or null: a served blocker on the newest reads; a
 * session, device generation or gate generation that moved since the dialog opened; or this
 * page's own held stage (only that request, held with its answer lost, may be sent again).
 *
 * @param {FrozenStage} request
 * @param {import("./nodeRead.js").NodeDevice|null} device the newest node read
 * @param {import("./nodeControl.js").EffectGate|null} gate the shell's newest gate
 * @param {HeldStage|null} held
 * @returns {string|null}
 */
export function stageRefusal(request, device, gate, held) {
  const blocked = stageBlocker(device, device?.operations, gate, null);
  if (blocked !== null) return blocked.reason === SWITCH_IN_PROGRESS ? SWITCH_IN_PROGRESS : `${STAGE_STALE} (${blocked.reason})`;
  const body = request.body;
  if (brokerSession(device.read).session.session_id !== body.session_id
      || device.read.device_generation !== body.device_generation || gate.generation !== body.rollout_generation) {
    return STAGE_STALE;
  }
  if ((device.operations.operations ?? []).some((entry) => entry.operation_id === body.operation_id)) {
    return "Central already recorded this stage";
  }
  const mine = pendingStage(held, device.operations);
  if (mine !== null && (mine.request !== request || mine.state !== "unknown")) {
    return "This page already sent a stage for this Player; the next read settles it";
  }
  return null;
}

// Central's stage codes (§26); every other code takes the fail-closed default.
const SESSION_CHANGED = { outcome: "changed", message: `${LAYER_NAMES.app_effect_broker}'s session changed; close and reopen` };
const refused = (message) => ({ outcome: "refused", message });
/**
 * The stage refusals a caller branches on (Update the wall, §25a), by Central's served code: an
 * outcome carries the code (`releaseResult`), so a branch never keys on the words below.
 */
export const STAGE_REFUSAL = Object.freeze({
  baseMismatch: "node_app_target_base_mismatch",
  fallbackRequired: "node_app_qualified_fallback_required",
});
const STAGE_CODES = Object.freeze({
  node_app_current_process_unlinked: refused("The Player app has not linked its current process on this boot"),
  [STAGE_REFUSAL.baseMismatch]: refused("This deployment's base differs from the base this Player booted, or it has no app"),
  [STAGE_REFUSAL.fallbackRequired]: refused("No qualified fallback for this Player's current Outputs and base: qualify "
    + "the running app first"),
  node_app_existing_drain: refused("An equipment drain is active on this Player"),
  node_app_operator_target_changed: SESSION_CHANGED,
  node_session_unavailable: SESSION_CHANGED,
  node_session_superseded: SESSION_CHANGED,
  node_deployment_unknown: refused("Central has no such deployment"),
  node_environment_unknown: refused("Central does not know this app environment"),
  node_app_operation_identity_conflict: refused("This request id was already used for another stage"),
});

/**
 * Central's answer to a stage as an outcome (§26). The gate's own refusal quotes the shell's
 * gate reason; every other `rollout_*` code (Central re-checks its serving evidence while the
 * gate row reads open) is "Central refused the effect", so Releases and this answer never state
 * opposite gate facts. Any other code not listed is refused.
 *
 * @param {{ok: boolean, status: number, error: string|null, data: any}|null} result
 * @param {import("./nodeControl.js").EffectGate|null} gate the shell's gate, for its reason words
 * @returns {Outcome}
 */
export function stageResult(result, gate) {
  const code = result?.error;
  const codes = { ...STAGE_CODES };
  if (code === "rollout_gate_closed") {
    const reason = gate != null && gate.effective_state !== "open" ? effectGateReason(gate) : "its reason is not readable here";
    codes[code] = refused(`Effect gate closed: ${reason} (see Releases › Effect gate)`);
  } else if (typeof code === "string" && code.startsWith("rollout_")) {
    codes[code] = refused(`Central refused the effect: ${code}`);
  }
  return releaseResult(result, () => `Stage recorded; ${LAYER_NAMES.app_effect_broker} has not responded yet.`,
    Object.freeze(codes));
}

/**
 * THE one send path for a stage: judge the frozen request on `node.latest()`,
 * `control.latest()` and this page's held stage at the moment of sending; mark it held in the
 * same step, then POST. The hold follows the answer: recorded until a read lists it, unknown
 * when the answer was lost (only then may this exact request be sent again), released on a
 * refusal or change.
 *
 * @param {string} deviceId
 * @param {FrozenStage} request
 * @param {{node: {latest: () => import("./nodeRead.js").NodeDevice},
 *          control: {latest: () => import("./nodeControl.js").NodeControlRead}}} hooks
 * @param {HeldStages} held
 * @returns {Promise<Outcome>}
 */
export async function sendStage(deviceId, request, { node, control }, held) {
  const gate = control.latest().gate;
  const refusal = stageRefusal(request, node.latest(), gate, held.get());
  if (refusal !== null) return { outcome: "changed", message: `${refusal}.` };
  held.set(request, "in_flight");
  let result;
  try {
    result = await apiWrite(`/v1/operator/node/devices/${encodeURIComponent(deviceId)}/app-stages`,
      { method: "POST", body: request.body });
  } catch {
    result = null;
  }
  const outcome = stageResult(result, gate);
  held.set(request, outcome.outcome === "done" || outcome.outcome === "already" ? "recorded"
    : outcome.outcome === "unknown" ? "unknown" : null);
  return outcome;
}

/**
 * The stage one page holds (`HeldStages`): a ref, so a send marks its request held in the same
 * step as its check, and a render after each change. The Player page and Update the wall each
 * hold their own.
 *
 * @returns {HeldStages}
 */
export function useHeldStage() {
  const heldRef = useRef(/** @type {HeldStage|null} */ (null));
  const [, setView] = useState(/** @type {HeldStage|null} */ (null));
  return useMemo(() => ({
    get: () => heldRef.current,
    set: (request, state) => {
      heldRef.current = state === null ? null : { request, state };
      setView(heldRef.current);
    },
  }), []);
}
