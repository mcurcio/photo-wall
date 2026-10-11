import { useCallback, useEffect, useRef, useState } from "react";

import { TICK_MS, useVisible } from "./liveAdjustment.js";
import {
  beginPicture, editPicture, keepPictureAlive, pictureDone, placePicture, readFramePicture, revertPicture,
} from "./pictureApi.js";

/*
 * The Picture tab's live session (roadmap 1c, slice K1; design language §8 row B5, the Live
 * model): the operator's draft of the Display's four picture settings, shown on the display while
 * the tab is open, then kept (Done) or put back (Revert). Central's rules are
 * central/displays/picture.py's; this module only drives the routes (pictureApi.js).
 *
 * THE DRAFT IS THE OPERATOR'S. A draft holds only what the operator changed in this visit: a value
 * per setting and, for brightness and contrast, where it acts. Every session begun while the tab is
 * open is sent the whole draft, so the draft outlives a session that ended by itself (the browser
 * tab hidden, the Pi restarted, Central's idle window). Done and Revert clear it.
 *
 * THE SESSION RUNS WHILE THE TAB IS OPEN AND SHOWN. `open` and the browser tab visible begin a
 * session (after this page's own Revert of an earlier one is answered, so it is never read as
 * someone else's); every TICK_MS the page keeps it and reads the tab (GET …/picture), which says
 * whether the Pi has shown the latest change. Closing the tab, hiding the browser tab or leaving
 * the page ends it at once with Revert (Central's idle window is the net).
 *
 * AT MOST TWO EDITS A SECOND. A change is sent at once when the last edit went EDIT_GAP_MS ago,
 * else when the gap is over, with the newest value: while a slider is dragged the display is
 * written at most twice a second (some displays save each write to memory that wears), and the
 * value where it was let go is always the last one sent. One request is in flight at a time, so
 * each edit names the sequence the previous one answered.
 *
 * SHOWN. The latest change counts as shown only when nothing is waiting to be sent, a read begun
 * after the last accepted change says `shown`, and that read carries the draft's values. Done is
 * offered only then; Central checks it again (`not_presented_yet`).
 */

/** The least time between two edits: at most 2 a second while dragging (design: "Live model"). */
export const EDIT_GAP_MS = 500;
const RETRY_MS = 2000;

/** The four settings, as Central names them (central/displays/picture.py `Setting`). */
export const SETTINGS = /** @type {const} */ (["brightness", "contrast", "colour-temperature", "gamma"]);

/** Central's refusals, in the operator's words; `stops`: the session waits for the operator. */
const REFUSALS = {
  frame_unbound: "Choose which Pi and HDMI output feed this Frame first (Hardware tab).",
  no_display: "The Pi has not reported a display on this HDMI port yet, so its picture waits until it does.",
  session_open: "Someone else is adjusting this display's picture. Photo Wall connects as soon as they finish.",
  stale_sequence: "Another window changed this display's picture at the same moment.",
  not_placed_yet: "This display has not been connected yet, so its brightness and contrast wait until it is.",
  out_of_range: "Photo Wall refused this value.",
  display_did_not_answer: "The display did not answer the Pi's check, so this setting stays a Photo Wall picture adjustment.",
  not_presented_yet: "The Pi has not shown the latest change yet. Wait a moment, then press Done again.",
  unreachable: "Photo Wall's server did not answer.",
};
// Begin refusals that leave the tab without a session until something changes on the Pi's side.
const UNAVAILABLE = new Set(["frame_unbound", "no_display", "unknown_frame"]);
// The session is gone: begin again (with the draft).
const GONE = new Set(["session_ended", "unknown_session"]);
// Refused values the draft drops (the slider goes back to what Central has).
const DROPPED = new Set(["out_of_range", "not_placed_yet", "display_did_not_answer"]);

// The pages left with changes not kept, by Frame, and when (this browser's clock, epoch seconds,
// only ever shown): the next visit says the display went back then.
const leftUnsaved = new Map();
// The Revert this browser last sent for each Frame's session, until it is answered: a begin for
// the same Frame waits for it.
const ending = new Map();

/** A setting's value as a read shows it: a percent (null: as the display has it), kelvin or tenths. */
export function viewValue(view, setting) {
  if (setting === "brightness" || setting === "contrast") return view[setting].value;
  return setting === "colour-temperature" ? view.colour_temperature_kelvin : view.gamma_tenths;
}

/**
 * @typedef {import("./pictureApi.js").FramePictureView} FramePictureView
 * @typedef {import("./pictureApi.js").Setting} Setting
 * @typedef {import("./pictureApi.js").ActsOn} ActsOn
 * @typedef {{values: Partial<Record<Setting, number>>, places: Partial<Record<"brightness"|"contrast", ActsOn>>}} Draft
 * @typedef {"idle"|"live"|"waiting"|"shown"} Phase
 *   `idle`: no session (not open, no display, or someone else's session); `live`: a session and
 *   no change to judge; `waiting`: the Pi has not shown the latest change; `shown`: it has.
 * @typedef {{words: string, code: string}} Problem   `code` is Central's, for Details only.
 */

const EMPTY = /** @type {Draft} */ ({ values: {}, places: {} });
const isEmpty = (draft) => Object.keys(draft.values).length === 0 && Object.keys(draft.places).length === 0;

/** The draft's next request against what this session already took; null when it has taken it all. */
function nextWrite(draft, taken) {
  for (const [setting, actsOn] of Object.entries(draft.places)) {
    if (taken.places[setting] !== actsOn) return { kind: "place", setting, value: actsOn };
  }
  for (const [setting, value] of Object.entries(draft.values)) {
    if (taken.values[setting] !== value) return { kind: "edit", setting, value };
  }
  return null;
}

/** Whether a read carries every part of the draft. */
function carries(view, draft) {
  return Object.entries(draft.values).every(([setting, value]) => viewValue(view, setting) === value)
    && Object.entries(draft.places).every(([setting, actsOn]) => view[setting].acts_on === actsOn);
}

/**
 * @param {{frameId: string, open: boolean}} props
 * @returns {{view: FramePictureView|null, phase: Phase, draft: Draft, dirty: boolean,
 *            change: (setting: Setting, value: number) => void,
 *            place: (setting: "brightness"|"contrast", actsOn: ActsOn) => void,
 *            done: () => Promise<boolean>, revert: () => Promise<void>, busy: boolean,
 *            error: Problem|null, unavailable: Problem|null, waitingFor: string|null,
 *            latest: number|null, savedAt: number|null, leftAt: number|null}}
 *   `unavailable`: why no session can begin (no Pi, no display); `waitingFor`: someone else's
 *   session, in words; `latest` counts this session's changes, for the bar (null: no session).
 */
export function usePictureSession({ frameId, open }) {
  const visible = useVisible();
  const live = open && visible;
  const [view, setView] = useState(/** @type {FramePictureView|null} */ (null));
  const [draft, setDraftState] = useState(EMPTY);
  const [session, setSessionState] = useState(/** @type {{session_id: string, sequence: number}|null} */ (null));
  const [error, setError] = useState(/** @type {Problem|null} */ (null));
  const [unavailable, setUnavailable] = useState(/** @type {Problem|null} */ (null));
  const [waitingFor, setWaitingFor] = useState(/** @type {string|null} */ (null));
  const [taken, setTakenState] = useState(EMPTY);
  const [shownRead, setShownRead] = useState(false);
  const [changes, setChanges] = useState(0);
  const [busy, setBusy] = useState(false);
  const [savedAt, setSavedAt] = useState(/** @type {number|null} */ (null));
  const [leftAt, setLeftAt] = useState(/** @type {number|null} */ (null));
  // Read by the loop between renders. `taken`: what this session has accepted; `wrote` counts
  // accepted writes and `readFor` is the count a read began after, so a read older than the last
  // write never says shown. `flight` is the request in flight; `acting` holds the loop.
  const state = useRef({
    draft: EMPTY, session: null, taken: EMPTY, wrote: 0, readFor: -1, editAt: 0, waitUntil: 0,
    flight: null, acting: false, live, mounted: true, timer: null,
  });
  state.current.live = live;
  const path = `frame:${frameId}`;

  const setDraft = useCallback((next) => {
    state.current.draft = next;
    setDraftState(next);
  }, []);
  const setTaken = useCallback((next) => {
    state.current.taken = next;
    setTakenState(next);
  }, []);
  const setSession = useCallback((next) => {
    state.current.session = next;
    if (next === null) setTaken(EMPTY);
    setSessionState(next);
  }, [setTaken]);
  const fail = useCallback((code) => setError({ words: REFUSALS[code] ?? "Photo Wall refused the change.", code }), []);

  const end = useCallback((current) => {
    const sent = revertPicture(current.session_id).finally(() => {
      if (ending.get(path) === sent) ending.delete(path);
    });
    ending.set(path, sent);
    return sent;
  }, [path]);

  /** Reads the tab; `shownRead` only when the read began after the last accepted write. */
  const read = useCallback(async () => {
    const current = state.current;
    const after = current.wrote;
    const result = await readFramePicture(frameId);
    if (!current.mounted) return;
    if (!result.ok) {
      fail(result.error);
      return;
    }
    setView(result.view);
    if (after === current.wrote) {
      current.readFor = after;
      setShownRead(result.view.status === "shown" && carries(result.view, current.draft));
    }
  }, [frameId, fail]);

  /** One write of the draft (a place or an edit); false when the loop should stop for now. */
  const write = useCallback(async (next) => {
    const current = state.current;
    const open_ = current.session;
    const body = { setting: next.setting, expected_sequence: open_.sequence };
    const result = next.kind === "place"
      ? await placePicture(open_.session_id, { ...body, acts_on: next.value })
      : await editPicture(open_.session_id, { ...body, value: next.value });
    current.editAt = performance.now() + EDIT_GAP_MS;
    if (!current.mounted || current.session !== open_) return;
    if (result.ok) {
      const part = next.kind === "place" ? "places" : "values";
      setTaken({ ...current.taken, [part]: { ...current.taken[part], [next.setting]: next.value } });
      current.wrote += 1;
      setShownRead(false);
      setSession({ ...open_, sequence: result.sequence.sequence });
      setError(null);
      return;
    }
    if (GONE.has(result.error)) {
      setSession(null);
      return;
    }
    if (result.error === "stale_sequence") {
      const kept = await keepPictureAlive(open_.session_id);
      if (kept.ok && current.session === open_) setSession({ ...open_, sequence: kept.session.sequence });
    } else if (DROPPED.has(result.error)) {
      const part = next.kind === "place" ? "places" : "values";
      const { [next.setting]: _dropped, ...rest } = current.draft[part];
      setDraft({ ...current.draft, [part]: rest });
    }
    fail(result.error);
  }, [fail, setDraft, setSession, setTaken]);

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

  // The loop: one step decides the next request from the session as last answered.
  const step = useCallback(() => {
    const current = state.current;
    if (!current.live || current.flight !== null || current.acting) return;
    void fly(async () => {
      if (current.session === null) {
        if (performance.now() >= current.waitUntil) {
          while (ending.has(path)) await ending.get(path).catch(() => {});
          if (!state.current.live) return;
          const begun = await beginPicture(frameId);
          if (!current.mounted) return;
          if (!current.live) {
            if (begun.ok) void end(begun.session);
            return;
          }
          if (begun.ok) {
            setTaken(EMPTY);
            setSession(begun.session);
            setChanges(0);
            setUnavailable(null);
            setWaitingFor(null);
            setError((problem) => (problem?.code === "unreachable" ? null : problem));
          } else {
            current.waitUntil = performance.now() + RETRY_MS;
            if (begun.error === "session_open") {
              setWaitingFor(REFUSALS.session_open);
            } else if (UNAVAILABLE.has(begun.error)) {
              setUnavailable({ words: REFUSALS[begun.error] ?? REFUSALS.no_display, code: begun.error });
            } else {
              fail(begun.error);
            }
          }
        }
        await read();
        return;
      }
      const next = nextWrite(current.draft, current.taken);
      if (next !== null) {
        const wait = current.editAt - performance.now();
        if (wait <= 0) {
          await write(next);
        } else {
          clearTimeout(current.timer);
          current.timer = setTimeout(step, wait + 5);
        }
      } else {
        const open_ = current.session;
        const kept = await keepPictureAlive(open_.session_id);
        if (current.session === open_ && !kept.ok) {
          if (GONE.has(kept.error)) setSession(null);
          else fail(kept.error);
        }
      }
      if (current.mounted) await read();
    });
  }, [fly, frameId, path, read, write, end, fail, setSession, setTaken]);

  useEffect(() => {
    if (!live) return undefined;
    step();
    const timer = setInterval(step, TICK_MS);
    return () => clearInterval(timer);
  }, [live, step]);

  // Leaving the tab, hiding the browser tab or leaving the page ends the session now (Revert).
  useEffect(() => {
    if (live) return;
    const current = state.current;
    clearTimeout(current.timer);
    if (current.session !== null) {
      void end(current.session);
      setSession(null);
    }
    setWaitingFor(null);
  }, [live, end, setSession]);
  useEffect(() => {
    const current = state.current;
    current.mounted = true;
    if (leftUnsaved.has(frameId)) {
      setLeftAt(leftUnsaved.get(frameId));
      leftUnsaved.delete(frameId);
    }
    return () => {
      current.mounted = false;
      clearTimeout(current.timer);
      if (current.session !== null) void end(current.session);
      if (!isEmpty(current.draft)) leftUnsaved.set(frameId, Date.now() / 1000);
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

  const change = useCallback((setting, value) => {
    const current = state.current;
    if (current.draft.values[setting] === value) return;
    setDraft({ ...current.draft, values: { ...current.draft.values, [setting]: value } });
    setShownRead(false);
    setChanges((count) => count + 1);
    clearTimeout(current.timer);
    current.timer = setTimeout(step, Math.max(0, current.editAt - performance.now()) + 5);
  }, [setDraft, step]);
  const place = useCallback((setting, actsOn) => {
    const current = state.current;
    setDraft({ ...current.draft, places: { ...current.draft.places, [setting]: actsOn } });
    setShownRead(false);
    setChanges((count) => count + 1);
    clearTimeout(current.timer);
    current.timer = setTimeout(step, Math.max(0, current.editAt - performance.now()) + 5);
  }, [setDraft, step]);

  const done = useCallback(() => act(async () => {
    const open_ = state.current.session;
    if (open_ === null) return false;
    const result = await pictureDone(open_.session_id);
    if (!result.ok) {
      if (GONE.has(result.error)) setSession(null);
      setShownRead(false);
      fail(result.error);
      return false;
    }
    setSession(null);
    setDraft(EMPTY);
    setError(null);
    setSavedAt(Date.now() / 1000);
    state.current.waitUntil = 0;
    return true;
  }), [act, fail, setDraft, setSession]);
  const revert = useCallback(() => act(async () => {
    const open_ = state.current.session;
    setSession(null);
    setDraft(EMPTY);
    setError(null);
    if (open_ !== null) await end(open_);
    state.current.waitUntil = 0;
  }), [act, end, setDraft, setSession]);

  const dirty = !isEmpty(draft);
  const pending = session !== null && nextWrite(draft, taken) !== null;
  const shown = session !== null && !pending && shownRead && view !== null && carries(view, draft);
  /** @type {Phase} */
  const phase = session === null ? "idle" : shown ? "shown" : dirty ? "waiting" : "live";
  return {
    view, phase, draft, dirty, change, place, done, revert, busy, error, unavailable, waitingFor,
    latest: session === null ? null : changes, savedAt, leftAt,
  };
}
