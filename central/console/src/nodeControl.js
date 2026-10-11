import { createContext, useCallback, useContext, useMemo } from "react";

import { apiWrite } from "./apiWrite.js";
import { clock, fact, words } from "./facts.js";
import { usePolledRead } from "./polledRead.js";

/**
 * Node control (console DDD Part E §24-§25, R20): the fleet effect gate, from ONE shell read of
 * `GET /v1/operator/node/status`. Central always runs node control; this is the only console
 * source of the effect gate (a source-scan test holds it): every gate consumer takes the
 * shell's value. A failed or unanswered read leaves the gate unreadable (`gate: null`), never
 * a claim; each node page shows its own read failures.
 *
 * @typedef {{effective_state: string, state?: string, reason?: string|null,
 *            generation?: number, changed_at?: number|null}} EffectGate
 *   the served `effect_gate` row (central/fleet/rollout_gate.py `status`)
 * @typedef {{gate: EffectGate|null}} NodeControlRead
 * @typedef {NodeControlRead & {refresh: () => Promise<void>, latest: () => NodeControlRead}} NodeControl
 */

const CADENCE_MS = 30000;
const UNREAD = Object.freeze({ gate: null });

/**
 * One node status answer as the shell's state (exported for the model tests).
 *
 * @param {{ok: boolean, data: any}|null} result `apiWrite`'s answer, or null when unanswered
 * @returns {NodeControlRead}
 */
export function controlFromStatus(result) {
  const gate = result?.ok === true ? result.data?.effect_gate : null;
  return gate !== null && typeof gate === "object" ? Object.freeze({ gate }) : UNREAD;
}

/**
 * The shell's node status read: on sign-in, every 30 s while visible, when the tab becomes
 * visible, and on demand (a Reboot dialog opening). Mounted once, in the Shell.
 *
 * @param {{skip?: boolean}} [options] `skip` while signed out
 * @returns {NodeControl}
 */
export function useNodeControl({ skip = false } = {}) {
  const load = useCallback(async () => controlFromStatus(
    await apiWrite("/v1/operator/node/status", { method: "GET" }).catch(() => null)), []);
  const { value, refresh, latest } = usePolledRead(load, { cadenceMs: CADENCE_MS, skip, initial: UNREAD });
  // One object per read, so the context's consumers re-render only when it changes.
  return useMemo(() => ({ ...value, refresh, latest }), [value, refresh, latest]);
}

const noRefresh = async () => {};

/** What every consumer reads when no Shell provides node control (the gate unreadable). */
export const NodeControlContext = createContext(
  /** @type {NodeControl} */ ({ ...UNREAD, refresh: noRefresh, latest: () => UNREAD }));

/** The shell's node control, from context. */
export const useNodeControlValue = () => useContext(NodeControlContext);

// Central's gate reason codes in words (§26); any other code is shown as its words.
const REASONS = Object.freeze({
  never_certified: "no deployment certification has opened it",
  deployment_changed: "the deployment changed",
});

/** Why the effect gate cannot be read: the status read has not answered or failed. */
export const GATE_UNREADABLE = "Central's effect gate is not readable";

/**
 * A closed gate's reason in its one wording (§26): an expired certification, or Central's
 * reason code in words. Stage's `rollout_gate_closed` refusal quotes it (stage.js).
 *
 * @param {EffectGate} gate
 * @returns {string}
 */
export function effectGateReason(gate) {
  return gate.state === "open" ? "its certification expired"
    : gate.reason ? REASONS[gate.reason] ?? words(gate.reason) : "none served";
}

/**
 * The effect gate in its one wording (§26): Central's state and reason, never an age (node
 * status serves no read time, so Central's timestamp is displayed, never subtracted from a
 * browser clock; R10).
 *
 * @param {EffectGate|null} gate
 * @returns {import("./facts.js").Fact}
 */
export function effectGateFact(gate) {
  if (gate == null) return fact({ kind: "unknown", why: GATE_UNREADABLE });
  if (gate.effective_state === "open") {
    return fact({ kind: "set", value: "Effect gate open · certified by the deployment · "
      + `generation ${gate.generation ?? "not served"} · Central re-checks its serving evidence on every Reboot and Stage` });
  }
  const reason = effectGateReason(gate);
  const recorded = typeof gate.changed_at === "number" ? ` · recorded ${clock(gate.changed_at)}` : "";
  return fact({ kind: "set", value: `Effect gate closed · Central's reason: ${reason}${recorded}` });
}
