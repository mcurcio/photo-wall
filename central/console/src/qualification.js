import { useCallback, useEffect, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { fact, LAYER_NAMES } from "./facts.js";
import { auditRef } from "./frozenRequest.js";
import { usePolledRead } from "./polledRead.js";
import { appHandle, releaseResult } from "./releases.js";
import { boundFrames } from "./stage.js";

/**
 * Qualified fallback (console DDD Part E §25, §27-§28, bead NS2): qualify the Player app this
 * boot linked, so a later Stage has an accepted environment to fall back to.
 *
 * WHAT CENTRAL SERVES (G4). The app-attempts read's `qualification` block names the current
 * boot's linked app (what a qualification observes) and this device generation's newest
 * acceptances. Whether an acceptance fits the current Outputs and base is Central's judgement
 * when a Stage is sent; this module never labels one usable.
 *
 * WHO SAMPLES (Q7). This page does: `useQualificationSampler` asks Central to sample every 2 s
 * while the tab is visible and judges nothing itself; Central judges every sample (30 s
 * sustained, no gap over 5 s on its own clock, witnesses that advance). Sampling stops on an
 * accepted answer, on a terminal or unlisted refusal, or after 2 minutes without progress
 * (an `observing` answer whose sustained seconds grew). The 2 minutes are the page's own
 * monotonic clock, compared only to itself. The qualification id lives in memory only: a
 * closed page begins again (Central restarts its window after any 5 s gap anyway).
 *
 * ONE SEND RULE PER VERB (R13). `sendBegin` judges `beginOffer` on the node hook's newest read
 * at the moment of sending and is the only console caller of the app-qualifications route; the
 * sampler's load is the only caller of the sample route, and it refuses to send once its last
 * answer stopped it (source-scan tests hold both).
 *
 * @typedef {{body: {qualification_id: string, environment_sha256: string,
 *            operator_audit_ref: string}}} FrozenBegin
 * @typedef {{kind: "progress"|"waiting"|"done"|"terminal", text: string,
 *            seconds?: number}} SampleAnswer
 * @typedef {{id: string|null, phase: "sampling"|"stopped"|"accepted", answer: SampleAnswer|null,
 *            stopped: string|null, sustained: number|null, quietMs: number, lastAt: number,
 *            samples: number}} SamplerState
 * @typedef {import("./releases.js").Outcome} Outcome
 */

export const SAMPLE_CADENCE_MS = 2000;
/** No progress for this long stops sampling (§27). */
export const NO_PROGRESS_MS = 120_000;
/**
 * A hidden tab samples nothing, and Central's window restarts after a 5 s gap anyway, so a
 * pause counts at most this much towards the no-progress limit.
 */
const MAX_COUNTED_GAP_MS = 5000;

/** What qualification needs, said out loud (§25a). */
export const REQUIREMENT = "Qualification needs this Player bound and showing one steady photo or looping video "
  + "on every bound Output for 30 s.";
/** Its costs (R16): the page samples, and each sample takes Central's fleet lock. */
export const SAMPLING_COST = "While qualifying, this page asks Central for a sample every 2 s and must stay open "
  + "and visible for at least 30 s; closing it ends this attempt (Central keeps its records). Each sample briefly "
  + "holds Central's fleet lock, so boots, Publish and Select wait for it.";

const NOT_BOUND = "qualification needs this Player bound to a Frame";
const UNLINKED = "the Player app has not linked its current process on this boot";

/** The served block, or null when this Central does not serve it. */
const block = (operations) => {
  const value = operations?.qualification;
  return value != null && typeof value === "object" && Array.isArray(value.acceptances) ? value : null;
};

/**
 * The Qualified fallback facts: the linked app, then each stored acceptance (newest first).
 *
 * @param {object|null} operations the app-attempts read
 * @returns {{linkedApp: import("./facts.js").Fact, acceptances: import("./facts.js").Fact[]}}
 */
export function qualificationView(operations) {
  const served = block(operations);
  if (served === null) {
    const why = operations == null ? "the app operations read has not answered"
      : "Central does not serve the qualification read";
    return { linkedApp: fact({ kind: "unknown", why }), acceptances: [] };
  }
  const readAt = operations.read_at;
  const linked = served.linked_app;
  const linkedApp = linked == null ? fact({ kind: "unknown", why: UNLINKED }) : fact({
    kind: "reported", source: LAYER_NAMES.app_effect_broker, receipt: "first",
    value: `the linked Player app ${appHandle(linked.environment_sha256)}`, receivedAt: linked.admitted_at, readAt,
    field: "the link's admission time",
  });
  const acceptances = served.acceptances.map((row) => fact({
    kind: "set", receivedAt: row.accepted_at, readAt,
    value: `Qualified on this Player: app ${appHandle(row.environment_sha256)} on `
      + `${row.base_tag == null ? "a base other than the one this boot runs" : `base ${row.base_tag}`}`
      + " · physical pixels unknown · Stage checks it against the current Outputs and base when you send",
  }));
  return { linkedApp, acceptances };
}

/**
 * What Begin offers now: Begin (naming the linked environment), "sampling", or nothing (why).
 *
 * @param {object|null} operations the app-attempts read
 * @param {object|null} snapshot the Player app snapshot
 * @param {string|null} playerId
 * @param {boolean} sampling whether this page is sampling for this Player now
 * @returns {{offer: "begin", environmentSha256: string} | {offer: "sampling"}
 *           | {offer: "blocked", reason: string}}
 */
export function beginOffer(operations, snapshot, playerId, sampling) {
  if (sampling) return { offer: "sampling" };
  if (boundFrames(snapshot, playerId).length === 0) return { offer: "blocked", reason: NOT_BOUND };
  if (operations == null) return { offer: "blocked", reason: "the app operations read has not answered" };
  const served = block(operations);
  if (served === null) return { offer: "blocked", reason: "Central does not serve the qualification read" };
  const sha = served.linked_app?.environment_sha256;
  if (typeof sha !== "string" || sha === "") return { offer: "blocked", reason: UNLINKED };
  return { offer: "begin", environmentSha256: sha };
}

/**
 * The Begin request from the read it opened on: the linked environment, an id minted for this
 * attempt, and the audit reference dated by Central's read time. Frozen, so a resend after a
 * lost answer is byte-identical (Central's identity check accepts exactly that).
 *
 * @returns {FrozenBegin|{refused: string}}
 */
export function beginRequest(operations, snapshot, playerId, qualificationId) {
  const offer = beginOffer(operations, snapshot, playerId, false);
  if (offer.offer !== "begin") return { refused: offer.reason };
  if (typeof operations.read_at !== "number") return { refused: "Central's read time is not served" };
  return Object.freeze({ body: Object.freeze({
    qualification_id: qualificationId,
    environment_sha256: offer.environmentSha256,
    operator_audit_ref: auditRef(operations.read_at),
  }) });
}

const refused = (message) => ({ outcome: "refused", message });
const BEGIN_CODES = Object.freeze({
  node_environment_unknown: refused("Central does not know this app environment"),
  node_qualification_identity_conflict: refused("This qualification id was already used for another request"),
  node_device_unavailable: refused("Central has no current node record for this box"),
});

/**
 * THE one send path for Begin: judge the frozen request on `node.latest()` (and the snapshot at
 * call time) and on whether this page samples already; refuse without a request when the rule
 * fails, else POST. Any code not listed is refused.
 *
 * @param {string} deviceId
 * @param {FrozenBegin} request
 * @param {{node: {latest: () => import("./nodeRead.js").NodeDevice}, snapshot: object|null,
 *          playerId: string|null}} reads
 * @param {boolean} sampling
 * @returns {Promise<Outcome>}
 */
export async function sendBegin(deviceId, request, { node, snapshot, playerId }, sampling) {
  const offer = beginOffer(node.latest().operations, snapshot, playerId, sampling);
  if (offer.offer === "sampling") return { outcome: "changed", message: "This page is already sampling." };
  if (offer.offer !== "begin") return { outcome: "changed", message: `Not begun: ${offer.reason}.` };
  if (offer.environmentSha256 !== request.body.environment_sha256) {
    return { outcome: "changed", message: "The Player app's linked process changed; begin again." };
  }
  let result;
  try {
    result = await apiWrite(`/v1/operator/node/devices/${encodeURIComponent(deviceId)}/app-qualifications`,
      { method: "POST", body: request.body });
  } catch {
    result = null;
  }
  return releaseResult(result, () => "Qualification begun.", BEGIN_CODES);
}

// Central's sample codes (§27). Waiting codes keep sampling; every other code stops it.
const WAITING = Object.freeze({
  node_qualification_control_stale: "the Player app has not applied Central's control in the last 5 s",
  node_qualification_control_unlinked: "the Player app's link does not match its current control",
  node_qualification_readiness_stale: "the Player app has not reported readiness in the last 5 s",
  node_qualification_readiness_unavailable: "the Player app reports missing capacity or a failure",
  node_qualification_plan_changed: "this Player's plan or bindings changed",
  node_qualification_handoff_unavailable: "Display Host is not showing the Player app's current surface on every bound Output",
  node_qualification_preview_active: "a preview is active on a bound Output",
  node_qualification_base_unavailable: "Central has no admitted boot for this Player",
  node_output_cohort_unavailable: "Display Host has not reported every Output connected in the last 10 s",
  node_representative_media_evidence_required: "every bound Output must be showing a prepared photo or video",
  node_representative_single_media_required:
    "every bound Output must keep showing the same photo or video at full opacity for 30 s",
  node_representative_frame_witness_mismatch: "Display Host's frame witness does not match the planned media",
});
const TERMINAL = Object.freeze({
  node_qualification_unknown: "Central has no such qualification",
  node_qualification_generation_changed: "this Player's device generation changed",
  node_qualification_base_abi_mismatch: "the linked app does not fit the base this Player booted",
  node_qualification_process_changed: "the Player app's linked process changed or unlinked",
  node_player_unavailable: "Central has no current Player for this box",
  node_device_unavailable: "Central has no current node record for this box",
});
const stopped = (reason) => ({
  kind: "terminal", text: `Stopped: ${reason}. Begin again when the Player app and its Outputs are steady.` });

/**
 * Central's answer to one sample, classified (§27). No answer, or a 5xx with no code, is
 * waiting; any code or status not listed is terminal.
 *
 * @param {{ok: boolean, status: number, error: string|null, data: any}|null} result
 * @returns {SampleAnswer}
 */
export function sampleAnswer(result) {
  if (result == null || (!result.ok && result.error == null && result.status >= 500)) {
    return { kind: "waiting", text: "Waiting: Central did not answer this sample" };
  }
  if (result.ok) {
    const status = result.data?.status;
    if (status === "accepted") return { kind: "done", text: "Qualified · physical pixels unknown" };
    if (status === "awaiting_new_witnesses") {
      return { kind: "waiting", text: `Waiting for new reports from the Player app and ${LAYER_NAMES.display_host}` };
    }
    const seconds = result.data?.sustained_seconds;
    if (status === "observing" && typeof seconds === "number" && Number.isFinite(seconds)) {
      return { kind: "progress", seconds,
        text: `Observing representative media: ${Math.floor(seconds)} s of 30 sustained` };
    }
    return stopped(`Central answered ${String(status ?? "nothing")}`);
  }
  const code = result.error;
  if (typeof code === "string" && Object.hasOwn(WAITING, code)) {
    return { kind: "waiting", text: `Waiting: ${WAITING[code]}` };
  }
  if (typeof code === "string" && Object.hasOwn(TERMINAL, code)) return stopped(TERMINAL[code]);
  return stopped(`Central refused: ${code ?? result.status}`);
}

/**
 * A sampler that has not answered yet.
 *
 * @param {string|null} id
 * @param {number} nowMs the page's monotonic clock
 * @returns {SamplerState}
 */
export const samplerStart = (id, nowMs) => Object.freeze({
  id, phase: "sampling", answer: null, stopped: null, sustained: null, quietMs: 0, lastAt: nowMs, samples: 0 });

/**
 * The sampler after one answer: accepted, stopped (terminal answer, or no progress for 2
 * minutes) or still sampling. Progress is an `observing` answer whose sustained seconds grew
 * over the previous one; the quiet time since the last progress is the page's own monotonic
 * time between answers, a gap counting at most 5 s.
 *
 * @param {SamplerState} state
 * @param {SampleAnswer} answer
 * @param {number} nowMs
 * @returns {SamplerState}
 */
export function samplerStep(state, answer, nowMs) {
  const base = { ...state, answer, lastAt: nowMs, samples: state.samples + 1 };
  if (answer.kind === "done") return Object.freeze({ ...base, phase: "accepted" });
  if (answer.kind === "terminal") return Object.freeze({ ...base, phase: "stopped", stopped: answer.text });
  const grew = answer.kind === "progress" && state.sustained !== null && answer.seconds > state.sustained;
  const quietMs = grew ? 0 : state.quietMs + Math.min(Math.max(0, nowMs - state.lastAt), MAX_COUNTED_GAP_MS);
  const sustained = answer.kind === "progress" ? answer.seconds : state.sustained;
  if (quietMs >= NO_PROGRESS_MS) {
    return Object.freeze({ ...base, quietMs, sustained, phase: "stopped",
      stopped: `Stopped: no progress for 2 minutes. Last answer: ${answer.text}.` });
  }
  return Object.freeze({ ...base, quietMs, sustained });
}

/**
 * This page's sampler for one qualification: a sample every 2 s while visible (the shared
 * polled read's single flight and hidden-tab pause), until accepted, stopped or inactive.
 * A new id starts afresh. The load itself refuses to send for a stopped, accepted or
 * superseded sampler, so no tick that fires before React re-renders can POST again.
 *
 * @param {string|null} qualificationId
 * @param {{active: boolean}} options
 * @returns {SamplerState}
 */
export function useQualificationSampler(qualificationId, { active }) {
  const load = useCallback(async (previous) => {
    const state = previous.id === qualificationId ? previous : samplerStart(qualificationId, performance.now());
    if (qualificationId == null || state.phase !== "sampling") return state;
    let result;
    try {
      result = await apiWrite(`/v1/operator/node/app-qualifications/${encodeURIComponent(qualificationId)}/sample`,
        { method: "POST" });
    } catch {
      result = null;
    }
    return samplerStep(state, sampleAnswer(result), performance.now());
  }, [qualificationId]);
  // Once this id has settled (accepted or stopped), its timer stops too.
  const [settled, setSettled] = useState(/** @type {string|null} */ (null));
  const { value } = usePolledRead(load, {
    cadenceMs: SAMPLE_CADENCE_MS, initial: IDLE,
    skip: !active || qualificationId == null || settled === qualificationId,
  });
  useEffect(() => {
    if (value.id === qualificationId && value.phase !== "sampling") setSettled(qualificationId);
  }, [value, qualificationId]);
  return value.id === qualificationId ? value : samplerStart(qualificationId, 0);
}

const IDLE = samplerStart(null, 0);
