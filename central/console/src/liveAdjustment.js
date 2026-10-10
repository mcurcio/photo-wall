import { useCallback, useEffect, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { isBound } from "./join.js";
import { draftKey, useDraft } from "./useDraft.js";
import { useMutate } from "./useMutate.js";

/*
 * A Frame's live adjustment (the Frame page's Position and Picture tabs): the operator's draft
 * of the Frame's calibration (useDraft.js) and the live session that shows it on the Frame's
 * Display while one of those tabs is open. Central calls the session a calibration trial
 * (central/fleet/node_calibration.py); none of that wording reaches the page.
 *
 * THE DRAFT IS THE OPERATOR'S, THE SESSION ONLY SHOWS IT. A draft carries its base, the saved
 * revision it was made from (useDraft.js), and every request that hands Central a draft sends
 * that base: `begin` with a changed draft, `edit`, and `renew`. So a draft outlives any number
 * of sessions (a tab revisited, a page hidden and shown again, a handover) and still cannot
 * overwrite a save made elsewhere: Central refuses a draft whose base is no longer the saved
 * revision. A session begun for an unchanged draft starts at the saved calibration, which the
 * draft then adopts (with its revision), so the draft follows saves made elsewhere.
 *
 * THE SESSION RUNS WHILE THE TAB IS OPEN AND SHOWN. `open` true (a Position or Picture tab is
 * shown for a bound Frame whose Pi serves live adjustment) and the browser tab visible begins a
 * session; the page then sends the draft as it changes, and every TICK_MS asks Central to keep
 * the session (`keepalive`), which also reads whether the Pi has shown the latest change.
 * Central ends a session it has not heard from within its idle window, so a closed page ends it
 * within seconds; `open` turning false, the browser tab hidden or the page unmounting ends it
 * at once, and a session still being begun or changed when that happens is ended as its answer
 * arrives. RENEW_BEFORE_S before Central's hard limit the page hands the session over with
 * `renew`, which ends it and begins the next one at the draft in one step: the Display never
 * shows the saved calibration in between and no other window can begin in a gap. The remaining
 * time is the served hard deadline minus the served touch time (both Central's clock, so only
 * their difference is used), less the time since the answer arrived on this browser's own
 * clock (never comparing the two clocks). A session that ends anyway while the tab is open (the
 * Pi restarted, a Done) is begun again with the draft.
 *
 * REFUSALS. Most pass: the page keeps the session and keeps trying, and the words go away once
 * a later answer succeeds (the Pi not having shown the latest change goes once it has). A few
 * stop it until the operator answers (STOPPING: someone saved meanwhile; the Frame lost its
 * Pi): the session is ended and the page waits for Revert.
 *
 * DONE (`save`) is offered only once the Pi has shown the latest change (Central refuses it
 * otherwise); REVERT ends the session and returns the draft to the saved calibration. Both are
 * the operator's: `busy` is true only while one runs, never for the loop's own requests, and
 * each waits for a loop request in flight rather than being dropped.
 */

const TICK_MS = 500;
const EDIT_DELAY_MS = 180;
const RENEW_BEFORE_S = 15;
const RETRY_MS = 2000;

// Someone saved this Frame's calibration after the draft was made (Central's compare-and-set).
const OVERTAKEN =
  "Someone saved a different position or picture for this Frame meanwhile. Press Revert to load it, then adjust again.";

/** Central's refusals, in the operator's words. */
const REFUSALS = {
  trial_display_unavailable: "This Frame's Pi is not connected to Photo Wall. Check that it is on.",
  trial_current_output_required:
    "The Pi has not reported this Display in the last few seconds. Check that the Display is on and plugged in.",
  trial_admitted_surface_required: "The Pi is still starting its picture. Wait a moment.",
  trial_latest_not_presented: "The Pi has not presented the latest change yet. Wait a moment, then press Done again.",
  trial_sequence_conflict: "Another window changed this Frame at the same moment.",
  trial_frame_unbound: "Choose which Pi and HDMI output feed this Frame first (Hardware tab).",
  trial_baseline_revision_changed: OVERTAKEN,
  calibration_revision_conflict: OVERTAKEN,
  calibration_trial_baseline_changed: OVERTAKEN,
};
const NO_ANSWER = "no_answer";
const NO_ANSWER_WORDS = "Photo Wall's server did not answer.";
// The refusals that stop the live adjustment until the operator presses Revert.
const STOPPING = new Set([
  "trial_baseline_revision_changed", "calibration_revision_conflict", "calibration_trial_baseline_changed",
  "trial_frame_unbound",
]);
const NOT_PRESENTED = "trial_latest_not_presented";
// Begin is refused while another session for this Frame runs (another window, or this page's
// own a moment ago); Central ends an abandoned one within seconds, so it is tried again.
const BUSY = "trial_already_active";

// The pages left with changes not kept, by Frame, and when (this browser's clock, epoch
// seconds, only ever shown): the next visit says the Display went back to the saved values then.
const leftUnsaved = new Map();
// The `end` this browser last sent for each Frame's sessions, until it is answered: a `begin`
// for the same Frame waits for it, so a Revert or a quick return to the tab (or the page) never
// races its own end and reads it as someone else's session.
const ending = new Map();

/** The browser tab is shown (the Page Visibility API). */
function useVisible() {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

/**
 * @typedef {{corners: number[][], crop: number[], rotation: number, gain: number}} Draft
 * @typedef {"checking"|"unsupported"|"unavailable"|"unbound"|"idle"|"connecting"|"waiting"
 *           |"stopped"|"sending"|"shown"} Phase
 *   `unsupported`: the Pi's software cannot show changes live; `waiting`: another session for
 *   this Frame is running; `stopped`: a refusal stopped it until Revert; `sending`: the latest
 *   change is not shown yet; `shown`: the Pi has presented it.
 * @typedef {{words: string, code: string, stops: boolean}} Problem
 *   `code` is Central's, for a Details disclosure only.
 */

/**
 * @param {{frameId: string, frame: object|undefined, open: boolean}} props
 * @returns {{draft: Draft, change: (patch: Partial<Draft>) => {valid: boolean, reason?: string},
 *            dirty: boolean, phase: Phase, error: Problem|null, busy: boolean,
 *            ack: {revision: number, at: number}|null, latest: number|null,
 *            done: () => Promise<boolean>, revert: () => Promise<void>, retry: () => void,
 *            savedAt: number|null, leftAt: number|null}}
 *   `latest` is the number of the change last made (null with no session); `ack` the newest
 *   one the Pi presented and when (Central's clock, seconds), so Done is offered only when
 *   they match. `savedAt` and `leftAt` are this browser's clock (epoch seconds, shown only,
 *   never compared with a served time): the last Done Central accepted, and when a previous
 *   visit left this Frame with changes not kept.
 */
export function useFrameAdjustment({ frameId, frame, open }) {
  const committed = frame?.calibration ?? null;
  const { trying: draft, origin, base, dirty, updateHandles: change, clearDraft, adopt } =
    useDraft(frameId, committed);
  const bound = isBound(frame);
  const generation = frame?.generation ?? null;
  const path = `/v1/operator/frames/${encodeURIComponent(frameId)}/calibration-trials`;
  const mutate = useMutate();
  const visible = useVisible();

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

  const live = open && visible && bound && capability === "native_trial";
  const [row, setRow] = useState(/** @type {object|null} */ (null));
  const [error, setError] = useState(/** @type {Problem|null} */ (null));
  const [waiting, setWaiting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [savedAt, setSavedAt] = useState(/** @type {number|null} */ (null));
  const [leftAt, setLeftAt] = useState(/** @type {number|null} */ (null));
  // Read by the loop between renders. `flight` is the request in flight (one at a time);
  // `acting` holds the loop while an operator's Done or Revert runs.
  const state = useRef({ row: null, received: 0, flight: null, acting: false, error: null, waitUntil: 0,
    editAt: 0, resync: false, draft, origin, base, dirty, live, mounted: true });
  Object.assign(state.current, { draft, origin, base, dirty, live, error });

  const keep = useCallback((next) => {
    state.current.row = next;
    state.current.received = performance.now();
    setRow(next);
  }, []);
  const fail = useCallback((code) => {
    const problem = { words: code === NO_ANSWER ? NO_ANSWER_WORDS : REFUSALS[code] ?? "Photo Wall refused the change.",
      code, stops: STOPPING.has(code) };
    state.current.error = problem;
    setError(problem);
  }, []);
  const end = useCallback((session) => {
    const done = apiWrite(`${path}/${session.trial_id}`, {
      method: "POST", body: { operation: "end", expected_sequence: session.sequence },
    }).catch(() => {}).finally(() => {
      if (ending.get(path) === done) ending.delete(path);
    });
    ending.set(path, done);
    return done;
  }, [path]);

  /** The draft as Central takes it: the saved calibration it was made from, changed, and its base. */
  const payload = () => ({ ...state.current.origin, ...state.current.draft, revision: state.current.base });

  /** One request; null when it failed (the problem is set) or the page went away. */
  const send = useCallback(async (operation, calibration) => {
    const current = state.current;
    const before = current.row;
    const begin = operation === "begin";
    const body = begin ? (calibration ? { calibration } : {}) : {
      operation, expected_sequence: before.sequence, ...(calibration ? { calibration } : {}),
    };
    const request = () => apiWrite(begin ? path : `${path}/${before.trial_id}`, { method: "POST", body });
    let result;
    try {
      result = operation === "save" ? await mutate(request) : await request();
    } catch {
      if (current.mounted && current.live) fail(NO_ANSWER);
      current.resync = !begin;
      if (begin) current.waitUntil = performance.now() + RETRY_MS;
      return null;
    }
    if (!current.mounted || !current.live) {
      // The tab or the page was left while this was in flight: a session it began or kept is
      // ended now, with the sequence it answered.
      if (result.ok && result.data?.state === "active") end(result.data);
      return null;
    }
    if (!result.ok) {
      if (begin && result.error === BUSY) {
        current.waitUntil = performance.now() + RETRY_MS;
        setWaiting(true);
        return null;
      }
      const code = result.error ?? `http_${result.status}`;
      fail(code);
      if (begin) current.waitUntil = performance.now() + RETRY_MS;
      if (STOPPING.has(code) && current.row?.state === "active") {
        end(current.row);
        keep(null);
      }
      // After a refusal the session as served may have moved on: read it before editing again.
      current.resync = !begin;
      return null;
    }
    setWaiting(false);
    keep(result.data);
    const passing = current.error;
    const shown = result.data.presented_sequence === result.data.sequence &&
      result.data.presented_sha256 === result.data.candidate_sha256;
    if (passing !== null && !passing.stops && (passing.code !== NOT_PRESENTED || shown)) {
      current.error = null;
      setError(null);
    }
    return result.data;
  }, [path, keep, fail, end, mutate]);

  /** Runs `work` as the one request in flight. */
  const fly = useCallback(async (work) => {
    const flight = work();
    state.current.flight = flight;
    try {
      await flight;
    } finally {
      if (state.current.flight === flight) state.current.flight = null;
    }
  }, []);

  // The loop: one step decides the next request from the session as last served.
  const step = useCallback(() => {
    const current = state.current;
    if (!current.live || current.flight !== null || current.acting || current.error?.stops) return;
    fly(async () => {
      const session = current.row;
      if (session === null || session.state !== "active") {
        if (performance.now() < current.waitUntil) return;
        while (ending.has(path)) await ending.get(path);
        if (!state.current.live) return;
        const changed = current.dirty;
        const begun = await send("begin", changed ? payload() : undefined);
        // A session begun for an unchanged draft is at the saved calibration: adopt it.
        if (begun !== null && !changed && !state.current.dirty) adopt(begun.calibration);
        return;
      }
      if (current.resync) {
        current.resync = false;
        await send("keepalive");
        return;
      }
      const left = session.hard_expires_at - session.touched_at - (performance.now() - current.received) / 1000;
      if (left < RENEW_BEFORE_S) {
        await send("renew", payload());
        return;
      }
      if (draftKey(current.draft) !== draftKey(session.calibration)) {
        if (performance.now() < current.editAt) return;
        await send("edit", payload());
        return;
      }
      await send("keepalive");
    });
  }, [fly, send, adopt, path]);

  useEffect(() => {
    if (!live) return undefined;
    step();
    const timer = setInterval(step, TICK_MS);
    return () => clearInterval(timer);
  }, [live, step]);

  // A draft change is sent shortly after the operator stops changing it.
  const changeKey = draftKey(draft);
  useEffect(() => {
    if (!live) return undefined;
    state.current.editAt = performance.now() + EDIT_DELAY_MS;
    const timer = setTimeout(step, EDIT_DELAY_MS + 10);
    return () => clearTimeout(timer);
  }, [changeKey, live, step]);

  // Leaving the tab, hiding the browser tab or leaving the page ends the session now
  // (Central's idle window is the net).
  useEffect(() => {
    if (live) return undefined;
    const session = state.current.row;
    if (session?.state === "active") end(session);
    keep(null);
    setWaiting(false);
    return undefined;
  }, [live, end, keep]);
  useEffect(() => {
    const current = state.current;
    current.mounted = true;
    if (leftUnsaved.has(frameId)) {
      setLeftAt(leftUnsaved.get(frameId));
      leftUnsaved.delete(frameId);
    }
    return () => {
      current.mounted = false;
      if (current.row?.state === "active") end(current.row);
      if (current.dirty) leftUnsaved.set(frameId, Date.now() / 1000);
    };
  }, [frameId, end]);

  /** An operator's action: waits for the loop's request in flight, holds the loop meanwhile. */
  const act = useCallback(async (work) => {
    const current = state.current;
    if (current.acting) return undefined;
    current.acting = true;
    setBusy(true);
    try {
      while (current.flight !== null) await current.flight.catch(() => {});
      let outcome;
      await fly(async () => { outcome = await work(); });
      return outcome;
    } finally {
      current.acting = false;
      if (current.mounted) setBusy(false);
    }
  }, [fly]);

  const active = row?.state === "active";
  const changed = active && draftKey(draft) !== draftKey(row.calibration);
  const presented = active && row.presented_sequence != null && row.presented_sha256 === row.candidate_sha256;
  const latest = active ? row.sequence + (changed ? 1 : 0) : null;
  const ack = presented ? { revision: row.presented_sequence, at: row.presented_at ?? null } : null;
  const shown = latest !== null && ack?.revision === latest;

  /** @type {Phase} */
  let phase;
  if (!bound) phase = "unbound";
  else if (capability === null) phase = "checking";
  else if (capability === "legacy_preview") phase = "unsupported";
  else if (capability !== "native_trial") phase = "unavailable";
  else if (!open || !visible) phase = "idle";
  else if (error?.stops) phase = "stopped";
  else if (waiting) phase = "waiting";
  else if (!active) phase = "connecting";
  else phase = shown ? "shown" : "sending";

  const done = useCallback(() => act(async () => {
    const result = await send("save");
    if (result?.state !== "saved") return false;
    setSavedAt(Date.now() / 1000);
    adopt(result.saved_calibration);
    return true;
  }), [act, send, adopt]);
  // Revert also clears a refusal: the next session begins at the saved calibration, which the
  // draft adopts.
  const revert = useCallback(() => act(async () => {
    const session = state.current.row;
    keep(null);
    if (session?.state === "active") await end(session);
    clearDraft();
    state.current.error = null;
    setError(null);
    state.current.waitUntil = 0;
  }), [act, end, keep, clearDraft]);
  const retry = useCallback(() => {
    state.current.error = null;
    setError(null);
    state.current.waitUntil = 0;
    if (capability !== "native_trial") setRetries((count) => count + 1);
  }, [capability]);

  return {
    draft, change, dirty, phase, error, busy, ack, latest, savedAt, leftAt,
    done, revert, retry,
  };
}
