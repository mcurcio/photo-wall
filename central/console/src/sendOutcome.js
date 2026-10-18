import { useMemo, useRef, useState } from "react";

/**
 * The one home of a sent write's outcome pattern (design rule 3: done / already / changed /
 * refused / unknown), shared by the equipment writes (equipmentApi.js), Reboot
 * (fleetCommands.js), Stage (stage.js), Select and its sibling verbs (releases.js) and the
 * Show side's writes. Each verb keeps its own code table; what they share lives here: when an
 * answer leaves the outcome unknown, the held-request store, and the words for "unknown",
 * "changed" and re-sending a frozen request.
 */

export const UNKNOWN_MESSAGE = "Central did not answer. Check this after the next refresh.";

// The one wording of the "changed" outcome in the confirmation dialogs (ConfirmAction.jsx);
// bind words its own conflict in BIND_MESSAGES.
export const CHANGED_MESSAGE = "Changed since you opened this. Reopen to review.";

/** The one label for re-sending the frozen request whose answer was lost (§27). */
export const RESEND_LABEL = "Send the same request again";

/**
 * Whether Central's answer leaves the outcome unknown: no answer at all (a throw, `null`), or
 * a 5xx that is not one of the verb's own refusals (a gateway, a lost answer). Which 5xx codes
 * are the owner's own refusals is the verb's to say (`centralRefusal`): none for the
 * equipment writes, the `node_`/`rollout_` codes for Reboot, any served code for the release verbs.
 *
 * @param {{ok: boolean, status: number, error?: string|null}|null|undefined} result
 * @param {(code: string|null|undefined) => boolean} [centralRefusal]
 * @returns {boolean}
 */
export function answerUnknown(result, centralRefusal = () => false) {
  if (result == null) return true;
  return !result.ok && result.status >= 500 && !centralRefusal(result.error);
}

/**
 * Central's answer to a node or release write as an outcome. The console keys on the code, never
 * the status (Central re-maps codes onto statuses). An unanswered request, or a 5xx with no code
 * (a gateway, a lost answer), is unknown; any code not listed is refused. The served code rides
 * on the outcome (`code`), so a caller branches on it, never on the words.
 *
 * @typedef {{outcome: "done"|"already"|"changed"|"refused"|"unknown", message: string, code?: string|null}} Outcome
 * @param {{ok: boolean, status: number, error: string|null, data: any}|null} result
 * @param {(data: any) => string} done the verb's words for a recorded write
 * @param {Readonly<Record<string, {outcome: Outcome["outcome"], message: string}>>} codes the
 *   verb's own code table
 * @returns {Outcome}
 */
export function answerOutcome(result, done, codes) {
  // Any served code at 5xx is Central's own refusal.
  if (answerUnknown(result, (code) => typeof code === "string" && code !== "")) {
    return { outcome: "unknown", message: UNKNOWN_MESSAGE, code: null };
  }
  if (result.ok) {
    return result.data?.duplicate === true
      ? { outcome: "already", message: `Already recorded. ${done(result.data)}`, code: null }
      : { outcome: "done", message: done(result.data), code: null };
  }
  const code = result.error;
  if (typeof code !== "string" || code === "") {
    return { outcome: "refused", message: `Central refused: ${result.status}.`, code: null };
  }
  const known = Object.hasOwn(codes, code) ? codes[code] : undefined;
  return known === undefined ? { outcome: "refused", message: `Central refused: ${code}.`, code }
    : { outcome: known.outcome, message: `${known.message}.`, code };
}

/**
 * The requests one page holds, by id (`HeldRequests`): a ref, so a send marks its request
 * held in the same step as its check, and a render after each change. `state` is
 * "in_flight", "recorded" or "unknown"; `frozen` is the request body this page sent, so a
 * re-send ({@link RESEND_LABEL}) sends exactly that body. Each page holds its own.
 *
 * @typedef {"in_flight"|"recorded"|"unknown"} HeldState
 * @typedef {{get: (id: string) => HeldState|null, frozen: (id: string) => object|null,
 *            set: (id: string, state: HeldState|null, request?: object|null) => void}} HeldRequests
 * @returns {HeldRequests}
 */
export function useHeldRequests() {
  const heldRef = useRef(/** @type {Map<string, {state: HeldState, request: object|null}>} */ (new Map()));
  const [, setVersion] = useState(0);
  return useMemo(() => ({
    get: (id) => heldRef.current.get(id)?.state ?? null,
    frozen: (id) => heldRef.current.get(id)?.request ?? null,
    set: (id, state, request = null) => {
      if (state === null) heldRef.current.delete(id);
      else heldRef.current.set(id, { state, request });
      setVersion((version) => version + 1);
    },
  }), []);
}
