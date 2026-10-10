import { useCallback, useEffect, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { isBound } from "./join.js";
import { useDraft } from "./useDraft.js";
import { useMutate } from "./useMutate.js";

/*
 * A Frame's live adjustment (the Frame page's Position and Picture tabs): the operator's draft
 * of the Frame's calibration (useDraft.js) and the live session that shows it on the Frame's
 * Display while one of those tabs is open. Central calls the session a calibration trial
 * (central/fleet/node_calibration.py); none of that wording reaches the page.
 *
 * THE SESSION RUNS WHILE THE TAB IS OPEN. `open` true (a Position or Picture tab is shown for a
 * bound Frame whose Pi serves live adjustment) begins a session; the page then sends the draft as
 * it changes, and every TICK_MS asks Central to keep the session (`keepalive`), which also reads
 * whether the Pi has shown the latest change. Central ends a session it has not heard from within
 * its idle window, so a closed page ends it within seconds; `open` turning false or the page
 * unmounting ends it at once. A session that ends while the tab is open (it reached Central's
 * hard limit, the Pi restarted, a Done or a Revert ended it) is begun again, and the draft sent
 * to it, so the operator never sees it expire. The hard limit is reached only by beginning the
 * next one RENEW_BEFORE_S early: the remaining time is the served hard deadline minus the
 * served touch time (both Central's clock, so only their difference is used), less the time
 * since the answer arrived on this browser's own clock (never comparing the two clocks).
 *
 * DONE (`save`) is offered only once the Pi has shown the latest change (Central refuses it
 * otherwise); REVERT ends the session and returns the draft to the saved calibration.
 */

const TICK_MS = 500;
const EDIT_DELAY_MS = 180;
const RENEW_BEFORE_S = 15;
const BUSY_RETRY_MS = 2000;

// Someone saved this Frame's calibration after the session began (Central's compare-and-set).
const OVERTAKEN =
  "Someone saved a different position or picture for this Frame meanwhile. Press Revert to load it, then adjust again.";

/** Central's refusals, in the operator's words. */
const REFUSALS = {
  trial_display_unavailable: "This Frame's Pi is not connected to Photo Wall. Check that it is on.",
  trial_current_output_required:
    "The Pi has not reported this Display in the last few seconds. Check that the Display is on and plugged in.",
  trial_admitted_surface_required: "The Pi is still starting its picture. Wait a moment.",
  trial_latest_not_presented: "The Pi has not shown the latest change yet. Wait a moment, then press Done again.",
  trial_sequence_conflict: "Another window changed this Frame at the same moment.",
  trial_frame_unbound: "Choose which Pi and HDMI output feed this Frame first (Hardware tab).",
  trial_baseline_revision_changed: OVERTAKEN,
  calibration_revision_conflict: OVERTAKEN,
  calibration_trial_baseline_changed: OVERTAKEN,
};
const NO_ANSWER = "Photo Wall's server did not answer.";
// Begin is refused while another session for this Frame runs (another window, or this page's
// own a moment ago); Central ends an abandoned one within seconds, so it is tried again.
const BUSY = "trial_already_active";

const key = (value) => JSON.stringify([value.corners, value.crop, value.rotation, value.gain]);

/**
 * @typedef {{corners: number[][], crop: number[], rotation: number, gain: number}} Draft
 * @typedef {"checking"|"unsupported"|"unavailable"|"unbound"|"idle"|"connecting"|"waiting"
 *           |"sending"|"shown"} Phase
 *   `unsupported`: the Pi's software cannot show changes live; `waiting`: another session for
 *   this Frame is running; `sending`: the latest change is not shown yet; `shown`: the Pi has
 *   shown it.
 */

/**
 * @param {{frameId: string, frame: object|undefined, open: boolean}} props
 * @returns {{draft: Draft, change: (patch: Partial<Draft>) => {valid: boolean, reason?: string},
 *            dirty: boolean, phase: Phase, error: string|null, busy: boolean,
 *            canDone: boolean, done: () => Promise<void>, revert: () => Promise<void>,
 *            retry: () => void, saved: number}}
 *   `saved` counts the Done presses Central accepted, for a page to announce.
 */
export function useFrameAdjustment({ frameId, frame, open }) {
  const committed = frame?.calibration ?? null;
  const { trying: draft, updateHandles: change, clearDraft } = useDraft(frameId, committed);
  const bound = isBound(frame);
  const generation = frame?.generation ?? null;
  const base = `/v1/operator/frames/${encodeURIComponent(frameId)}/calibration-trials`;
  const mutate = useMutate();

  const [capability, setCapability] = useState(/** @type {string|null} */ (null));
  const [retries, setRetries] = useState(0);
  useEffect(() => {
    let current = true;
    setCapability(null);
    if (!bound) return undefined;
    apiWrite(`/v1/operator/frames/${encodeURIComponent(frameId)}/calibration-capability`, { method: "GET" })
      .then((result) => { if (current) setCapability(result.ok ? result.data?.mode ?? "unavailable" : "unavailable"); })
      .catch(() => { if (current) setCapability("unavailable"); });
    return () => { current = false; };
  }, [frameId, generation, bound, retries]);

  const live = open && bound && capability === "native_trial";
  const [row, setRow] = useState(/** @type {object|null} */ (null));
  const [error, setError] = useState(/** @type {string|null} */ (null));
  const [waiting, setWaiting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(0);
  // Read by the loop between renders.
  const state = useRef({ row: null, received: 0, gate: false, error: null, waitUntil: 0, editAt: 0,
    resync: false, draft, live, mounted: true });
  state.current.draft = draft;
  state.current.live = live;
  state.current.error = error;

  const keep = useCallback((next) => {
    state.current.row = next;
    state.current.received = performance.now();
    setRow(next);
  }, []);

  /** One request; null when it failed (the error is set) or the page went away. */
  const send = useCallback(async (operation, calibration) => {
    const before = state.current.row;
    const path = operation === "begin" ? base : `${base}/${before.trial_id}`;
    const body = operation === "begin" ? {} : {
      operation, expected_sequence: before.sequence, ...(calibration ? { calibration } : {}),
    };
    const request = () => apiWrite(path, { method: "POST", body });
    let result;
    try {
      result = operation === "save" ? await mutate(request) : await request();
    } catch {
      if (state.current.mounted) setError(NO_ANSWER);
      state.current.resync = operation !== "begin";
      return null;
    }
    if (!state.current.mounted) return null;
    if (!result.ok) {
      if (operation === "begin" && result.error === BUSY) {
        state.current.waitUntil = performance.now() + BUSY_RETRY_MS;
        setWaiting(true);
        return null;
      }
      setError(REFUSALS[result.error] ?? `Photo Wall refused the change (${result.error ?? result.status}).`);
      // After a refusal the session as served may have moved on: read it before editing again.
      state.current.resync = operation !== "begin";
      return null;
    }
    setWaiting(false);
    keep(result.data);
    return result.data;
  }, [base, keep, mutate]);

  /** Runs `work` alone: the loop and the buttons never send two requests at once. */
  const exclusive = useCallback(async (work) => {
    if (state.current.gate) return;
    state.current.gate = true;
    setBusy(true);
    try {
      await work();
    } finally {
      state.current.gate = false;
      if (state.current.mounted) setBusy(false);
    }
  }, []);

  // The loop: one step decides the next request from the session as last served.
  const step = useCallback(() => exclusive(async () => {
    const current = state.current;
    if (!current.live || current.error !== null) return;
    const session = current.row;
    if (session === null || session.state !== "active") {
      if (performance.now() < current.waitUntil) return;
      await send("begin");
      return;
    }
    if (current.resync) {
      current.resync = false;
      await send("keepalive");
      return;
    }
    const left = session.hard_expires_at - session.touched_at - (performance.now() - current.received) / 1000;
    if (left < RENEW_BEFORE_S) {
      await send("end");
      return; // the next step begins the next session
    }
    if (key(current.draft) !== key(session.calibration)) {
      if (performance.now() < current.editAt) return;
      await send("edit", { ...session.calibration, ...current.draft, revision: session.calibration_revision });
      return;
    }
    await send("keepalive");
  }), [exclusive, send]);

  useEffect(() => {
    if (!live) return undefined;
    step();
    const timer = setInterval(step, TICK_MS);
    return () => clearInterval(timer);
  }, [live, step]);

  // A draft change is sent shortly after the operator stops changing it.
  const draftKey = key(draft);
  useEffect(() => {
    if (!live) return undefined;
    state.current.editAt = performance.now() + EDIT_DELAY_MS;
    const timer = setTimeout(step, EDIT_DELAY_MS + 10);
    return () => clearTimeout(timer);
  }, [draftKey, live, step]);

  // Leaving the tab, or the page, ends the session now (Central's idle window is the net).
  useEffect(() => {
    if (live) return undefined;
    const session = state.current.row;
    if (session?.state === "active") {
      apiWrite(`${base}/${session.trial_id}`, {
        method: "POST", body: { operation: "end", expected_sequence: session.sequence },
      }).catch(() => {});
    }
    state.current.row = null;
    setRow(null);
    setWaiting(false);
    return undefined;
  }, [live, base]);
  useEffect(() => {
    state.current.mounted = true;
    return () => {
      state.current.mounted = false;
      const session = state.current.row;
      if (session?.state === "active") {
        apiWrite(`${base}/${session.trial_id}`, {
          method: "POST", body: { operation: "end", expected_sequence: session.sequence },
        }).catch(() => {});
      }
    };
  }, [base]);

  const active = row?.state === "active";
  const changed = active && key(draft) !== key(row.calibration);
  const shown = active && !changed && row.presented_sequence === row.sequence &&
    row.presented_sha256 === row.candidate_sha256;
  const dirty = committed !== null && key(draft) !== key({
    corners: committed.corners ?? [[0, 0], [1, 0], [1, 1], [0, 1]], crop: committed.crop ?? [0, 0, 1, 1],
    rotation: committed.rotation ?? 0, gain: committed.gain ?? 1,
  });

  /** @type {Phase} */
  let phase;
  if (!bound) phase = "unbound";
  else if (capability === null) phase = "checking";
  else if (capability === "legacy_preview") phase = "unsupported";
  else if (capability !== "native_trial") phase = "unavailable";
  else if (!open) phase = "idle";
  else if (waiting) phase = "waiting";
  else if (!active) phase = "connecting";
  else phase = shown ? "shown" : "sending";

  const done = useCallback(() => exclusive(async () => {
    const result = await send("save");
    if (result?.state === "saved") setSaved((count) => count + 1);
  }), [exclusive, send]);
  // Revert also clears a refusal: the next session begins at the saved calibration.
  const revert = useCallback(() => exclusive(async () => {
    if (state.current.row?.state === "active") await send("end");
    clearDraft();
    setError(null);
  }), [clearDraft, exclusive, send]);
  const retry = useCallback(() => {
    setError(null);
    state.current.waitUntil = 0;
    if (capability !== "native_trial") setRetries((count) => count + 1);
  }, [capability]);

  return {
    draft, change, dirty, phase, error, busy, saved,
    canDone: shown && dirty && !busy && error === null,
    done, revert, retry,
  };
}
