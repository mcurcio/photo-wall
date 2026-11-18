import { apiWrite } from "./apiWrite.js";
import { answerUnknown, CHANGED_MESSAGE, UNKNOWN_MESSAGE } from "./sendOutcome.js";
import { clock as localClock, fact, LAYER_NAMES, words } from "./facts.js";
import { auditRef, frozen } from "./frozenRequest.js";
import { outputStates } from "./health.js";
import { liveRunsFor } from "./join.js";
import { nodeUnknown } from "./nodeRead.js";

/**
 * The fleet's commands on one box (console DDD §10-§11, bead B2): Reboot Player and the
 * read-only app operations, as pure functions from the node read (nodeRead.js) to the
 * request the console sends and the named state it shows.
 *
 * REBOOT (decision 0034 S4c). Central queues the reboot as one job that asks the Pi's Host
 * Management over the Pi's link (central/fleet/pi_reboot.py); no node session is named.
 * `rebootTarget` needs only an active device read. `rebootRequest` freezes the WHOLE request
 * body when the dialog opens (command id, reason); a retry re-sends that frozen body unchanged,
 * so Central answers it as a duplicate.
 *
 * ONE SEND RULE (§10, §16). A new command id may be sent only when no reboot of this Pi is
 * OUTSTANDING — Central serves `outstanding` per request: asked, not yet run, and still on its
 * queue, the same queue whose one-per-Pi copy its `node_reboot_outstanding` refusal reads. The
 * request the page itself holds (`heldReboot`) counts as outstanding until a read lists it or
 * Central's read time reaches its frozen `retryUntil` (`rebootOffer`). `sendReboot` is the ONLY
 * console function that POSTs to the reboots route (a source-scan test enforces it), and it
 * judges the rule on `node.latest()`, the hook's newest read at the moment of sending, never on
 * a read the caller hands it.
 *
 * STATES. `rebootCommandState` and `appOperationState` compute each state only from
 * Central's records and Central's read time (R10): no browser or node clock is involved.
 * A later boot is never linked to a request: a later boot does not show what caused it.
 *
 * @typedef {{available: true, deviceGeneration: number, kernelBootId: string|null}
 *         | {available: false, reason: string}} RebootTarget
 *   `kernelBootId`: the box's current Host Management session's boot, when served (the Update
 *   Wall page tells a later boot by it)
 * @typedef {{command_id: string, reason: string}} RebootBody
 * @typedef {{body: RebootBody, kernelBootId: string|null, retryUntil: number,
 *            frames: Array<{frameId: string, runs: Array<{runId: string, sceneId: string, phase: string}>}>,
 *            previous: string|null}} FrozenRebootRequest
 * @typedef {{state: string, label: string, fact: import("./facts.js").Fact}} CommandState
 * @typedef {{outcome: "done"|"already"|"changed"|"refused"|"unknown", message: string,
 *            retryable: boolean}} RebootResult
 * @typedef {{offer: "new"} | {offer: "retry", reason: string}
 *         | {offer: "blocked", reason: string}} RebootOffer
 *   `retry`: only the held request may be re-sent (its frozen body); `reason` says why no
 *   new request may be built.
 */

/** A request this page sent that no read lists yet may be re-sent for this long (Central's clock). */
export const REBOOT_RETRY_SECONDS = 30;

// The reason sent: `console/<Central's date>[/<note>]`, one word (pi_reboot.py `_REASON`).
const TOKEN = /^[A-Za-z0-9][A-Za-z0-9_.:/-]*$/;
const MAX_REASON = 256;

const isTime = (value) => typeof value === "number" && Number.isFinite(value);
const isGeneration = (value) => Number.isInteger(value) && value >= 1;

/** The kernel boot of the box's ONE current Host Management session, or null. */
export function hostBoot(read) {
  const hosts = (read?.sessions ?? []).filter((session) => session.current && session.producer?.owner === "host_core");
  return hosts.length === 1 && typeof hosts[0].producer?.kernel_boot_id === "string"
    ? hosts[0].producer.kernel_boot_id : null;
}

/**
 * Whether this box may be asked to reboot, from the node read shown, or why not. Central
 * serves the device read only for an active device; it asks the Pi over its link.
 *
 * @param {import("./nodeRead.js").NodeDevice|null} nodeDevice
 * @returns {RebootTarget}
 */
export function rebootTarget(nodeDevice) {
  const why = nodeUnknown(nodeDevice);
  if (why !== null) return { available: false, reason: why };
  if (nodeDevice.error !== null && nodeDevice.error !== undefined) {
    return { available: false,
      reason: `the last node read failed (${nodeDevice.error.code}); press Refresh` };
  }
  const read = nodeDevice.read;
  if (!isGeneration(read.device_generation)) return { available: false, reason: "the device generation is not served" };
  return { available: true, deviceGeneration: read.device_generation, kernelBootId: hostBoot(read) };
}

/**
 * The request this page keeps after sending it, or null. A request Central recorded
 * (done, already) or whose outcome is unknown and may be retried is held; one Central
 * refused is not.
 *
 * @param {FrozenRebootRequest} request
 * @param {RebootResult} result
 * @returns {FrozenRebootRequest|null}
 */
export function heldReboot(request, result) {
  return result.outcome === "done" || result.outcome === "already" || result.retryable ? request : null;
}

/** Central serves `outstanding` per request (§16); anything but `false` is treated as outstanding. */
const isOutstanding = (command) => command?.outstanding !== false;

/**
 * What the Reboot control offers now (§10): a new request, only a retry of the request
 * this page holds, or nothing (with why).
 *
 * Every listed request is judged by Central's served `outstanding` — not only the newest. The
 * held request counts as outstanding until a read lists it (then Central's `outstanding`
 * decides) or Central's read time reaches its frozen `retryUntil`; while it counts, only it may
 * be re-sent.
 *
 * Passing a frozen request as `held` asks whether THAT request may be sent now: "retry" means
 * yes. `sendReboot` and the dialog ask exactly this (`rebootRefusal`).
 *
 * @param {RebootTarget} target
 * @param {Array<object>|null} commands the read's `reboot_commands` (every one)
 * @param {number|null} readAt the device read's `read_at`
 * @param {FrozenRebootRequest|null} held the request this page sent (`heldReboot`)
 * @returns {RebootOffer}
 */
export function rebootOffer(target, commands, readAt, held) {
  if (!target.available) return { offer: "blocked", reason: target.reason };
  const listed = commands ?? [];
  const other = listed.find((command) => command.command_id !== held?.body.command_id && isOutstanding(command));
  if (other !== undefined) {
    return { offer: "blocked", reason: "a reboot request is queued; only the page that sent it can retry it" };
  }
  if (held != null) {
    const record = listed.find((command) => command.command_id === held.body.command_id);
    if (record !== undefined ? isOutstanding(record) : isTime(readAt) && readAt < held.retryUntil) {
      return { offer: "retry",
        reason: "the reboot request this page sent may still be queued; only that request can be retried" };
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
 * @param {number|null} readAt the device read's `read_at` (Central's clock; dates the reason)
 * @param {{playerId: string|null, commandId: string, reason?: string,
 *          held?: FrozenRebootRequest|null, clock?: (seconds: number) => string}} options
 * @returns {FrozenRebootRequest|{refused: string}}
 */
export function rebootRequest(target, commands, snapshot, readAt,
  { playerId, commandId, reason = "", held = null, clock = localClock }) {
  const offer = rebootOffer(target, commands, readAt, held);
  if (offer.offer !== "new") return { refused: offer.reason };
  if (!isTime(readAt)) return { refused: "Central's read time is not served" };
  const note = reason.trim();
  const reference = auditRef(readAt, note);
  if (note !== "" && (!TOKEN.test(note) || reference.length > MAX_REASON)) {
    return { refused: "a reason is one word: letters, digits and _ . : / - only" };
  }
  const frames = playerId === null ? [] : outputStates(snapshot, playerId)
    .filter((output) => output.state === "bound")
    .map((output) => ({ frameId: output.frameId,
      runs: liveRunsFor(snapshot?.runtime, output.frameId)
        .map((run) => ({ runId: run.run_id, sceneId: run.scene_id, phase: run.phase })) }));
  const [latest = null] = commands ?? [];
  const previous = latest == null ? null
    : `The previous request: ${rebootCommandState(latest, readAt, { clock }).label}.`;
  return frozen({
    body: { command_id: commandId, reason: reference },
    kernelBootId: target.kernelBootId,
    // Central's clock: the read time it was frozen at plus the retry window; `rebootOffer`
    // stops counting a request no read lists at this time.
    retryUntil: readAt + REBOOT_RETRY_SECONDS,
    frames,
    previous,
  });
}

/** What the reboot dialog says when its frozen request may no longer be sent (§10). */
export const REBOOT_STALE = "This request is out of date; close and reopen";
/**
 * The one wording for "another reboot request for this Player is outstanding" (§16): the
 * client-side refusal and Central's 409 `node_reboot_outstanding` are the same outcome.
 */
export const REBOOT_OUTSTANDING =
  "Another reboot request for this Player is outstanding; close this dialog and review it";

/**
 * Why a frozen request may not be sent now (first time or retry), or null: `rebootOffer`
 * with the request as `held`, on the given read. Another outstanding request refuses it; so
 * does a request that a read has settled, or that no read lists once Central's read time
 * reaches its `retryUntil` (reopening rebuilds it from a fresh read).
 *
 * @param {FrozenRebootRequest} request
 * @param {import("./nodeRead.js").NodeDevice|null} nodeDevice
 * @returns {string|null}
 */
export function rebootRefusal(request, nodeDevice) {
  const target = rebootTarget(nodeDevice);
  const offer = rebootOffer(target, nodeDevice?.read?.reboot_commands, nodeDevice?.readAt ?? null, request);
  if (offer.offer === "retry") return null;
  if (!target.available) return `${REBOOT_STALE} (${target.reason})`;
  return offer.offer === "blocked" ? REBOOT_OUTSTANDING : REBOOT_STALE;
}

/**
 * One reboot request's named state (§10), from Central's record and read time only: queued
 * (Requested), asked and accepted by Host Management, not delivered (no link, or no answer),
 * or off the queue with no answer recorded (Outcome unknown).
 *
 * @param {object} command one `reboot_commands` record of the device read
 * @param {number|null} readAt the device read's `read_at`
 * @param {{clock?: (seconds: number) => string}} [options] `clock` formats Central's times
 * @returns {CommandState}
 */
export function rebootCommandState(command, readAt, { clock = localClock } = {}) {
  const asked = [typeof command.reason === "string" ? `reason ${command.reason}` : null,
    typeof command.by === "string" ? `by ${command.by}` : null].filter((part) => part !== null);
  const recorded = fact({ kind: "set",
    value: asked.length > 0 ? `Recorded by Central · ${asked.join(" · ")}` : "Recorded by Central",
    receivedAt: command.requested_at, readAt });
  if (command.delivered === true) {
    return { state: "asked", label: "Host Management accepted the reboot · completion unknown",
      fact: fact({ kind: "reported", source: LAYER_NAMES.host_core, receipt: "first",
        value: "an accepted answer to Central's ask", receivedAt: command.answered_at, readAt,
        field: "answered_at" }) };
  }
  if (command.delivered === false) {
    return { state: "not_delivered",
      label: `Not delivered: the Pi had no link to Central or did not answer${
        isTime(command.answered_at) ? ` at ${clock(command.answered_at)}` : ""}; its next boot is offered its version`,
      fact: recorded };
  }
  if (isOutstanding(command)) {
    return { state: "requested", label: "Requested · delivery unknown · Central asks Host Management over the Pi's link",
      fact: recorded };
  }
  return { state: "outcome_unknown", label: "Outcome unknown: Central's queue no longer holds it and recorded no answer",
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
    case "effect_unknown":
      return { state: "effect_unknown", label: "App Effect Broker reported the outcome as unknown", fact: effectFact() };
    case "superseded":
      return { state: "superseded", label: "Replaced by a later stage",
        fact: fact({ kind: "set", value: "A later stage is recorded by Central" }), prior: prior() };
    case "interrupted_by_reboot":
      return { state: "interrupted_by_reboot", label: "Interrupted",
        fact: fact({ kind: "derived", value: "Interrupted", basis: "a later boot of this Player was admitted" }),
        prior: prior() };
    case "ended_by_later_boot":
      // G2: the stage had finished (target or fallback running); a later boot was admitted, and Central offers each
      // boot its Player's own version.
      return { state: "ended_by_later_boot", label: "Ended by a later boot",
        fact: fact({ kind: "derived", value: "Ended by a later boot",
          basis: "a later boot was admitted; Central offers each boot its Player's own version" }),
        prior: prior() };
    default:
      return { state: "unknown", label: "Unknown state",
        fact: fact({ kind: "unknown", why: `Central served an unrecognised state "${words(operation.state)}"` }) };
  }
}

/** Devices with a reboot POST in flight from this page (module state: one page, one send path). */
const rebootsInFlight = new Set();
const REBOOT_IN_FLIGHT_MESSAGE = "A reboot for this Player is already being sent.";

/**
 * THE one send path for a reboot (§10): judge the frozen request on `node.latest()` — the
 * hook's newest read at the moment of sending — and POST its body (first time or retry, the
 * same bytes) only when `rebootRefusal` allows it; otherwise refuse without a POST. Central's
 * answer is read in the equipment outcome vocabulary: an unanswered or gateway-failed request
 * is unknown and may be retried with the same body.
 *
 * @param {string} deviceId
 * @param {FrozenRebootRequest} request
 * @param {{latest: () => import("./nodeRead.js").NodeDevice}} node the `useNodeDevice` hook
 * @returns {Promise<RebootResult>}
 */
export async function sendReboot(deviceId, request, node) {
  // The per-device in-flight guard lives here, in the one send path, so no caller can bring back a double send.
  // It is taken synchronously before the first await and always released: apiWrite's abort budget settles the POST.
  if (rebootsInFlight.has(deviceId)) {
    return { outcome: "changed", message: REBOOT_IN_FLIGHT_MESSAGE, retryable: false };
  }
  const refusal = rebootRefusal(request, node.latest());
  if (refusal !== null) return { outcome: "changed", message: `${refusal}.`, retryable: false };
  rebootsInFlight.add(deviceId);
  let result;
  try {
    result = await apiWrite(`/v1/operator/node/devices/${encodeURIComponent(deviceId)}/reboots`,
      { method: "POST", body: request.body });
  } catch {
    return { outcome: "unknown", message: UNKNOWN_MESSAGE, retryable: true };
  } finally {
    rebootsInFlight.delete(deviceId);
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
  // Central's own refusals carry its node_ codes; anything else at 5xx did not come from the
  // reboot owner (a gateway, a lost response) and its outcome is unknown.
  if (answerUnknown(result, (served) => typeof served === "string" && served.startsWith("node_"))) {
    return { outcome: "unknown", message: UNKNOWN_MESSAGE, retryable: true };
  }
  if (code === "node_reboot_outstanding") {
    return { outcome: "changed", retryable: false,
      message: `${REBOOT_OUTSTANDING}.` };
  }
  if (code === "node_device_unavailable") {
    return { outcome: "changed", message: CHANGED_MESSAGE, retryable: false };
  }
  return { outcome: "refused", message: `Refused: ${words(code ?? result.status)}.`, retryable: false };
}
