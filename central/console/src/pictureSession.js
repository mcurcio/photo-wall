import { useCallback, useEffect, useRef, useState } from "react";

import { TICK_MS, useVisible } from "./liveAdjustment.js";
import { listLuts } from "./lutsApi.js";
import {
  beginPicture, editHardware, editLut, editSignalRange, editSoftware, editTestPattern, keepPictureAlive, pictureDone,
  readFramePicture, restorePicture, revertPicture,
} from "./pictureApi.js";

/*
 * The Picture tab's live session (roadmap 1c's Live model, design language §8 row B5; decision
 * 0022, the picture split, slice S2): the operator's draft of the Display's two groups, the
 * display's own settings (hardware) and Photo Wall's (software), shown on the display while
 * the tab is open, then kept (Done) or put back (Revert). Central's rules are
 * central/displays/picture.py's and hardware.py's; this module only drives the routes
 * (pictureApi.js).
 *
 * THE DRAFT IS THE OPERATOR'S. A draft holds only what the operator changed in this visit: a value
 * per display setting (null hands it back: as the display has it), per Photo Wall setting, and the
 * LUT choice (`lut.lut`: a paper profile's digest, or null for no LUT; print look), the signal range
 * (`signal.range`) and the test pattern (`pattern.pattern`, null for none; display tuning). The test
 * pattern is never kept: it is no change to keep (`dirty` leaves it out), and Done and Revert take
 * it off as they end the session. Every
 * session begun while the tab is open is sent the whole draft, so the draft outlives a session that
 * ended by itself (the browser tab hidden, the Pi restarted, Central's idle window). Done and Revert
 * clear it.
 *
 * THE SESSION RUNS WHILE THE TAB IS OPEN AND SHOWN. `open` and the browser tab visible begin a
 * session (after this page's own Revert of an earlier one is answered, so it is never read as
 * someone else's); every TICK_MS the page keeps it and reads the tab (GET …/picture), which says
 * whether the Pi has shown the latest change. Closing the tab or leaving the page ends it at once
 * with Revert (Central's idle window is the net). Hiding the browser tab does too, unless the
 * session shows a test pattern: then the page only stops keeping it, so the pattern stays up while
 * the operator's phone goes from the browser to its camera (owner, 2026-10-14: "It would be nice if
 * the testing screen persisted, even for a minute"). Central keeps a session with a pattern a
 * minute without a keepalive (central/displays/picture.py `idle_seconds`, the one home of that
 * rule); shown again within it, the page keeps the same session, and after it begins a new one with
 * the draft.
 *
 * AT MOST TWO EDITS A SECOND. A change is sent at once when the last edit went EDIT_GAP_MS ago,
 * else when the gap is over, with the newest value: while a slider is dragged the display is
 * written at most twice a second (some displays save each write to memory that wears), and the
 * value where it was let go is always the last one sent. Both groups share the gap. The signal
 * range and the test pattern are choices, never dragged: each is sent at once, ahead of a slider's
 * paced value. One request is in flight at a time, so each edit names the sequence the previous one
 * answered.
 *
 * RESTORE. A picture history entry (historyApi.js) is loaded into Central's draft as one edit of the
 * session (`restore`): it waits for the request in flight like Done, names the current sequence, is
 * tried once more after a `stale_sequence`, and on success the draft becomes what the session now
 * holds, read back from Central (`restoredDraft`), so nothing of the old draft is sent over it and the
 * sliders show the restored values. The test pattern is never restored: the draft keeps its own.
 *
 * THE LUTS. While the tab is open the LUTs list is read once (lutsApi.js), for the picker.
 *
 * SHOWN. The latest change counts as shown only when nothing is waiting to be sent, a read begun
 * after the last accepted change says `shown`, and that read carries the draft's values (the signal
 * range and the test pattern among them). Central's `shown` waits for the Pi's signal answer too.
 * Done is offered only then; Central checks it again (`not_presented_yet`).
 */

/** The least time between two edits: at most 2 a second while dragging (design: "Live model"). */
export const EDIT_GAP_MS = 500;
const RETRY_MS = 2000;

/** Photo Wall's settings, as Central names them (central/displays/picture.py `SoftwareSetting`). */
export const SOFTWARE_SETTINGS = /** @type {const} */ ([
  "brightness", "contrast", "colour-temperature", "gamma", "saturation", "highlight-rolloff", "black-floor", "lut-amount",
]);

/** Central's refusals, in the operator's words; `stops`: the session waits for the operator. */
const REFUSALS = {
  frame_unbound: "Choose which Pi and HDMI output feed this Frame first (Hardware tab).",
  no_display: "The Pi has not reported a display on this HDMI port yet, so its picture waits until it does.",
  session_open: "Someone else is adjusting this display's picture. Photo Wall connects as soon as they finish.",
  stale_sequence: "Another window changed this display's picture at the same moment.",
  not_offered: "This display does not offer that setting, so Photo Wall cannot set it.",
  out_of_range: "Photo Wall refused this value.",
  not_presented_yet: "The Pi has not shown the latest change yet. Wait a moment, then press Done again.",
  unknown_lut: "That LUT is no longer on the LUTs list, so Photo Wall cannot use it.",
  unreachable: "Photo Wall's server did not answer.",
};
// Begin refusals that leave the tab without a session until something changes on the Pi's side.
const UNAVAILABLE = new Set(["frame_unbound", "no_display", "unknown_frame"]);
// The session is gone: begin again (with the draft).
const GONE = new Set(["session_ended", "unknown_session"]);
// Refused values the draft drops (the slider goes back to what Central has).
const DROPPED = new Set(["out_of_range", "not_offered", "unknown_lut"]);

// The pages left with changes not kept, by Frame, and when (this browser's clock, epoch seconds,
// only ever shown): the next visit says the display went back then.
const leftUnsaved = new Map();
// The Revert this browser last sent for each Frame's session, until it is answered: a begin for
// the same Frame waits for it.
const ending = new Map();

/** The field of SoftwarePictureView each Photo Wall setting is read from. */
const SOFTWARE_FIELDS = {
  brightness: "brightness_percent",
  contrast: "contrast_percent",
  "colour-temperature": "colour_temperature_kelvin",
  gamma: "gamma_tenths",
  saturation: "saturation_percent",
  "highlight-rolloff": "highlight_rolloff_percent",
  "black-floor": "black_floor_tenths",
  "lut-amount": "lut_amount_percent",
};

/** A Photo Wall setting's value as a read shows it: a percent, kelvin or tenths. */
export function softwareValue(view, setting) {
  return view.software[SOFTWARE_FIELDS[setting]];
}

/** The LUT a read shows in force: its paper profile's digest, or null for none. */
export function lutValue(view) {
  return view.software.lut?.profile_sha256 ?? null;
}

/** The test pattern a read shows asked for (whatever its state); null: none. */
export function patternValue(view) {
  return view.test_pattern?.pattern ?? null;
}

/** A display setting's value as a read shows it; null: as the display has it. */
export function hardwareValue(view, code) {
  return view.hardware.find((row) => row.code === code)?.value ?? null;
}

/**
 * @typedef {import("./pictureApi.js").FramePictureView} FramePictureView
 * @typedef {import("./pictureApi.js").SoftwareSetting} SoftwareSetting
 * @typedef {import("./pictureApi.js").VcpCode} VcpCode
 * @typedef {import("./pictureApi.js").SignalRange} SignalRange
 * @typedef {import("./pictureApi.js").TestPattern} TestPattern
 * @typedef {{hardware: Record<VcpCode, number|null>, software: Partial<Record<SoftwareSetting, number>>,
 *            lut: {lut?: string|null}, signal: {range?: SignalRange}, pattern: {pattern?: TestPattern|null}}} Draft
 * @typedef {{profile_sha256: string, name: string}} LutChoice   a LUT the picker offers
 * @typedef {"idle"|"live"|"waiting"|"shown"} Phase
 *   `idle`: no session (not open, no display, or someone else's session); `live`: a session and
 *   no change to judge; `waiting`: the Pi has not shown the latest change; `shown`: it has.
 * @typedef {{words: string, code: string}} Problem   `code` is Central's, for Details only.
 */

const EMPTY = /** @type {Draft} */ ({ hardware: {}, software: {}, lut: {}, signal: {}, pattern: {} });
// The draft's parts in the order they are sent: the choices first, each at once (`CHOICES`), then
// the paced ones.
const PARTS = /** @type {const} */ (["signal", "pattern", "hardware", "software", "lut"]);
const CHOICES = new Set(["signal", "pattern"]);
// The parts Done keeps: every one but the test pattern.
const KEPT = PARTS.filter((part) => part !== "pattern");
const isEmpty = (draft) => KEPT.every((part) => Object.keys(draft[part]).length === 0);

/**
 * The draft's next request against what this session already took; null when it has taken it all.
 * @returns {{kind: "signal"|"pattern"|"hardware"|"software"|"lut", key: string, value: number|string|null}|null}
 */
function nextWrite(draft, taken) {
  for (const kind of PARTS) {
    for (const [key, value] of Object.entries(draft[kind])) {
      if (!(key in taken[kind]) || taken[kind][key] !== value) return { kind, key, value };
    }
  }
  return null;
}

/**
 * The draft after a Restore, from the first read that follows it: every Photo Wall value, the LUT and
 * the signal range as the session holds them, each display setting set there or in `before` (one
 * handed back reads null), and `before`'s test pattern (never restored).
 * @param {FramePictureView} view
 * @param {Draft} before
 * @returns {Draft}
 */
export function restoredDraft(view, before) {
  const hardware = Object.fromEntries(view.hardware
    .filter((row) => row.value !== null || row.code in before.hardware)
    .map((row) => [row.code, row.value]));
  for (const code of Object.keys(before.hardware)) if (!(code in hardware)) hardware[code] = null;
  const software = Object.fromEntries(SOFTWARE_SETTINGS.map((setting) => [setting, softwareValue(view, setting)]));
  return { hardware, software, lut: { lut: lutValue(view) }, signal: { range: view.signal_range }, pattern: before.pattern };
}

/** Whether a read carries every part of the draft. */
function carries(view, draft) {
  return Object.entries(draft.hardware).every(([code, value]) => hardwareValue(view, code) === value)
    && Object.entries(draft.software).every(([setting, value]) => softwareValue(view, setting) === value)
    && (!("lut" in draft.lut) || lutValue(view) === draft.lut.lut)
    && (!("range" in draft.signal) || view.signal_range === draft.signal.range)
    && (!("pattern" in draft.pattern) || patternValue(view) === draft.pattern.pattern);
}

/**
 * @param {{frameId: string, open: boolean}} props
 * @returns {{view: FramePictureView|null, phase: Phase, draft: Draft, dirty: boolean,
 *            changeHardware: (code: VcpCode, value: number|null) => void,
 *            changeSoftware: (setting: SoftwareSetting, value: number) => void,
 *            changeLut: (profileSha256: string|null) => void,
 *            changeSignalRange: (range: SignalRange) => void, changeTestPattern: (pattern: TestPattern|null) => void,
 *            luts: LutChoice[]|null,
 *            done: () => Promise<boolean>, revert: () => Promise<void>,
 *            restore: (entryId: string) => Promise<{ok: true, result: import("./pictureApi.js").PictureRestoreResult}|{ok: false, error: string}>,
 *            busy: boolean,
 *            error: Problem|null, unavailable: Problem|null, waitingFor: string|null,
 *            latest: number|null, savedAt: number|null, leftAt: number|null}}
 *   `unavailable`: why no session can begin (no Pi, no display); `waitingFor`: someone else's
 *   session, in words; `latest` counts this session's changes, for the bar (null: no session);
 *   `luts` the LUTs list (null until read).
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
  const [luts, setLuts] = useState(/** @type {LutChoice[]|null} */ (null));
  // Read by the loop between renders. `taken`: what this session has accepted; `wrote` counts
  // accepted writes and `readFor` is the count a read began after, so a read older than the last
  // write never says shown. `flight` is the request in flight; `acting` holds the loop.
  const state = useRef({
    draft: EMPTY, session: null, taken: EMPTY, view: null, wrote: 0, readFor: -1, editAt: 0, waitUntil: 0,
    flight: null, acting: false, live, mounted: true, timer: null, restored: false,
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
    if (next === null) {
      setTaken(EMPTY);
      state.current.restored = false;
    }
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
    current.view = result.view;
    setView(result.view);
    if (current.restored && current.session !== null) {
      // The first read after a Restore: the draft becomes what the session holds, all of it taken.
      current.restored = false;
      const next = restoredDraft(result.view, current.draft);
      setDraft(next);
      setTaken({ ...next, pattern: current.taken.pattern });
    }
    if (after === current.wrote) {
      current.readFor = after;
      setShownRead(result.view.status === "shown" && carries(result.view, current.draft));
    }
  }, [frameId, fail, setDraft, setTaken]);

  /** One write of the draft (a display setting, a Photo Wall one, the signal range or the test pattern). */
  const write = useCallback(async (next) => {
    const current = state.current;
    const open_ = current.session;
    const sequence = { expected_sequence: open_.sequence };
    const expected = { ...sequence, value: next.value };
    const sent = {
      hardware: () => editHardware(open_.session_id, { ...expected, code: next.key }),
      software: () => editSoftware(open_.session_id, { ...expected, setting: next.key }),
      lut: () => editLut(open_.session_id, { ...sequence, profile_sha256: next.value }),
      signal: () => editSignalRange(open_.session_id, { ...sequence, signal_range: next.value }),
      pattern: () => editTestPattern(open_.session_id, { ...sequence, test_pattern: next.value }),
    }[next.kind];
    const result = await sent();
    if (!CHOICES.has(next.kind)) current.editAt = performance.now() + EDIT_GAP_MS;
    if (!current.mounted || current.session !== open_) return;
    const part = next.kind;
    if (result.ok) {
      setTaken({ ...current.taken, [part]: { ...current.taken[part], [next.key]: next.value } });
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
      const { [next.key]: _dropped, ...rest } = current.draft[part];
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
        const wait = CHOICES.has(next.kind) ? 0 : current.editAt - performance.now();
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

  // The LUTs list, read once each time the tab is opened.
  useEffect(() => {
    if (!open) return undefined;
    let current = true;
    void listLuts().then((result) => {
      if (current && result.ok) setLuts(result.luts.map(({ profile_sha256, name }) => ({ profile_sha256, name })));
    });
    return () => { current = false; };
  }, [open]);

  // Leaving the tab, hiding the browser tab or leaving the page ends the session now (Revert);
  // hidden with a test pattern taken, it is left to Central's idle window (THE SESSION RUNS above).
  const patternTaken = (taken.pattern.pattern ?? null) !== null;
  useEffect(() => {
    if (live || (open && patternTaken)) return;
    const current = state.current;
    clearTimeout(current.timer);
    if (current.session !== null) {
      void end(current.session);
      setSession(null);
    }
    setWaitingFor(null);
  }, [live, open, patternTaken, end, setSession]);
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

  /** One change to the draft's `part`; the loop sends it when the edit gap allows (a choice at once). */
  const change = useCallback((part, key, value) => {
    const current = state.current;
    if (key in current.draft[part] && current.draft[part][key] === value) return;
    setDraft({ ...current.draft, [part]: { ...current.draft[part], [key]: value } });
    setShownRead(false);
    setChanges((count) => count + 1);
    clearTimeout(current.timer);
    const wait = CHOICES.has(part) ? 0 : Math.max(0, current.editAt - performance.now());
    current.timer = setTimeout(step, wait + 5);
  }, [setDraft, step]);
  const changeHardware = useCallback((code, value) => change("hardware", code, value), [change]);
  const changeSoftware = useCallback((setting, value) => change("software", setting, value), [change]);
  const changeLut = useCallback((profileSha256) => change("lut", "lut", profileSha256), [change]);
  const changeSignalRange = useCallback((range) => change("signal", "range", range), [change]);
  /** A test pattern, or null for none. Choosing the one the Pi took off at its bound (`ended`) asks
   * for it again: a new edit, which restarts the Pi's bound. */
  const changeTestPattern = useCallback((pattern) => {
    const current = state.current;
    const ended = pattern !== null && current.view?.test_pattern?.pattern === pattern
      && current.view.test_pattern.state === "ended";
    if (ended && current.draft.pattern.pattern === pattern) {
      const { pattern: _asked, ...rest } = current.taken.pattern;
      setTaken({ ...current.taken, pattern: rest });
      setShownRead(false);
      setChanges((count) => count + 1);
      clearTimeout(current.timer);
      current.timer = setTimeout(step, 5);
      return;
    }
    change("pattern", "pattern", pattern);
  }, [change, setTaken, step]);

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
  /**
   * Restore a picture history entry into the session's draft (one edit; see RESTORE above).
   * @param {string} entryId
   * @returns {Promise<{ok: true, result: import("./pictureApi.js").PictureRestoreResult}|{ok: false, error: string}>}
   */
  const restore = useCallback(async (entryId) => (await act(async () => {
    const current = state.current;
    let open_ = current.session;
    if (open_ === null) return { ok: false, error: "session_ended" };
    let result = await restorePicture(open_.session_id, { entry_id: entryId, expected_sequence: open_.sequence });
    if (!result.ok && result.error === "stale_sequence") {
      const kept = await keepPictureAlive(open_.session_id);
      if (kept.ok && current.session === open_) {
        open_ = { ...open_, sequence: kept.session.sequence };
        setSession(open_);
        result = await restorePicture(open_.session_id, { entry_id: entryId, expected_sequence: open_.sequence });
      }
    }
    if (!current.mounted || current.session !== open_) return result;
    if (!result.ok) {
      if (GONE.has(result.error)) setSession(null);
      return result;
    }
    // Nothing of the old draft may be sent over the restored values: the draft holds only the test
    // pattern until the read below (or the loop's next one) folds the session's values in.
    const kept = { ...EMPTY, pattern: current.draft.pattern };
    setDraft(kept);
    setTaken({ ...EMPTY, pattern: current.taken.pattern });
    current.restored = true;
    current.wrote += 1;
    setShownRead(false);
    setSession({ ...open_, sequence: result.result.sequence });
    setChanges((count) => count + 1);
    setError(null);
    await read();
    return result;
  })) ?? { ok: false, error: "busy" }, [act, read, setDraft, setSession, setTaken]);
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
    view, phase, draft, dirty, changeHardware, changeSoftware, changeLut, changeSignalRange, changeTestPattern, luts, done,
    revert, restore, busy, error, unavailable, waitingFor,
    latest: session === null ? null : changes, savedAt, leftAt,
  };
}
