import { fact, factText, LAYER_NAMES } from "./facts.js";
import { boundOutput, frameForOutput, isBound, liveRunsFor } from "./join.js";

// The one definition lives with the bound-output join; consumers read it through here.
export { isBound };

/**
 * Wall health: the ONE classifier (console pass 2, slice 1 — design
 * docs/operator-console-ux-pass2.md §4). Every surface that says whether a frame
 * or Player is alright — the plan tile, the Inspector header, Calibration,
 * Binding, the Players pages, the Unplaced tray, the Showrunner frame list and
 * the attention strip — reads it through here, so the states, their precedence
 * and their wording live in exactly one place. Equipment standing (slice 2,
 * docs/operator-console-ux-pass2-onboarding.md §4; standing words per
 * docs/operator-console-ddd.md §3) lives here too: a Player's standing, each
 * Output's state, the bindable set, and the one Output wording.
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
 * @typedef {"unbound"|"awaiting-report"|"player-silent"|"output-interrupted"|
 *           "no-panel-at-enrollment"|"needs-calibration"|"ok"} FrameState
 * @typedef {"calibration"|"binding"|"nowshowing"} Facet
 * @typedef {"liveness"|"binding"|"output"|"panel"|"calibration"} Cause
 * @typedef {{state: FrameState, severity: Severity, cause: Cause|null,
 *            label: string, tileLabel: string, settling: boolean,
 *            facet: Facet|null}} FrameHealth
 * @typedef {{state: "heard"|"silent"|"awaiting-report", age: number,
 *            overdue: boolean, settling: boolean, label: string}} Liveness
 *
 * `cause` groups states by what must change: "liveness" (awaiting-report and
 * player-silent — the Player's reports are not reaching Central), "binding"
 * (unbound), "output" (output-interrupted — a node layer reported the bound Output
 * lost while its Run continues), "panel" (no-panel-at-enrollment), "calibration"
 * (needs-calibration); null when ok. `label` is the full fact with its age;
 * `tileLabel` is the same fact without the age, short enough for a plan tile.
 * `settling` marks an awaiting-report frame enrolled within the grace (two
 * report intervals): it is still never ok, but the attention strip does not
 * count the moment between a Player's (re)enrollment and its first report.
 */

// Enrolled this many report intervals ago or less, a Player is still settling.
const SETTLING_INTERVALS = 2;

/**
 * "N s" / "N min" / "N h" / "N d" for a non-negative age in seconds: the one age
 * formatting every surface uses (the Showrunner's Runs and Programs included).
 */
export function formatAge(seconds) {
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

/** "4.1" of "4.1 of 8 GB": decimal gigabytes, one place (the media cache, a release download). */
export function gigabytes(bytes) {
  return `${Number((Number(bytes ?? 0) / 1e9).toFixed(1))}`;
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
export function ageAt(readAt, timestamp) {
  return readAt == null || timestamp == null ? NaN : readAt - timestamp;
}

/**
 * The health of one frame: the first matching state, in design §4 order.
 * Rows 1–3 make the facts below them stale; an Output interruption Central has
 * linked to the current Binding (console DDD §15) follows them; a physical cause (no Panel
 * listed at enrollment) precedes a configuration to-do (calibration). Only alarms are alarms.
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
  const interruption = interruptionFor(snapshot, frameId);
  if (interruption !== null) {
    const tile = interruption.suffix === null ? "Output interrupted"
      : `Output interrupted · ${interruption.suffix}`;
    return healthOf("output-interrupted", "alarm", "output", interruption.label, tile, "binding");
  }
  // Fail closed: a missing output row reads as no Panel listed. It stays an alarm (console
  // DDD §19): the enrollment record is the Wall's only signal for an unplugged Panel. Its
  // facet is Binding, where the Panel at enrollment is shown.
  if (!displayDetected(boundOutput(snapshot, frameId))) {
    return healthOf(
      "no-panel-at-enrollment",
      "alarm",
      "panel",
      NO_PANEL_AT_ENROLLMENT,
      NO_PANEL_LISTED,
      "binding",
    );
  }
  if (frame.calibration_valid !== true) {
    return healthOf(
      "needs-calibration",
      "todo",
      "calibration",
      "Needs calibration",
      "Needs calibration",
      "calibration",
    );
  }
  return healthOf("ok", "ok", null, liveness.label, "Heard recently", null);
}

/**
 * The Panel record at the Player app's last enrollment when no Panel is listed as connected
 * (console DDD §19): the one wording players.js `panelAtEnrollment` and the Frame-health
 * alarm both use. `connected=false` is Central's own record, so it may be stale.
 */
export const NO_PANEL_AT_ENROLLMENT =
  "No Panel listed as connected at the Player app's last enrollment (may be stale)";

/** The short form of {@link NO_PANEL_AT_ENROLLMENT}: a plan tile and an Output's state. */
export const NO_PANEL_LISTED = "No Panel listed at the last enrollment";

/**
 * What an Output-interrupted wording ends with when a live Run targets the Frame (R6: the
 * Run is not cancelled). With no live Run it is omitted: the console never claims a Run.
 */
export const RUN_CONTINUES = "the Run continues";

/**
 * What each layer actually reported when Central records an Output loss from it
 * (node_runtime_reconciliation.py; the owner of each fact kind is fixed in
 * contracts/node_protocol.py `owners`): the App Effect Broker reports an app process
 * exit, which Central applies to every Output linked to that process; Display Host
 * reports one Output's app surface invalidated or withdrawn. Any other served layer gets
 * the neutral wording, never a report it did not make. Each entry follows the layer's name
 * from LAYER_NAMES, read at call time (facts.js and this module import each other).
 */
const LOSS_REPORTS = Object.freeze({
  app_effect_broker: "reported the app process exited",
  display_host: "reported the app surface invalidated or withdrawn",
});

/**
 * The Output interruption of a Frame, from Central's interruption read
 * (`snapshot.outputInterruptions`, the snapshot's `output_interruptions`; design §15-§16):
 * the one function the Frame-health state and the Player page's Output row both read.
 *
 * Central serves only unresolved losses that fence the Frame's CURRENT Binding, so a row
 * is matched by Frame id alone and never re-matched to a Binding here. The fact is
 * `derived` (Central's inference from its linked Output-loss record), its basis what the
 * cause layer actually reported (LOSS_REPORTS), its age Central's read time minus
 * Central's record time (R10). With no row this returns null: Central records only the
 * losses it could link, so absence is never worded as "not interrupted".
 *
 * @param {object|null} snapshot
 * @param {string} frameId
 * @returns {{fact: import("./facts.js").Fact, suffix: string|null, label: string}|null}
 *   `suffix` is RUN_CONTINUES when a live Run targets the Frame, else null; `label` is the
 *   full wording: the fact, then " · " and the suffix when there is one
 */
export function interruptionFor(snapshot, frameId) {
  const row = (snapshot?.outputInterruptions ?? []).find((entry) => entry?.frame_id === frameId);
  if (row === undefined) {
    return null;
  }
  const layer = LAYER_NAMES[row.cause_layer] ?? "An unnamed layer";
  const report = `${layer} ${LOSS_REPORTS[row.cause_layer] ?? "sent the evidence Central linked to this Output"}`;
  const age = ageAt(snapshot?.readAt, row.interrupted_at);
  const recorded = Number.isNaN(age) ? "" : ` · recorded ${formatAge(Math.max(0, age))} ago`;
  const value = fact({ kind: "derived", value: "Output interrupted", basis: `${report}${recorded}` });
  const suffix = liveRunsFor(snapshot?.runtime, frameId).length > 0 ? RUN_CONTINUES : null;
  return { fact: value, suffix, label: suffix === null ? factText(value) : `${factText(value)} · ${suffix}` };
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
 * needs no attention yet and is left out, but counted in `awaiting` so the
 * all-clear never claims a report that has not arrived.
 *
 * @param {object|null} snapshot
 * @returns {{frameCount: number, awaiting: number,
 *            alarms: Array<{frame: object, health: FrameHealth}>,
 *            todos: Array<{frame: object, health: FrameHealth}>}}
 */
export function wallAttention(snapshot) {
  const frames = [...(snapshot?.inventory?.frames ?? [])].sort((a, b) =>
    a.id < b.id ? -1 : a.id > b.id ? 1 : 0,
  );
  const alarms = [];
  const todos = [];
  let awaiting = 0;
  for (const frame of frames) {
    const health = frameHealth(snapshot, frame.id);
    if (health.severity === "alarm") {
      alarms.push({ frame, health });
    } else if (health.severity === "todo" && health.settling) {
      awaiting += 1;
    } else if (health.severity === "todo") {
      todos.push({ frame, health });
    }
  }
  return { frameCount: frames.length, awaiting, alarms, todos };
}

// --- Equipment standing (slice 2 §4).

/**
 * @typedef {"retired"|"unbound"|"bound"} PlayerState
 * @typedef {"retired"|"bound"|"no-display"|"free"} OutputState
 * @typedef {{state: PlayerState, label: string}} PlayerStanding
 * @typedef {{playerId: string, outputId: string, state: OutputState,
 *            frameId: string|null, label: string}} OutputStanding
 */

/**
 * Whether the Player app listed a Panel as connected on this Output at its last
 * enrollment (`observation.connected`). Fails closed: a missing row reads as no Panel.
 *
 * @param {object|null|undefined} output an OutputInventory row
 * @returns {boolean}
 */
export function displayDetected(output) {
  return output?.observation?.connected === true;
}

function findPlayer(snapshot, playerId) {
  return (snapshot?.inventory?.players ?? []).find((player) => player.id === playerId) ?? null;
}

/** Players by registration, then id: the one Player order every list uses. */
export function playersInOrder(snapshot) {
  return [...(snapshot?.inventory?.players ?? [])].sort(
    (a, b) =>
      a.registered_at - b.registered_at || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
}

/**
 * Each Output of a Player, ordered by output id, with its state (first match):
 * retired (its Player is retired), bound (a Frame is bound to it), no-display
 * (unbound, and no Panel listed at the Player app's last enrollment — a stale row
 * cannot be told from an empty connector), free.
 *
 * @param {object|null} snapshot
 * @param {string} playerId
 * @returns {OutputStanding[]}
 */
export function outputStates(snapshot, playerId) {
  const player = findPlayer(snapshot, playerId);
  const outputs = (snapshot?.inventory?.outputs ?? [])
    .filter((output) => output.player_id === playerId)
    .sort((a, b) => (a.output_id < b.output_id ? -1 : a.output_id > b.output_id ? 1 : 0));
  return outputs.map((output) => {
    const standing = { playerId, outputId: output.output_id, frameId: null };
    if (player?.retired_at != null) {
      return { ...standing, state: "retired", label: "Retired with its Player" };
    }
    const frame = frameForOutput(snapshot, playerId, output.output_id);
    if (frame !== null) {
      return { ...standing, state: "bound", frameId: frame.id, label: `Bound to Frame ${frame.id}` };
    }
    if (!displayDetected(output)) {
      return {
        ...standing,
        state: "no-display",
        label: NO_PANEL_LISTED,
      };
    }
    return { ...standing, state: "free", label: "Free" };
  });
}

/**
 * A Player's standing (first match): retired (`retired_at` set), unbound (no
 * Output bound — the store's pending queue), bound (at least one Output bound).
 * The label states the fact; liveness is read separately ({@link playerLiveness}).
 * A box seen only at boot has no Player and so no standing here; the Players
 * list calls it "Not enrolled" (players.js).
 *
 * @param {object|null} snapshot
 * @param {string} playerId
 * @returns {PlayerStanding|null} null when the Player is not in the inventory
 */
export function playerStanding(snapshot, playerId) {
  const player = findPlayer(snapshot, playerId);
  if (player === null) {
    return null;
  }
  const readAt = snapshot.inventory.read_at;
  if (player.retired_at != null) {
    const age = ageAt(readAt, player.retired_at);
    return {
      state: "retired",
      label: Number.isNaN(age) ? "Retired" : `Retired ${formatAge(age)} ago`,
    };
  }
  const outputs = outputStates(snapshot, playerId);
  if (!outputs.some((output) => output.state === "bound")) {
    const age = ageAt(readAt, player.registered_at);
    return {
      state: "unbound",
      label: Number.isNaN(age) ? "Unbound" : `Unbound · enrolled ${formatAge(age)} ago`,
    };
  }
  const free = outputs.filter((output) => output.state === "free").length;
  return {
    state: "bound",
    label: `Bound · ${free} of ${outputs.length} outputs free`,
  };
}

/**
 * Every Output an operator may bind: `free` Outputs only, Players in
 * registration order, Outputs by id. Bound, no-display and retired Outputs are
 * never offered.
 *
 * @param {object|null} snapshot
 * @returns {OutputStanding[]}
 */
export function bindableOutputs(snapshot) {
  return playersInOrder(snapshot).flatMap((player) =>
    outputStates(snapshot, player.id).filter((output) => output.state === "free"),
  );
}

// A handle is this many trailing characters of the serial (or the Player id).
const HANDLE_LENGTH = 6;

/**
 * A short handle for a Player: the last six characters of the last known
 * serial for its `device_id` (boot facts), or else of the Player id. Without a
 * serial it is a hash suffix and proves nothing physical; even with one, the
 * serial is the Player's claim. Callers capture it when a dialog opens.
 *
 * @param {object|null} snapshot
 * @param {{devices?: Map<string, object>}|null} bootFacts
 * @param {string} playerId
 * @returns {string}
 */
export function playerHandle(snapshot, bootFacts, playerId) {
  return (playerSerial(snapshot, bootFacts, playerId) ?? playerId).slice(-HANDLE_LENGTH);
}

/**
 * The last known serial for a Player, joined on `device_id` (never on the
 * Player id): null without a netboot record.
 *
 * @param {object|null} snapshot
 * @param {{devices?: Map<string, object>}|null} bootFacts
 * @param {string} playerId
 * @returns {string|null}
 */
export function playerSerial(snapshot, bootFacts, playerId) {
  const player = findPlayer(snapshot, playerId);
  return (player && bootFacts?.devices?.get(player.device_id)?.serial) || null;
}

export const BOOT_FACTS_UNAVAILABLE = "Boot records unavailable";

/**
 * The one Output wording (chooser, Player page and dialogs): handle · output id ·
 * state label.
 *
 * @param {object|null} snapshot
 * @param {{devices?: Map<string, object>}|null} bootFacts
 * @param {string} playerId
 * @param {string} outputId
 * @returns {string}
 */
export function outputLabel(snapshot, bootFacts, playerId, outputId) {
  const handle = playerHandle(snapshot, bootFacts, playerId);
  const standing = outputStates(snapshot, playerId).find((output) => output.outputId === outputId);
  return standing === undefined
    ? `${handle} · ${outputId}`
    : `${handle} · ${outputId} · ${standing.label}`;
}
