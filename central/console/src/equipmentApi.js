import { apiWrite } from "./apiWrite.js";
import { answerUnknown, UNKNOWN_MESSAGE } from "./sendOutcome.js";

/**
 * The one equipment write module (slice 2 §6): bind, unbind, retire and identify, each
 * with its message table. Every write goes through {@link apiWrite} (the write
 * fence) and answers ONE result shape, so the Binding facet, the Player page
 * and the confirmation dialogs read the same outcomes:
 *
 *  - "done":    2xx.
 *  - "already": the effect is already in place (unbind: 404 not_bound or
 *               unknown_frame).
 *  - "changed": 409 binding_generation_conflict — the Frame moved since the
 *               operator captured its generation. Never resent.
 *  - "refused": any other 4xx, with the reason in plain words.
 *  - "unknown": the request threw (timeout or network error) or answered 5xx
 *               (a gateway or server failure): Central may or may not have
 *               applied it.
 *
 * Wrap each call in `useMutate()` at the call site so Plane A refreshes once
 * the write completes.
 *
 * @typedef {"done"|"already"|"changed"|"refused"|"unknown"} Outcome
 * @typedef {{outcome: Outcome, code: string|null, message: string|null}} EquipmentResult
 */

// UNKNOWN_MESSAGE and CHANGED_MESSAGE live with the outcome pattern (sendOutcome.js); the
// "already" wording of the confirmation dialogs (ConfirmAction.jsx) is the equipment writes'.
export const ALREADY_MESSAGE = "Already done.";

const GONE_PLAYER = "That Player is no longer available. Choose another.";

const BIND_MESSAGES = {
  binding_generation_conflict: "This Frame changed — reload and review its binding.",
  output_already_bound: "That output was just bound elsewhere. Choose another.",
  unknown_or_retired_player: GONE_PLAYER,
  unknown_output: GONE_PLAYER,
  unknown_frame: "This frame no longer exists.",
};

const RETIRE_MESSAGES = {
  player_bound:
    "This Player has a bound output. Only a Player with no bound outputs can be retired; unbind it first.",
  unknown_player: "This Player no longer exists.",
};

/**
 * Send one write and classify its answer.
 *
 * @param {string} path
 * @param {{method: string, body?: any}} init
 * @param {Record<string, string>} messages code -> operator sentence
 * @param {Set<string>} already codes meaning the effect is already in place
 * @param {string} fallback the sentence for an unmapped refusal
 * @returns {Promise<EquipmentResult>}
 */
async function send(path, init, messages, already, fallback) {
  let result;
  try {
    result = await apiWrite(path, init);
  } catch {
    return { outcome: "unknown", code: null, message: UNKNOWN_MESSAGE };
  }
  if (result.ok) {
    return { outcome: "done", code: null, message: null };
  }
  if (answerUnknown(result)) { // any 5xx: no equipment refusal is served as one
    return { outcome: "unknown", code: null, message: UNKNOWN_MESSAGE };
  }
  const code = result.error ?? String(result.status);
  const outcome =
    code === "binding_generation_conflict"
      ? "changed"
      : already.has(code)
        ? "already"
        : "refused";
  return { outcome, code, message: messages[code] ?? fallback };
}

/**
 * Bind an Output to a Frame: PUT /v1/operator/frames/{id}/binding with the
 * explicit (player, output) the operator chose and the Frame generation they
 * saw when they chose it (the server's optimistic fence, central/registry.py).
 *
 * @param {string} frameId
 * @param {string} playerId
 * @param {string} outputId
 * @param {number} expectedGeneration captured when the operator chose
 * @returns {Promise<EquipmentResult>}
 */
export function bind(frameId, playerId, outputId, expectedGeneration) {
  return send(
    `/v1/operator/frames/${frameId}/binding`,
    {
      method: "PUT",
      body: { player_id: playerId, output_id: outputId, expected_generation: expectedGeneration },
    },
    BIND_MESSAGES,
    new Set(),
    "Bind failed — please retry.",
  );
}

/**
 * Unbind a Frame: DELETE /v1/operator/frames/{id}/binding under the same fence.
 *
 * @param {string} frameId
 * @param {number} expectedGeneration captured when the operator opened the dialog
 * @returns {Promise<EquipmentResult>}
 */
export function unbind(frameId, expectedGeneration) {
  return send(
    `/v1/operator/frames/${frameId}/binding`,
    { method: "DELETE", body: { expected_generation: expectedGeneration } },
    {}, // its conflict and 404s are "changed" and "already", worded by the dialog
    new Set(["not_bound", "unknown_frame"]),
    "Unbind failed — please retry.",
  );
}

/**
 * Retire a Player: POST /v1/operator/players/{id}/retire (no body). Permanent:
 * a retired serial is refused at enrollment. Central refuses (409
 * player_bound) while any of its Outputs is bound; retiring twice is a no-op.
 *
 * @param {string} playerId
 * @returns {Promise<EquipmentResult>}
 */
export function retirePlayer(playerId) {
  return send(
    `/v1/operator/players/${playerId}/retire`,
    { method: "POST" },
    RETIRE_MESSAGES,
    new Set(),
    "Retire failed — please retry.",
  );
}

/**
 * Ask a Player app to briefly identify one connected, unbound Output (console DDD §19;
 * players.js `identifyOffer` decides where it is offered). Acceptance means Central queued
 * the request, not that anything was observed on the Panel. The caller owns the success
 * wording and never claims output. `identify_unsupported` names its cause: Central has not
 * negotiated Identify with this Player app's current enrollment (no schema-2 control session
 * on the current epoch offering `identify_output`).
 *
 * @param {string} playerId
 * @param {string} outputId
 * @returns {Promise<EquipmentResult>}
 */
export async function identifyOutput(playerId, outputId) {
  const unknown =
    "The request outcome is unknown. Check the Panel before trying again.";
  let result;
  try {
    result = await apiWrite(
      `/v1/operator/players/${encodeURIComponent(playerId)}/outputs/${encodeURIComponent(outputId)}/identify`,
      { method: "POST" },
    );
  } catch {
    return { outcome: "unknown", code: null, message: unknown };
  }
  if (result.ok) {
    return { outcome: "done", code: null, message: null };
  }
  if (result.status >= 500) {
    return { outcome: "unknown", code: null, message: unknown };
  }
  return {
    outcome: "refused",
    code: result.error ?? String(result.status),
    message: result.error === "identify_unsupported"
      ? "Central has not negotiated Identify with this Player app's current enrollment"
      : result.status === 404 || result.status === 409
        ? "This Output changed or the Player is no longer eligible. Press Refresh before trying again."
        : "Identify was refused. Press Refresh and try again.",
  };
}

// Each Frame's result in an "Unbind all" sequence, in plain words.
const SEQUENCE_LABELS = {
  done: "unbound",
  changed: "changed since you opened this",
  already: "already done",
  unknown: "outcome unknown",
  "not-attempted": "not attempted",
};

/**
 * "Unbind all" (slice 2 §7) is a SEQUENCE, not a batch: one fenced unbind per
 * Frame, in order, each carrying the generation captured when the dialog
 * opened. A stale generation is never resent. A conflict or "already done"
 * continues to the next Frame; an unknown outcome stops, and the rest are not
 * attempted.
 *
 * @param {Array<{frameId: string, generation: number}>} targets
 * @returns {Promise<Array<{frameId: string, outcome: string, label: string}>>}
 */
export async function unbindSequence(targets) {
  const results = [];
  let stopped = false;
  for (const { frameId, generation } of targets) {
    if (stopped) {
      results.push({ frameId, outcome: "not-attempted", label: SEQUENCE_LABELS["not-attempted"] });
      continue;
    }
    const result = await unbind(frameId, generation);
    results.push({
      frameId,
      outcome: result.outcome,
      label: SEQUENCE_LABELS[result.outcome] ?? result.message,
    });
    stopped = result.outcome === "unknown";
  }
  return results;
}
