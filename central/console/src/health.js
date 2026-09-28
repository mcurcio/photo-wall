import { boundOutput, isBound } from "./join.js";

// The one definition lives with the bound-output join; consumers read it through here.
export { isBound };

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
 * never hard-coded here; so is the Player's report interval
 * (`report_interval_seconds`), from which the enrollment grace below is derived.
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
 * @typedef {"liveness"|"binding"|"display"|"commissioning"} Cause
 * @typedef {{state: FrameState, severity: Severity, cause: Cause|null,
 *            label: string, tileLabel: string, settling: boolean,
 *            facet: Facet|null}} FrameHealth
 * @typedef {{state: "heard"|"silent"|"awaiting-report", age: number,
 *            overdue: boolean, settling: boolean, label: string}} Liveness
 *
 * `cause` groups states by what must change: "liveness" (awaiting-report and
 * player-silent — the Player's reports are not reaching Central), "binding"
 * (unbound), "display" (display-not-detected), "commissioning"
 * (needs-commissioning); null when ok. `label` is the full fact with its age;
 * `tileLabel` is the same fact without the age, short enough for a plan tile.
 * `settling` marks an awaiting-report frame enrolled within the grace (two
 * report intervals): it is still never ok, but the attention strip does not
 * count the moment between a Player's (re)enrollment and its first report.
 */

// Enrolled this many report intervals ago or less, a Player is still settling.
const SETTLING_INTERVALS = 2;

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
    const age = ageAt(inventory.read_at, player.last_seen);
    return {
      state: "awaiting-report",
      age,
      overdue: !(age <= limit),
      settling: age <= SETTLING_INTERVALS * inventory.report_interval_seconds,
      label: Number.isNaN(age)
        ? "Enrolled, no report yet"
        : `Enrolled ${formatAge(age)} ago, no report yet`,
    };
  }
  const age = ageAt(inventory.read_at, player.last_report_at);
  if (!(age <= limit)) {
    return {
      state: "silent",
      age,
      overdue: true,
      settling: false,
      label: Number.isNaN(age)
        ? "Player silent"
        : `Player silent · last heard ${formatAge(age)} ago`,
    };
  }
  return {
    state: "heard",
    age,
    overdue: false,
    settling: false,
    label: `Last heard ${formatAge(age)} ago`,
  };
}

/**
 * Seconds from `timestamp` to `readAt`; NaN when either is missing, so every
 * comparison against it fails closed and no label prints an age.
 */
function ageAt(readAt, timestamp) {
  return readAt == null || timestamp == null ? NaN : readAt - timestamp;
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
    return healthOf("unbound", "todo", "binding", "Needs a Player", "Needs a Player", "binding");
  }
  const liveness = playerLiveness(snapshot, frame.player_id);
  if (liveness === null || liveness.state === "awaiting-report") {
    // A bound Player missing from the inventory is unreachable (the binding's
    // foreign key); it is classified as the worst case of this row.
    return {
      ...healthOf(
        "awaiting-report",
        liveness === null || liveness.overdue ? "alarm" : "todo",
        "liveness",
        liveness?.label ?? "No report from the Player yet",
        "No report yet",
        "binding",
      ),
      settling: liveness?.settling === true,
    };
  }
  if (liveness.state === "silent") {
    const { label } = liveness;
    return healthOf("player-silent", "alarm", "liveness", label, "Player silent", "binding");
  }
  // Fail closed: a missing output row reads as no display detected.
  if (boundOutput(snapshot, frameId)?.observation?.connected !== true) {
    return healthOf(
      "display-not-detected",
      "alarm",
      "display",
      "No display detected when the Player started",
      "No display detected",
      "commissioning",
    );
  }
  if (frame.calibration_valid !== true) {
    return healthOf(
      "needs-commissioning",
      "todo",
      "commissioning",
      "Needs commissioning",
      "Needs commissioning",
      "commissioning",
    );
  }
  return healthOf("ok", "ok", null, liveness.label, "Heard recently", null);
}

/** One FrameHealth; only an awaiting-report frame can be settling. */
function healthOf(state, severity, cause, label, tileLabel, facet) {
  return { state, severity, cause, label, tileLabel, settling: false, facet };
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
 * frame-id order. A settling frame (enrolled within the grace, no report yet)
 * needs no attention yet and is left out.
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
    } else if (health.severity === "todo" && !health.settling) {
      todos.push({ frame, health });
    }
  }
  return { frameCount: frames.length, alarms, todos };
}
