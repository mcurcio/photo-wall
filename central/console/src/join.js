/**
 * Now-showing + bound-output joins (shared primitive #4).
 *
 * A pure module imported by Plan.jsx (tile chips), the Now-showing facet, the
 * precedence "why" panel and health.js. It carries the TWO load-bearing joins the
 * design pins down (design §1b, §6a), and `isBound`, the one definition of a bound
 * frame (health.js re-exports it for its consumers):
 *
 *  1. The now-showing join is a VERIFIED STRING compare. `visible[].target` is the
 *     string `"frame:<id>"` (runtime.py:18,140; a serialized `RuntimeView` yields
 *     `"target":"frame:abc"`), NOT the object `Target {kind,id}` used only in the
 *     player protocol (contracts/models.py:19). A predecessor review asserted the
 *     object shape; that premise was checked and refuted — the object join returns
 *     empty on every tile. The correct join is `entry.target === "frame:" + frameId`.
 *  2. The bound-output join is on the COMPOUND key (player_id AND output_id).
 *     The outputs primary key is `(player_id, output_id)` (001_registry.sql:20) and
 *     `output_id` (e.g. "hdmi0") repeats across players, so joining on output_id
 *     alone resolves the wrong player's port.
 *
 * The chip built from this asserts operator INTENT ("Scheduled"), never confirmed
 * playback — there is no execution/render readback in /inventory+/runtime, so the
 * word "LIVE" must never appear (design §6a).
 */

/**
 * Intended now-showing for a frame: the winning visible Intent whose target is the
 * string `"frame:<id>"`. Returns the winner's `scene_id` + `phase`, or null when no
 * Scene currently targets the frame.
 *
 * @param {{current?: {visible?: Array<{target: string, scene_id: string, phase: string}>}}} runtime
 *   the `/v1/operator/runtime` payload (snapshot.runtime)
 * @param {string} frameId
 * @returns {{scene_id: string, phase: string}|null}
 */
export function nowShowing(runtime, frameId) {
  const visible = runtime?.current?.visible ?? [];
  const target = "frame:" + frameId;
  const entry = visible.find((intent) => intent.target === target);
  if (!entry) {
    return null;
  }
  return { scene_id: entry.scene_id, phase: entry.phase };
}

/**
 * The "why" for a frame: every `contribution` targeting it, ranked by the total
 * precedence order — the shared precedence read of primitive #4.
 *
 * The filter is the SAME verified string join as `nowShowing`
 * (`intent.target === "frame:" + frameId`), NOT the object `{kind,id}` shape
 * (that targets only the player protocol and would match nothing here). The sort
 * is DESCENDING by the precedence tuple `(priority, root_order, admission_order)`,
 * matching the runtime's own winner rule — it keeps the MAX-precedence Intent as
 * the visible winner (`intent.precedence > winner.precedence`, runtime.py:708) —
 * so the winning contribution sits at the top. The order is total and
 * deterministic (design J4/§6a: no ties).
 *
 * This is the ONE copy of the precedence ranking: the Now-showing facet's "why"
 * (Bead 3) and the Showrunner Runs "why" panel (Bead 16) both read through here,
 * so the ordering rule lives in exactly one place.
 *
 * @param {{current?: {contributions?: Array<{target: string, scene_id: string, phase: string, priority: number, root_order: number, admission_order: number, run_id: string, role: string|null}>}}} runtime
 *   the `/v1/operator/runtime` payload (snapshot.runtime)
 * @param {string} frameId
 * @returns {Array<object>} contributions for the frame, highest precedence first
 */
export function rankedContributions(runtime, frameId) {
  const target = "frame:" + frameId;
  const contributions = runtime?.current?.contributions ?? [];
  return contributions
    .filter((intent) => intent.target === target)
    .slice()
    .sort(
      (a, b) =>
        b.priority - a.priority ||
        b.root_order - a.root_order ||
        b.admission_order - a.admission_order,
    );
}

/**
 * Who wins a frame, in Central's plan (pass 2 slice 3 §10), read on
 * {@link rankedContributions}. The Runtime keeps the highest
 * `(priority, root_order, admission_order)`; each hidden entry is compared with
 * the winner on the first element that differs, and every sentence says
 * "priority N". The winner's origin is read from its ROOT Run (`program_id`),
 * never from the Intent. The limit line is always shown: this is the plan, not
 * what the panel shows (R2).
 *
 * @param {object} runtime the `/v1/operator/runtime` payload (snapshot.runtime)
 * @param {string} frameId
 * @returns {{heading: string, rows: Array<{intent: object, sentence: string}>,
 *            limit: string}|null} null when nothing targets the frame
 */
export function explainPrecedence(runtime, frameId) {
  const ranked = rankedContributions(runtime, frameId);
  if (ranked.length === 0) {
    return null;
  }
  const runs = new Map((runtime?.current?.runs ?? []).map((run) => [run.run_id, run]));
  const [winner] = ranked;
  const tag = (intent) => {
    const root = runs.get(intent.root_id);
    if (root === undefined) {
      return `priority ${intent.priority}`;
    }
    const origin = root.program_id != null ? `Program ${root.program_id}` : "activated directly";
    return `priority ${intent.priority}, ${origin}`;
  };
  const underneath = (intent) => {
    const lead = `${intent.scene_id} (priority ${intent.priority}) is underneath:`;
    if (intent.priority !== winner.priority) {
      return `${lead} ${winner.scene_id} has priority ${winner.priority}.`;
    }
    if (intent.root_order !== winner.root_order) {
      return (
        `${lead} same priority, and Central admitted ${winner.scene_id}'s Run later. ` +
        "Admission order, not the Program's start time; Programs starting at the same " +
        "instant are admitted in Program-id order."
      );
    }
    const root = runs.get(winner.root_id)?.scene_id ?? winner.scene_id;
    return `${lead} same Run of ${root}; the later child Scene is on top.`;
  };
  return {
    heading: `Central's plan for ${frameId}: ${winner.scene_id} (${tag(winner)}) on top.`,
    rows: ranked.map((intent, index) => ({
      intent,
      sentence: index === 0 ? `${intent.scene_id} (${tag(intent)}) is on top.` : underneath(intent),
    })),
    limit:
      `If ${winner.scene_id} has no usable media for this frame (none eligible, still ` +
      "preparing, or no compatible variant), Central plans the next layer down instead. " +
      "An unbound frame gets no layers at all. A partly transparent or fading layer shows " +
      "what is underneath.",
  };
}

/** A frame is bound when both halves of its compound binding key are set. */
export function isBound(frame) {
  return frame != null && frame.player_id != null && frame.output_id != null;
}

/**
 * The OutputInventory row serving a frame, resolved on the COMPOUND key
 * (player_id AND output_id). This is the ONE copy of the compound-key join rule
 * (outputs PK is `(player_id, output_id)`, 001_registry.sql:20; `output_id`
 * repeats across players so output_id alone resolves the wrong player's port).
 * Both health.js (display detection) and the Commissioning facet's Display facts
 * read the bound output through here, so the rule lives in exactly one place.
 *
 * Returns null when the frame is unknown, unbound (either id null), or has no
 * matching OutputInventory row.
 *
 * @param {{inventory?: {frames?: Array<object>, outputs?: Array<object>}}} snapshot
 * @param {string} frameId
 * @returns {object|null} the OutputInventory row (with `.observation`), or null
 */
export function boundOutput(snapshot, frameId) {
  const frames = snapshot?.inventory?.frames ?? [];
  const frame = frames.find((candidate) => candidate.id === frameId);
  if (!isBound(frame)) {
    return null;
  }
  const outputs = snapshot?.inventory?.outputs ?? [];
  return (
    outputs.find(
      (candidate) =>
        candidate.player_id === frame.player_id &&
        candidate.output_id === frame.output_id,
    ) ?? null
  );
}

/**
 * The Frame an Output serves: the reverse of {@link boundOutput}, on the same
 * COMPOUND key (player_id AND output_id). Null when no Frame is bound to it.
 *
 * @param {{inventory?: {frames?: Array<object>}}} snapshot
 * @param {string} playerId
 * @param {string} outputId
 * @returns {object|null} the FrameInventory row, or null
 */
export function frameForOutput(snapshot, playerId, outputId) {
  const frames = snapshot?.inventory?.frames ?? [];
  return (
    frames.find(
      (frame) => isBound(frame) && frame.player_id === playerId && frame.output_id === outputId,
    ) ?? null
  );
}

// A Run is live in these phases: the same predicate the delete guard uses
// (central/app.py `remove_frame`), so the console lists exactly the Runs that
// would refuse a delete. Every live/ended split in the console reads it here.
export const LIVE_PHASES = new Set(["body", "outro"]);

/**
 * The live Runs a Frame participates in (phase body or outro), in the runtime's
 * order. A Run's `participants` holds target strings (`"frame:<id>"`).
 *
 * @param {{current?: {runs?: Array<{run_id: string, scene_id: string, phase: string, participants: string[]}>}}} runtime
 * @param {string} frameId
 * @returns {Array<object>}
 */
export function liveRunsFor(runtime, frameId) {
  const target = "frame:" + frameId;
  return (runtime?.current?.runs ?? []).filter(
    (run) => LIVE_PHASES.has(run.phase) && (run.participants ?? []).includes(target),
  );
}
