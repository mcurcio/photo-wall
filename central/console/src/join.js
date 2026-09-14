/**
 * Now-showing + connectivity joins (shared primitive #4).
 *
 * A pure module imported by Plan.jsx (tile chips) and, later, the Now-showing
 * facet and the precedence "why" panel. It carries the TWO load-bearing joins the
 * design pins down (design §1b, §6a):
 *
 *  1. The now-showing join is a VERIFIED STRING compare. `visible[].target` is the
 *     string `"frame:<id>"` (runtime.py:18,140; a serialized `RuntimeView` yields
 *     `"target":"frame:abc"`), NOT the object `Target {kind,id}` used only in the
 *     player protocol (contracts/models.py:19). A predecessor review asserted the
 *     object shape; that premise was checked and refuted — the object join returns
 *     empty on every tile. The correct join is `entry.target === "frame:" + frameId`.
 *  2. The connectivity join is on the COMPOUND key (player_id AND output_id).
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
 * Connectivity fact for a frame, from its bound output's observation.
 *
 * "unbound" when the frame has no binding (player_id/output_id null). Otherwise the
 * frame's OutputInventory is found on the COMPOUND key (player_id AND output_id) and
 * its `observation.connected` decides "connected" vs "disconnected". A bound frame
 * whose output report is missing is reported "disconnected" — there is no connected
 * observation to trust (a conservative, honesty-preserving default).
 *
 * @param {{inventory?: {frames?: Array<object>, outputs?: Array<object>}}} snapshot
 * @param {string} frameId
 * @returns {"connected"|"disconnected"|"unbound"}
 */
export function connectivity(snapshot, frameId) {
  const frames = snapshot?.inventory?.frames ?? [];
  const frame = frames.find((candidate) => candidate.id === frameId);
  if (!frame || frame.player_id == null || frame.output_id == null) {
    return "unbound";
  }
  const outputs = snapshot?.inventory?.outputs ?? [];
  const output = outputs.find(
    (candidate) =>
      candidate.player_id === frame.player_id && candidate.output_id === frame.output_id,
  );
  if (!output) {
    return "disconnected";
  }
  return output.observation?.connected ? "connected" : "disconnected";
}
