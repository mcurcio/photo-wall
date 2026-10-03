import { fact, plannedNothing } from "./facts.js";
import { runOrigin } from "./showState.js";

/**
 * Planned-intent + bound-output joins (shared primitive #4).
 *
 * A pure module imported by Plan.jsx (tile facts), the Frame's Status facet, the
 * precedence "why" panel and health.js. It carries the TWO load-bearing joins the
 * design pins down (design §1b, §6a), and `isBound`, the one definition of a bound
 * frame (health.js re-exports it for its consumers):
 *
 *  1. The planned-intent join is a VERIFIED STRING compare. `visible[].target` is the
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
 * The fact built from this is Central's Runtime projection (`planned`, console DDD §35),
 * never confirmed playback — there is no execution/render readback in
 * /inventory+/runtime, so the word "LIVE" must never appear (design §6a).
 */

const FRAME = "frame:";

/** The target string of a Frame, `"frame:<id>"`: the one home of the prefix. */
export function toTarget(frameId) {
  return FRAME + frameId;
}

/** The frame id of a `"frame:<id>"` target; null for any other target. */
export function frameOf(target) {
  return target.startsWith(FRAME) ? target.slice(FRAME.length) : null;
}

/**
 * The top Intent on a frame in Central's Runtime: the winning visible Intent whose target
 * is the string `"frame:<id>"`, or null when no visible Intent targets the frame (no Run,
 * a gap between cycles, or a child Scene before its delay).
 *
 * @param {{current?: {visible?: Array<{target: string, scene_id: string, phase: string,
 *          run_id: string, root_id: string}>}}} runtime
 *   the `/v1/operator/runtime` payload (snapshot.runtime)
 * @param {string} frameId
 * @returns {object|null} the served Intent
 */
export function plannedIntent(runtime, frameId) {
  const visible = runtime?.current?.visible ?? [];
  const target = toTarget(frameId);
  return visible.find((intent) => intent.target === target) ?? null;
}

const MEDIA_NOT_CHECKED = "media not checked";

/**
 * A root Run's origin inside a sentence that is already in parentheses (§35): the Run
 * cards' "started directly (Show now or the API)" reads "started directly, by Show now or
 * the API" here; a Program reads as `runOrigin` words it.
 */
function originPhrase(runtime, root) {
  const origin = runOrigin(runtime, root);
  return origin.kind === "direct" ? "started directly, by Show now or the API" : origin.words;
}
const UNBOUND = "this Frame is unbound, so Central sends it no layers";

/**
 * A frame's `planned` fact (console DDD §35): which Run is on top in Central's Runtime, who
 * started it and what was not checked. The origin is read from the top Intent's ROOT Run
 * (a child Run's `program_id` is null): a root Intent names its root's origin
 * (showState.js `runOrigin`), a child's names its root's Run and that Run's origin. A root
 * missing from the read leaves the origin unnamed, so the fact becomes `unknown`. The basis
 * is "media not checked" on a bound frame; an unbound frame gets no layers at all
 * (central/planner.py), and the basis says so.
 *
 * @param {object|null|undefined} runtime the served runtime (snapshot.runtime)
 * @param {string} frameId
 * @param {boolean} bound whether the frame is bound (`isBound`)
 * @returns {{fact: import("./facts.js").Fact, sceneId: string|null, phase: string|null}}
 */
export function plannedFor(runtime, frameId, bound) {
  const intent = plannedIntent(runtime, frameId);
  if (intent === null) {
    return { fact: plannedNothing(), sceneId: null, phase: null };
  }
  return { fact: plannedFact(runtime, intent, bound), sceneId: intent.scene_id, phase: intent.phase };
}

/**
 * Who started an Intent's Run, in words (§35), read from its ROOT Run: a root Intent names
 * its root's origin, a child's "part of <root scene>'s Run, <root's origin>". Null when the
 * root is missing from the read. The one home of the origin wording for the `planned` fact,
 * the Why heading and its rows.
 *
 * @param {object|null|undefined} runtime the served runtime (snapshot.runtime)
 * @param {{run_id: string, root_id: string}} intent a served Intent or contribution
 * @returns {string|null}
 */
export function intentOrigin(runtime, intent) {
  const root = (runtime?.current?.runs ?? []).find((run) => run.run_id === intent.root_id);
  if (root === undefined) return null;
  const rootOrigin = originPhrase(runtime, root);
  return intent.run_id === intent.root_id ? rootOrigin : `part of ${root.scene_id}'s Run, ${rootOrigin}`;
}

/**
 * The `planned` fact for one served Intent or contribution on a frame (§35): every site that
 * names the top Run says it through here, so none states Central's Runs as the Panel's output.
 *
 * @param {object|null|undefined} runtime the served runtime (snapshot.runtime)
 * @param {{scene_id: string, run_id: string, root_id: string}} intent
 * @param {boolean} bound whether the frame is bound (`isBound`)
 * @returns {import("./facts.js").Fact}
 */
export function plannedFact(runtime, intent, bound) {
  return fact({
    kind: "planned",
    value: intent.scene_id,
    origin: intentOrigin(runtime, intent),
    basis: bound ? MEDIA_NOT_CHECKED : UNBOUND,
  });
}

/**
 * The "why" for a frame: every `contribution` targeting it, ranked by the total
 * precedence order — the shared precedence read of primitive #4.
 *
 * The filter is the SAME verified string join as `plannedIntent`
 * (`intent.target === "frame:" + frameId`), NOT the object `{kind,id}` shape
 * (that targets only the player protocol and would match nothing here). The sort
 * is DESCENDING by the precedence tuple `(priority, root_order, admission_order)`,
 * matching the runtime's own winner rule — it keeps the MAX-precedence Intent as
 * the visible winner (`intent.precedence > winner.precedence`, runtime.py:708) —
 * so the winning contribution sits at the top. The order is total and
 * deterministic (design J4/§6a: no ties).
 *
 * This is the ONE copy of the precedence ranking: the Status facet's "why"
 * (Bead 3) and the Showrunner Runs "why" panel (Bead 16) both read through here,
 * so the ordering rule lives in exactly one place.
 *
 * @param {{current?: {contributions?: Array<{target: string, scene_id: string, phase: string, priority: number, root_order: number, admission_order: number, run_id: string, role: string|null}>}}} runtime
 *   the `/v1/operator/runtime` payload (snapshot.runtime)
 * @param {string} frameId
 * @returns {Array<object>} contributions for the frame, highest precedence first
 */
export function rankedContributions(runtime, frameId) {
  const target = toTarget(frameId);
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
 * Who wins a frame, in Central's Runs (pass 2 slice 3 §10; console DDD §34), read on
 * {@link rankedContributions}. The Runtime keeps the highest
 * `(priority, root_order, admission_order)`; each hidden entry is compared with
 * the winner on the first element that differs, and every sentence says
 * "priority N". The winner's origin is read from its ROOT Run (`program_id`),
 * never from the Intent. The limit line is always shown: this is Central's Runtime, not
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
    const origin = intentOrigin(runtime, intent);
    return origin === null ? `priority ${intent.priority}` : `priority ${intent.priority}, ${origin}`;
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
    heading: `Central's Runs on ${frameId}: ${winner.scene_id} (${tag(winner)}) on top.`,
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
 * Both health.js (the Panel at enrollment) and the Binding facet's Panel facts
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
  const target = toTarget(frameId);
  return (runtime?.current?.runs ?? []).filter(
    (run) => LIVE_PHASES.has(run.phase) && (run.participants ?? []).includes(target),
  );
}
