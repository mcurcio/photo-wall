import { apiWrite } from "./apiWrite.js";
import { CHANGED_MESSAGE, UNKNOWN_MESSAGE } from "./equipmentApi.js";
import { clock as localClock, fact, LAYER_NAMES, words } from "./facts.js";
import { outputStates } from "./health.js";
import { liveRunsFor } from "./join.js";
import { nodeUnknown } from "./nodeRead.js";

/**
 * The fleet's commands on one box (console DDD §10-§11, bead B2): Reboot Player and the
 * read-only app operations, as pure functions from the node read (nodeRead.js) to the
 * request the console sends and the named state it shows.
 *
 * REBOOT. `rebootTarget` binds Central's fences from the read shown — the ONE current
 * Host Management (`host_core`) session, the device generation and the effect gate's
 * generation — so the operator never picks transport plumbing. `rebootRequest` freezes
 * the WHOLE request body when the dialog opens (command id, session, generations, audit
 * reference, window); a retry re-sends that frozen body unchanged, so Central's
 * `node_reboot_identity_conflict` cannot arise from the console.
 *
 * ONE SEND RULE (§10, §16). A new command id may be sent only when no command on the target
 * Host Management session is OUTSTANDING — Central serves `outstanding` per command, from the
 * same predicate its `node_reboot_outstanding` fence uses. The request the page itself holds
 * (`heldReboot`) counts as outstanding until a read lists it or Central's read time reaches
 * its frozen window (`rebootOffer`). `sendReboot` is the ONLY console function that POSTs to
 * the reboots route (a source-scan test enforces it), and it judges the rule on `node.latest()`, the
 * hook's newest read at the moment of sending, never on a read the caller hands it.
 *
 * STATES. `rebootCommandState` and `appOperationState` compute each state only from
 * Central's records and Central's read time (R10): no browser or node clock is involved.
 * No state is terminal while a later response or event can still arrive — Central stores
 * a late response without an expiry check — so Outcome unknown moves on when one does. A
 * later boot is never linked to a request: a later boot does not show what caused it.
 *
 * @typedef {{available: true, sessionId: string, deviceGeneration: number,
 *            rolloutGeneration: number, kernelBootId: string}
 *         | {available: false, reason: string}} RebootTarget
 * @typedef {{command_id: string, session_id: string, device_generation: number,
 *            operator_audit_ref: string, rollout_generation: number,
 *            valid_for_seconds: number}} RebootBody
 * @typedef {{body: RebootBody, kernelBootId: string, windowSeconds: number,
 *            retryUntil: number, frames: Array<{frameId: string, runs: Array<{runId: string, sceneId: string, phase: string}>}>,
 *            previous: string|null}} FrozenRebootRequest
 * @typedef {{state: string, label: string, fact: import("./facts.js").Fact}} CommandState
 * @typedef {{outcome: "done"|"already"|"changed"|"refused"|"unknown", message: string,
 *            retryable: boolean}} RebootResult
 * @typedef {{offer: "new"} | {offer: "retry", reason: string}
 *         | {offer: "blocked", reason: string}} RebootOffer
 *   `retry`: only the held request may be re-sent (its frozen body); `reason` says why no
 *   new request may be built.
 */

/** Central offers a reboot request to Host Management for this long (§11 current choices). */
export const REBOOT_WINDOW_SECONDS = 30;

// The console's audit reference: `console/<Central's date>[/<reason>]`, a node token.
const TOKEN = /^[A-Za-z0-9][A-Za-z0-9_.:/-]*$/;
const MAX_AUDIT_REF = 256;

const isTime = (value) => typeof value === "number" && Number.isFinite(value);
const isGeneration = (value) => Number.isInteger(value) && value >= 1;

/** Deep-freeze a plain value (the frozen request cannot change after it is built). */
function frozen(value) {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    Object.values(value).forEach(frozen);
    Object.freeze(value);
  }
  return value;
}

/**
 * The fences a reboot binds, from the node read shown, or why Reboot is unavailable.
 *
 * @param {import("./nodeRead.js").NodeDevice|null} nodeDevice
 * @param {object|null} gate the `GET /v1/operator/node/status` answer
 * @returns {RebootTarget}
 */
export function rebootTarget(nodeDevice, gate) {
  const why = nodeUnknown(nodeDevice);
  if (why !== null) return { available: false, reason: why };
  if (nodeDevice.error !== null && nodeDevice.error !== undefined) {
    return { available: false,
      reason: `the last node read failed (${nodeDevice.error.code}); press Refresh` };
  }
  const effectGate = gate?.effect_gate;
  if (effectGate?.effective_state !== "open") {
    return { available: false, reason: effectGate?.reason
      ? `Central's effect gate is closed (${words(effectGate.reason)})`
      : "Central's effect gate is closed" };
  }
  const read = nodeDevice.read;
  const hosts = read.sessions.filter((session) => session.current && session.producer?.owner === "host_core");
  if (hosts.length === 0) return { available: false, reason: `no current ${LAYER_NAMES.host_core} session on this box` };
  if (hosts.length > 1) {
    return { available: false, reason: `Central holds more than one current ${LAYER_NAMES.host_core} session` };
  }
  const [host] = hosts;
  if (host.scope !== "operator_reboot" || host.command_eligible !== true) {
    return { available: false,
      reason: `Central refuses commands to this ${LAYER_NAMES.host_core} session (${words(host.command_reason)})` };
  }
  if (!isGeneration(read.device_generation)) return { available: false, reason: "the device generation is not served" };
  if (!isGeneration(effectGate.generation)) return { available: false, reason: "the effect gate's generation is not served" };
  if (typeof host.producer?.kernel_boot_id !== "string") {
    return { available: false, reason: "the session's boot is not served" };
  }
  return { available: true, sessionId: host.session_id, deviceGeneration: read.device_generation,
    rolloutGeneration: effectGate.generation, kernelBootId: host.producer.kernel_boot_id };
}

/**
 * The request this page keeps after sending it, or null. A request Central recorded
 * (done, already) or whose outcome is unknown and may be retried is held; one Central
 * refused, or whose window ended (410), is not.
 *
 * @param {FrozenRebootRequest} request
 * @param {RebootResult} result
 * @returns {FrozenRebootRequest|null}
 */
export function heldReboot(request, result) {
  return result.outcome === "done" || result.outcome === "already" || result.retryable ? request : null;
}

/** Central serves `outstanding` per command (§16); anything but `false` is treated as outstanding. */
const isOutstanding = (command) => command?.outstanding !== false;

/**
 * What the Reboot control offers now (§10): a new request, only a retry of the request
 * this page holds, or nothing (with why).
 *
 * Every command on the target session is judged by Central's served `outstanding` — not only
 * the newest, so an older outstanding entry still blocks, and Accepted or Initiated requests
 * block until they expire. A command to an earlier session never blocks. The held request
 * counts as outstanding until a read lists it (then Central's `outstanding` decides) or
 * Central's read time reaches its frozen `retryUntil`; while it counts, only it may be re-sent.
 *
 * Passing a frozen request as `held` asks whether THAT request may be sent now: "retry" means
 * yes. `sendReboot` and the dialog ask exactly this (`rebootRefusal`).
 *
 * @param {RebootTarget} target
 * @param {Array<object>|null} commands the read's `reboot_commands` (every one)
 * @param {number|null} readAt the device read's `read_at`
 * @param {FrozenRebootRequest|null} held the request this page sent (`heldReboot`)
 * @param {{clock?: (seconds: number) => string}} [options]
 * @returns {RebootOffer}
 */
export function rebootOffer(target, commands, readAt, held, { clock = localClock } = {}) {
  if (!target.available) return { offer: "blocked", reason: target.reason };
  const listed = commands ?? [];
  const mine = held != null && held.body.session_id === target.sessionId ? held : null;
  const other = listed.find((command) => command.command?.command_session_id === target.sessionId
    && command.command_id !== mine?.body.command_id && isOutstanding(command));
  if (other !== undefined) {
    return { offer: "blocked", reason: isTime(other.expires_at)
      ? `a reboot request is outstanding until ${clock(other.expires_at)}; only the page that sent it can retry it`
      : "a reboot request is outstanding; only the page that sent it can retry it" };
  }
  if (mine !== null) {
    const record = listed.find((command) => command.command_id === mine.body.command_id);
    if (record !== undefined ? isOutstanding(record) : isTime(readAt) && readAt < mine.retryUntil) {
      return { offer: "retry",
        reason: "the reboot request this page sent may still be offered; only that request can be retried" };
    }
  }
  return { offer: "new" };
}

/**
 * Freeze the whole reboot request when its dialog opens: the body sent (and re-sent
 * unchanged on retry), the Frames bound to this Player with their live Runs, and what is
 * known of the previous request. Refuses unless `rebootOffer` is "new".
 *
 * @param {RebootTarget} target
 * @param {Array<object>|null} commands the read's `reboot_commands`, newest first
 * @param {object|null} snapshot
 * @param {number|null} readAt the device read's `read_at` (Central's clock; dates the audit reference)
 * @param {{playerId: string|null, commandId: string, reason?: string,
 *          held?: FrozenRebootRequest|null, clock?: (seconds: number) => string}} options
 * @returns {FrozenRebootRequest|{refused: string}}
 */
export function rebootRequest(target, commands, snapshot, readAt,
  { playerId, commandId, reason = "", held = null, clock = localClock }) {
  const offer = rebootOffer(target, commands, readAt, held, { clock });
  if (offer.offer !== "new") return { refused: offer.reason };
  if (!isTime(readAt)) return { refused: "Central's read time is not served" };
  const note = reason.trim();
  const auditRef = `console/${new Date(readAt * 1000).toISOString().slice(0, 10)}${note ? `/${note}` : ""}`;
  if (note !== "" && (!TOKEN.test(note) || auditRef.length > MAX_AUDIT_REF)) {
    return { refused: "a reason is one word: letters, digits and _ . : / - only" };
  }
  const frames = playerId === null ? [] : outputStates(snapshot, playerId)
    .filter((output) => output.state === "bound")
    .map((output) => ({ frameId: output.frameId,
      runs: liveRunsFor(snapshot?.runtime, output.frameId)
        .map((run) => ({ runId: run.run_id, sceneId: run.scene_id, phase: run.phase })) }));
  let previous = null;
  const [latest = null] = commands ?? [];
  if (latest != null) {
    const prior = rebootCommandState(latest, readAt, { clock });
    if (prior.state === "outcome_unknown") {
      const targeted = latest.command?.producer?.kernel_boot_id ?? null;
      previous = targeted === null
        ? "The previous request's outcome is unknown. The boot it targeted is not served."
        : `The previous request's outcome is unknown. Host Management's current session is ${
          targeted === target.kernelBootId
            ? "the same boot that request targeted"
            : "a later boot than the one that request targeted"}.`;
    } else {
      previous = `The previous request: ${prior.label}.`;
    }
  }
  return frozen({
    body: {
      command_id: commandId,
      session_id: target.sessionId,
      device_generation: target.deviceGeneration,
      operator_audit_ref: auditRef,
      rollout_generation: target.rolloutGeneration,
      valid_for_seconds: REBOOT_WINDOW_SECONDS,
    },
    kernelBootId: target.kernelBootId,
    windowSeconds: REBOOT_WINDOW_SECONDS,
    // Central's clock: the read time it was frozen at plus its window. Central's own
    // expires_at counts from the later send (and is capped by the session's expiry), so
    // the two may differ; `rebootOffer` stops counting an unlisted request at this time.
    retryUntil: readAt + REBOOT_WINDOW_SECONDS,
    frames,
    previous,
  });
}

/** What the reboot dialog says when its frozen request may no longer be sent (§10). */
export const REBOOT_STALE = "This request is out of date; close and reopen";
/**
 * The one wording for "another reboot request on this session is outstanding" (§16): the
 * client-side refusal and Central's 409 `node_reboot_outstanding` are the same outcome.
 */
export const REBOOT_OUTSTANDING =
  "Another reboot request for this Player is outstanding; close this dialog and review it";

/**
 * Why a frozen request may not be sent now (first time or retry), or null: `rebootOffer`
 * with the request as `held`, on the given read. Another outstanding command on its session
 * refuses it; so does a request that a read has settled, or that no read lists once Central's
 * read time reaches its frozen window (reopening rebuilds it from a fresh read).
 *
 * @param {FrozenRebootRequest} request
 * @param {import("./nodeRead.js").NodeDevice|null} nodeDevice
 * @returns {string|null}
 */
export function rebootRefusal(request, nodeDevice) {
  const target = rebootTarget(nodeDevice, nodeDevice?.gate ?? null);
  const offer = rebootOffer(target, nodeDevice?.read?.reboot_commands, nodeDevice?.readAt ?? null, request);
  if (offer.offer === "retry") return null;
  if (!target.available) return `${REBOOT_STALE} (${target.reason})`;
  return offer.offer === "blocked" ? REBOOT_OUTSTANDING : REBOOT_STALE;
}

/**
 * Why Central is not offering a Requested reboot now, from the read shown, or null. Central's
 * poll offers a request only while the effect gate is open and the targeted Host Management
 * session authenticates (`node_commands.py` `poll`). The request's own gate generation is not
 * served, so a gate that closed and reopened is not detected.
 *
 * @param {object} command one `reboot_commands` record
 * @param {import("./nodeRead.js").NodeDevice|null} nodeDevice the read it came from
 * @returns {string|null}
 */
function notOffered(command, nodeDevice) {
  const effectGate = nodeDevice?.gate?.effect_gate;
  if (effectGate != null && effectGate.effective_state !== "open") return "effect gate closed";
  const sessions = nodeDevice?.read?.sessions;
  const targeted = command.command?.command_session_id;
  if (Array.isArray(sessions) && typeof targeted === "string"
      && !sessions.some((session) => session.current && session.session_id === targeted)) {
    return "session no longer current";
  }
  return null;
}

/** A response record's decision, wherever the stored message carries it. */
const decisionOf = (response) => response?.message?.message?.decision ?? null;

/**
 * One reboot request's named state (§10), from Central's records and read time only.
 *
 * @param {object} command one `reboot_commands` record of the device read
 * @param {number|null} readAt the device read's `read_at`
 * @param {{clock?: (seconds: number) => string,
 *          nodeDevice?: import("./nodeRead.js").NodeDevice|null}} [options] `clock` formats
 *   Central's times; `nodeDevice` (the read shown) lets a Requested label say when Central is
 *   not offering it now (gate closed, session no longer current). The state is the same.
 * @returns {CommandState}
 */
export function rebootCommandState(command, readAt, { clock = localClock, nodeDevice = null } = {}) {
  const initiated = (command.effects ?? []).find((effect) => effect.state === "reboot_initiated");
  if (initiated !== undefined) {
    return { state: "initiated", label: "Host Management reported the reboot started · completion unknown",
      fact: fact({ kind: "reported", source: LAYER_NAMES.host_core, receipt: "first", value: "a reboot-initiated event naming this request",
        receivedAt: initiated.received_at, readAt, field: "effects[].received_at" }) };
  }
  const responses = [...(command.responses ?? [])];
  const response = responses.reverse().find((item) => ["accepted", "rejected"].includes(decisionOf(item)))
    ?? responses.find((item) => decisionOf(item) === "received");
  if (response !== undefined) {
    const decision = decisionOf(response);
    const label = { received: "Received by Host Management",
      accepted: "Accepted by Host Management, not yet started",
      rejected: "Rejected by Host Management" }[decision];
    // HostCore's served reason token names why (e.g. reboot_scope_or_expiry), when present.
    const why = response.message?.message?.reason;
    return { state: decision, label,
      fact: fact({ kind: "reported", source: LAYER_NAMES.host_core, receipt: "first",
        value: `a "${decision}" response${typeof why === "string" && why !== "" ? ` (${words(why)})` : ""}`,
        receivedAt: response.received_at, readAt, field: "responses[].received_at" }) };
  }
  const recorded = fact({ kind: "set",
    value: command.operator_audit_ref ? `Recorded by Central · audit ${command.operator_audit_ref}` : "Recorded by Central",
    receivedAt: command.issued_at, readAt });
  if (isTime(command.expires_at) && isTime(readAt) && command.expires_at <= readAt) {
    return { state: "outcome_unknown",
      label: `Outcome unknown: no response from Host Management; Central stopped offering it at ${clock(command.expires_at)}`,
      fact: recorded };
  }
  const paused = notOffered(command, nodeDevice);
  return { state: "requested",
    label: paused !== null
      ? `Requested · Central is not offering it now (${paused})`
      : isTime(command.expires_at)
        ? `Requested · delivery unknown · Central offers it to Host Management until ${clock(command.expires_at)}`
        : "Requested · delivery unknown · Central's offer window is not served",
    fact: recorded };
}

/**
 * One app operation's named state (§10). Central keeps `state` at `staged` whatever the
 * broker answered, so the broker's `command_response` is read too: a rejected stage reads
 * "Rejected by App Effect Broker", never "Staged". A superseded or interrupted operation
 * keeps what the broker had reported before (its latest effect, else its response) as
 * `prior`, a second Evidence fact, so a rejection is not hidden behind the later state.
 *
 * @param {object} operation one entry of the app-attempts read's `operations`
 * @param {number|null} readAt that read's `read_at`
 * @returns {CommandState & {prior: import("./facts.js").Fact|null}}
 */
export function appOperationState(operation, readAt) {
  return { prior: null, ...operationState(operation, readAt) };
}

function operationState(operation, readAt) {
  const response = operation.command_response;
  const effect = operation.latest_effect;
  const recorded = fact({ kind: "set", value: operation.operator_audit_ref
    ? `Stage recorded by Central · audit ${operation.operator_audit_ref}` : "Stage recorded by Central" });
  // One phase event of this operation, received once: a `first` receipt, never the broker's
  // last report (Central does not serve when the broker layer last reported).
  const effectFact = () => fact({ kind: "reported", source: LAYER_NAMES.app_effect_broker, receipt: "first",
    value: effect?.phase ? `phase ${words(effect.phase)}` : null, receivedAt: effect?.received_at, readAt,
    field: "latest_effect.received_at" });
  const responseFact = () => fact({ kind: "reported", source: LAYER_NAMES.app_effect_broker, receipt: "first",
    value: `a "${words(response.decision)}" response`, receivedAt: response.received_at, readAt,
    field: "command_response.received_at" });
  // What the broker had reported before a later stage or boot replaced this operation.
  const prior = () => (effect != null ? effectFact() : response != null ? responseFact() : null);
  switch (operation.state) {
    case "staged": {
      if (response == null) {
        return { state: "staged", label: "Staged; no response from App Effect Broker", fact: recorded };
      }
      const label = { received: "Received by App Effect Broker",
        accepted: "Accepted by App Effect Broker; preparing",
        rejected: "Rejected by App Effect Broker" }[response.decision];
      if (label === undefined) {
        return { state: "unknown", label: "Unknown response from App Effect Broker",
          fact: fact({ kind: "unknown", why: `Central served an unrecognised decision "${words(response.decision)}"` }) };
      }
      return { state: response.decision, label, fact: responseFact() };
    }
    case "switching":
      return { state: "switching", label: `App Effect Broker reported switching (${words(effect?.phase)})`,
        fact: effectFact() };
    case "target_running":
      return { state: "target_running", label: "App Effect Broker reported the staged app running", fact: effectFact() };
    case "fallback_running":
      return { state: "fallback_running", label: "App Effect Broker reported the fallback app running",
        fact: effectFact() };
    case "effect_unknown":
      return { state: "effect_unknown", label: "App Effect Broker reported the outcome as unknown", fact: effectFact() };
    case "superseded":
      return { state: "superseded", label: "Replaced by a later stage",
        fact: fact({ kind: "set", value: "A later stage is recorded by Central" }), prior: prior() };
    case "interrupted_by_reboot":
      return { state: "interrupted_by_reboot", label: "Interrupted",
        fact: fact({ kind: "derived", value: "Interrupted", basis: "a later boot of this Player was admitted" }),
        prior: prior() };
    default:
      return { state: "unknown", label: "Unknown state",
        fact: fact({ kind: "unknown", why: `Central served an unrecognised state "${words(operation.state)}"` }) };
  }
}

/**
 * THE one send path for a reboot (§10): judge the frozen request on `node.latest()` — the
 * hook's newest read at the moment of sending — and POST its body (first time or retry, the
 * same bytes) only when `rebootRefusal` allows it; otherwise refuse without a POST. Central's
 * answer is read in the equipment outcome vocabulary: a 410 (the window ended) is Outcome
 * unknown, never "refused"; an unanswered or gateway-failed request is unknown and may be
 * retried with the same body.
 *
 * @param {string} deviceId
 * @param {FrozenRebootRequest} request
 * @param {{latest: () => import("./nodeRead.js").NodeDevice}} node the `useNodeDevice` hook
 * @returns {Promise<RebootResult>}
 */
export async function sendReboot(deviceId, request, node) {
  const refusal = rebootRefusal(request, node.latest());
  if (refusal !== null) return { outcome: "changed", message: `${refusal}.`, retryable: false };
  let result;
  try {
    result = await apiWrite(`/v1/operator/node/devices/${encodeURIComponent(deviceId)}/reboots`,
      { method: "POST", body: request.body });
  } catch {
    return { outcome: "unknown", message: UNKNOWN_MESSAGE, retryable: true };
  }
  return rebootResult(result);
}

/**
 * Central's answer to a reboot request, as an outcome (exported for the model tests).
 *
 * @param {{ok: boolean, status: number, error: string|null, data: any}} result
 * @returns {RebootResult}
 */
export function rebootResult(result) {
  if (result.ok) {
    return result.data?.duplicate === true
      ? { outcome: "already", message: "Already recorded. Requested · delivery unknown.", retryable: false }
      : { outcome: "done", message: "Reboot recorded. Requested · delivery unknown.", retryable: false };
  }
  const code = result.error;
  if (result.status === 410) {
    return { outcome: "unknown", retryable: false,
      message: "Outcome unknown: Central stopped offering this request before it was confirmed." };
  }
  // Central's own refusals carry its node_/rollout_ codes; anything else at 5xx did not
  // come from the reboot owner (a gateway, a lost response) and its outcome is unknown.
  const central = typeof code === "string" && /^(node|rollout)_/.test(code);
  if (result.status >= 500 && !central) {
    return { outcome: "unknown", message: UNKNOWN_MESSAGE, retryable: true };
  }
  if (code === "node_reboot_outstanding") {
    return { outcome: "changed", retryable: false,
      message: `${REBOOT_OUTSTANDING}.` };
  }
  if (code === "node_reboot_session_unavailable" || code === "rollout_gate_closed") {
    return { outcome: "changed", message: CHANGED_MESSAGE, retryable: false };
  }
  return { outcome: "refused", message: `Refused: ${words(code ?? result.status)}.`, retryable: false };
}
