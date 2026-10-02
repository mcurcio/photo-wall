import React, { createContext, useCallback, useContext, useMemo } from "react";

import { apiWrite } from "./apiWrite.js";
import { clock, fact, words } from "./facts.js";
import { usePolledRead } from "./polledRead.js";

/**
 * Node control (console DDD Part E §24-§25, R20): whether this Central runs node control, and
 * the fleet effect gate, from ONE shell read of `GET /v1/operator/node/status`.
 *
 * The console assumes node control. A Central started without it is a misconfiguration,
 * shown once as a banner above every page (`NodeControlBanner`); node pages then show one
 * "not shown" line (`NodeRecords`) and mount their node reads with `skip`. This is the ONLY
 * module that reads `transport_enabled` and the only console source of the effect gate
 * (a source-scan test holds both): every gate consumer takes the shell's value.
 *
 * States, re-judged on every read (§27): `on` (`transport_enabled: true`), `off` (`false`),
 * `unread` (no answer yet, or the read failed: no banner and no claim). The console never
 * infers `off` from a page-level refusal: a node read refused `node_control_disabled` is an
 * ordinary failed read until this read raises the banner.
 *
 * @typedef {{effective_state: string, state?: string, reason?: string|null,
 *            generation?: number, changed_at?: number|null}} EffectGate
 *   the served `effect_gate` row (central/fleet/rollout_gate.py `status`)
 * @typedef {{state: "on"|"off"|"unread", gate: EffectGate|null, failed: boolean}} NodeControlRead
 *   `failed`: the last read got no usable answer (as opposed to no answer yet)
 * @typedef {NodeControlRead & {refresh: () => Promise<void>, latest: () => NodeControlRead}} NodeControl
 */

const CADENCE_MS = 30000;
const UNREAD = Object.freeze({ state: "unread", gate: null, failed: false });

/**
 * One node status answer as the shell's state (exported for the model tests).
 *
 * @param {{ok: boolean, data: any}|null} result `apiWrite`'s answer, or null when unanswered
 * @returns {NodeControlRead}
 */
export function controlFromStatus(result) {
  const enabled = result?.ok === true ? result.data?.transport_enabled : undefined;
  if (enabled === true || enabled === false) {
    const gate = result.data?.effect_gate;
    return Object.freeze({ state: enabled ? "on" : "off",
      gate: gate !== null && typeof gate === "object" ? gate : null, failed: false });
  }
  return Object.freeze({ state: "unread", gate: null, failed: true });
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

/** What every consumer reads when no Shell provides node control (no claim either way). */
export const NodeControlContext = createContext(
  /** @type {NodeControl} */ ({ ...UNREAD, refresh: noRefresh, latest: () => UNREAD }));

/** The shell's node control, from context. */
export const useNodeControlValue = () => useContext(NodeControlContext);

/**
 * Whether a node page may send its node reads: with node control on, or when the status read
 * failed (pages then show their own read failures). Not while it is off, and not before the
 * first answer, so a Central without node control receives no node read at all.
 *
 * @param {NodeControlRead} control
 * @returns {boolean}
 */
export function nodeReadsAllowed(control) {
  return control.state === "on" || (control.state === "unread" && control.failed);
}

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

const BANNER_TITLE = "Node management is off on this Central.";
const NOT_SHOWN = "Node records are not shown: node management is off (see the banner).";
const h = React.createElement;

/**
 * The one banner (§25), above every page, only while node control is `off`.
 *
 * @param {{control: NodeControlRead}} props
 */
export function NodeControlBanner({ control }) {
  if (control.state !== "off") return null;
  return h("div", { className: "node-banner", role: "region", "aria-label": "Node control" },
    h("p", null,
      h("strong", null, BANNER_TITLE),
      " It was started without node control: Players that boot by node path are refused, and node "
        + "records, Reboot, App operations and Releases have nothing to read. Start Central with ",
      h("code", null, "central.node_app"), " and set ", h("code", null, "PHOTO_WALL_NODE_AUDIENCE"),
      " (runbook › Node control)."));
}

/**
 * A page's node sections, or, while node control is `off`, the one "not shown" line in
 * their place. `quiet` renders nothing instead, for further node sections on a page that
 * already shows the line once.
 *
 * @param {{children: React.ReactNode, quiet?: boolean}} props
 */
export function NodeRecords({ children, quiet = false }) {
  const control = useNodeControlValue();
  if (control.state !== "off") return children;
  return quiet ? null : h("p", { className: "roster__empty" }, NOT_SHOWN);
}
