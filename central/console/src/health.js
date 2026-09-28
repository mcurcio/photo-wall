import { boundOutput } from "./join.js";

/**
 * Wall health: the ONE classifier (console pass 2, slice 1 — design
 * docs/operator-console-ux-pass2.md §4). Every surface that says whether a frame
 * or Player is alright — the plan tile, the Inspector header, Commissioning,
 * Binding, the Equipment rail, the Unplaced tray, the Showrunner frame list and
 * the attention strip — reads it through here, so the states, their precedence
 * and their wording live in exactly one place.
 *
 * LIVENESS is Central's record of the last readiness report it ACCEPTED from a
 * Player on that Player's current authority epoch (`last_report_at`). Enrollment
 * is not a report (`last_seen` is enrollment time). Every age is taken from the
 * inventory's `read_at` — Central's clock, frozen when the snapshot was read — so
 * no browser clock is involved and nothing ticks between reads. The silence
 * threshold is served by Central (`silent_after_seconds`, contracts/liveness.py),
 * never hard-coded here.
 *
 * Honesty rules: a label states a fact and its age, nothing more; `ok` never
 * implies playback (R2); nothing here says "LIVE", "online" or "connected".
 * Comparisons are written `!(age <= limit)` so a missing threshold or read time
 * fails CLOSED (reads as overdue), never open.
 */

/**
 * @typedef {"ok"|"todo"|"alarm"} Severity
 * @typedef {"unbound"|"awaiting-report"|"player-silent"|"display-not-detected"|
 *           "needs-commissioning"|"ok"} FrameState
 * @typedef {"commissioning"|"binding"|"nowshowing"} Facet
 * @typedef {{state: FrameState, severity: Severity, label: string,
 *            facet: Facet|null}} FrameHealth
 * @typedef {{state: "heard"|"silent"|"awaiting-report", age: number,
 *            overdue: boolean, label: string}} Liveness
 */

/** A frame is bound when both halves of its compound binding key are set. */
export function isBound(frame) {
  return frame != null && frame.player_id != null && frame.output_id != null;
}

/** "N s" / "N min" / "N h" / "N d" for a non-negative age in seconds. */
function formatAge(seconds) {
  const whole = Math.floor(seconds);
  if (whole < 60) {
    return `${whole} s`;
  }
  if (whole < 3600) {
    return `${Math.floor(whole / 60)} min`;
  }
  if (whole < 86400) {
    return `${Math.floor(whole / 3600)} h`;
  }
  return `${Math.floor(whole / 86400)} d`;
}

/**
 * What Central last heard from a Player, aged against the snapshot's `read_at`.
 *
 *  - "awaiting-report": enrolled, but no report accepted on its current epoch;
 *    `age` is the time since enrollment and `overdue` is set past the threshold.
 *  - "silent": the last accepted report is older than the threshold.
 *  - "heard": the last accepted report is within the threshold.
 *
 * @param {object|null} snapshot
 * @param {string} playerId
 * @returns {Liveness|null} null when the Player is not in the inventory
 */
export function playerLiveness(snapshot, playerId) {
  const inventory = snapshot?.inventory;
  const player = (inventory?.players ?? []).find((candidate) => candidate.id === playerId);
  if (!player) {
    return null;
  }
  const limit = inventory.silent_after_seconds;
  if (player.last_report_at == null) {
    const age = inventory.read_at - player.last_seen;
    return {
      state: "awaiting-report",
      age,
      overdue: !(age <= limit),
      label: `Enrolled ${formatAge(age)} ago, no report yet`,
    };
  }
  const age = inventory.read_at - player.last_report_at;
  if (!(age <= limit)) {
    return {
      state: "silent",
      age,
      overdue: true,
      label: `Player silent · last heard ${formatAge(age)} ago`,
    };
  }
  return { state: "heard", age, overdue: false, label: `Last heard ${formatAge(age)} ago` };
}

/**
 * The health of one frame: the first matching state, in design §4 order.
 * Rows 1–3 make the facts below them stale; a physical cause (no display)
 * precedes a configuration to-do (commissioning). Only alarms are alarms.
 *
 * @param {object|null} snapshot
 * @param {string} frameId
 * @returns {FrameHealth|null} null when the frame is not in the inventory
 */
export function frameHealth(snapshot, frameId) {
  const frame = (snapshot?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
  if (!frame) {
    return null;
  }
  if (!isBound(frame)) {
    return { state: "unbound", severity: "todo", label: "Needs a Player", facet: "binding" };
  }
  const liveness = playerLiveness(snapshot, frame.player_id);
  if (liveness === null || liveness.state === "awaiting-report") {
    // A bound Player missing from the inventory is unreachable (the binding's
    // foreign key); it is classified as the worst case of this row.
    return {
      state: "awaiting-report",
      severity: liveness === null || liveness.overdue ? "alarm" : "todo",
      label: liveness?.label ?? "No report from the Player yet",
      facet: "binding",
    };
  }
  if (liveness.state === "silent") {
    return { state: "player-silent", severity: "alarm", label: liveness.label, facet: "binding" };
  }
  // Fail closed: a missing output row reads as no display detected.
  if (boundOutput(snapshot, frameId)?.observation?.connected !== true) {
    return {
      state: "display-not-detected",
      severity: "alarm",
      label: "No display detected when the Player started",
      facet: "commissioning",
    };
  }
  if (frame.calibration_valid !== true) {
    return {
      state: "needs-commissioning",
      severity: "todo",
      label: "Needs commissioning",
      facet: "commissioning",
    };
  }
  return { state: "ok", severity: "ok", label: liveness.label, facet: null };
}

/**
 * The Inspector facet that shows the cause of a frame's health; `ok` keeps the
 * operator's current facet (no reset).
 *
 * @param {FrameHealth|null} health
 * @param {Facet} currentFacet
 * @returns {Facet}
 */
export function facetFor(health, currentFacet) {
  return health?.facet ?? currentFacet;
}

/**
 * Every frame that needs attention, alarms first then to-dos, each group in
 * frame-id order.
 *
 * @param {object|null} snapshot
 * @returns {{frameCount: number,
 *            alarms: Array<{frame: object, health: FrameHealth}>,
 *            todos: Array<{frame: object, health: FrameHealth}>}}
 */
export function wallAttention(snapshot) {
  const frames = [...(snapshot?.inventory?.frames ?? [])].sort((a, b) =>
    a.id < b.id ? -1 : a.id > b.id ? 1 : 0,
  );
  const alarms = [];
  const todos = [];
  for (const frame of frames) {
    const health = frameHealth(snapshot, frame.id);
    if (health.severity === "alarm") {
      alarms.push({ frame, health });
    } else if (health.severity === "todo") {
      todos.push({ frame, health });
    }
  }
  return { frameCount: frames.length, alarms, todos };
}
