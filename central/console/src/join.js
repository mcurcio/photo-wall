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
