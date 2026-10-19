import { apiWrite, callApi } from "./apiWrite.js";
import { dragToPlacement } from "./projection.js";
import { FRAME_ID_PATTERN } from "./frameIds.js";

export { FRAME_ID_PATTERN } from "./frameIds.js";

/**
 * Low-level Frame write module (bead R-apiwrite). Holds the four Frame mutations
 * that both {@link module:Plan} and {@link module:UnplacedTray} issue —
 * `createFrame`/`moveFrame`/`deleteFrame`/`dropFromTray` — so the tray no longer
 * imports them from its sibling `Plan.jsx`. Each write goes through the shared
 * {@link apiWrite} helper and keeps its own result shape and message table; wrap
 * the calls in `useMutate()` at the call site so the plan refreshes after a write.
 */

/**
 * The usable Frame id rule (slice 2 §8): letters, digits, `-`, `_` or `.`, at
 * most 96 characters, no `:` — the only ids a Scene can target. It mirrors
 * contracts/models.py `TARGET_ID_PATTERN` (which `FrameCreate.id` enforces); a
 * pytest pins the two equal, so there is one rule.
 */

/**
 * Normalize a create/move `apiWrite` result to the frame-write shape. On success
 * the parsed Frame row is returned as `frame`; on failure the server error code
 * (or the raw status) is surfaced for the caller's inline reason.
 *
 * @param {{ok: boolean, status: number, error: string|null, data: any}} result
 * @returns {{ok:true, frame:object}|{ok:false, error:string}}
 */
function interpretFrame(result) {
  if (result.ok) {
    return { ok: true, frame: result.data };
  }
  return { ok: false, error: result.error ?? String(result.status) };
}

/**
 * Create a Frame (Bead 10; slice 2 §8): POST /v1/operator/frames with the
 * operator's readable id, the drag placement and the Frame profile. The id
 * must match {@link FRAME_ID_PATTERN} (the caller checks it before sending) and
 * cannot be changed later; a taken id answers 409 `frame_exists`. The server
 * re-runs the orientation-coherence guard and 422s an incoherent profile. Wrap
 * in `useMutate()` at the call site so the plan refreshes.
 *
 * @param {string} id the operator-chosen Frame id
 * @param {{surface_id: string, x_mm: number, y_mm: number, width_mm: number, height_mm: number}} placement
 * @param {{width_px: number, height_px: number, diagonal_inches: number, video: boolean}} profile
 * @returns {Promise<{ok:true, frame:object}|{ok:false, error:string}>}
 */
export async function createFrame(id, placement, profile) {
  const result = await apiWrite("/v1/operator/frames", {
    method: "POST",
    body: { id, ...placement, profile },
  });
  return interpretFrame(result);
}

/**
 * Set how a Frame's screen hangs (render redesign S5): PUT /v1/operator/frames/{id}/turn with
 * `body` = contract.ts `FrameTurnChange` ({turn, expected_generation}, the generation the operator
 * saw). Central answers {frame_id, turn, generation, changed}; a turn already set is
 * `changed: false` with no write. Refusals: 409 `binding_generation_conflict` (the Frame changed
 * meanwhile), 404 `unknown_frame`, 422 for a body outside the contract. Wrap in `useMutate()` at
 * the call site so the inventory (the Frame's turn, generation and readiness) is read again.
 *
 * @param {string} frameId
 * @param {import("./generated/contract").FrameTurnChange} body
 * @returns {Promise<{ok:true, changed:boolean, generation:number}|{ok:false, code:string, status:number}>}
 */
export async function turnFrame(frameId, body) {
  const result = await apiWrite(`/v1/operator/frames/${frameId}/turn`, { method: "PUT", body });
  if (result.ok) {
    return { ok: true, changed: result.data?.changed === true, generation: result.data?.generation };
  }
  return { ok: false, code: result.error ?? String(result.status), status: result.status };
}

/**
 * Set whether a Frame shows videos (frame box run, fb-5): PUT /v1/operator/frames/{id}/video with
 * contract.ts `FrameVideoChange` ({video}). The Frame's own setting, last write wins: no
 * generation, and a ready Frame stays ready. Central answers {frame_id, video, changed}; the same
 * value is `changed: false`. Refusals: 404 `unknown_frame`, 422 for a body outside the contract.
 * Wrap in `useMutate()` at the call site so the inventory is read again.
 *
 * @param {string} frameId
 * @param {boolean} video
 * @returns {Promise<{ok:true, changed:boolean, video:boolean}|{ok:false, code:string, status:number}>}
 */
export async function setFrameVideo(frameId, video) {
  /** @type {import("./generated/contract").FrameVideoChange} */
  const body = { video };
  const result = await apiWrite(`/v1/operator/frames/${frameId}/video`, { method: "PUT", body });
  if (result.ok) {
    return { ok: true, changed: result.data?.changed === true, video: result.data?.video === true };
  }
  return { ok: false, code: result.error ?? String(result.status), status: result.status };
}

/**
 * Reposition a Frame (Bead 10): PATCH /v1/operator/frames/{id} with a partial
 * placement. Last-write-wins, NO concurrency token (design §9a — the one
 * deliberate exception to R3; placement is cosmetic and never reaches a player).
 * Wrap in `useMutate()` at the call site so the plan corrects from the next
 * snapshot.
 *
 * @param {string} frameId
 * @param {{surface_id?: string, x_mm?: number, y_mm?: number, width_mm?: number, height_mm?: number}} placement
 * @returns {Promise<{ok:true, frame:object}|{ok:false, error:string}>}
 */
export async function moveFrame(frameId, placement) {
  const result = await apiWrite(`/v1/operator/frames/${frameId}`, {
    method: "PATCH",
    body: placement,
  });
  return interpretFrame(result);
}

/**
 * Guard-code -> operator message for a refused DELETE (design §9a). The distinctive
 * wording is load-bearing: {@link deleteFrame} maps the server's 409 `error` code to
 * exactly these sentences so the operator is told the specific remedy (unbind, or
 * finish/cancel the Run), not a generic "could not delete." A test asserts the
 * distinctive substrings, so collapsing this map to a generic string is caught.
 */
const DELETE_MESSAGES = {
  frame_bound: "This Frame still has a bound Output — unbind it before deleting.",
  frame_in_use: "A live Run is scheduled on this Frame — finish or cancel it before deleting.",
};

/**
 * Delete a Frame (Bead 11, design §9a/J3): DELETE /v1/operator/frames/{id}. The
 * server refuses with 409 while the Frame is bound (`frame_bound`) or while a live
 * Run targets it (`frame_in_use`); those codes are mapped to the design's
 * plain-language operator messages so the guidance names the specific remedy. Any
 * other non-ok response falls back to a generic message. Wrap in `useMutate()` at
 * the call site so the plan refreshes once the delete lands.
 *
 * @param {string} frameId
 * @returns {Promise<{ok:true}|{ok:false, code:string, message:string,
 *   scene_ids:string[], program_ids:string[], queued_activation_ids:string[], run_ids:string[]}>}
 */
export async function deleteFrame(frameId) {
  const result = await apiWrite(`/v1/operator/frames/${frameId}`, {
    method: "DELETE",
  });
  if (result.ok) {
    return { ok: true };
  }
  const code = result.error ?? String(result.status);
  const ids = (key) => Array.isArray(result.data?.[key])
    ? result.data[key].filter((id) => typeof id === "string") : [];
  return {
    ok: false,
    code,
    message: DELETE_MESSAGES[code] ?? "Could not delete the frame.",
    scene_ids: ids("scene_ids"),
    program_ids: ids("program_ids"),
    queued_activation_ids: ids("queued_activation_ids"),
    run_ids: ids("run_ids"),
  };
}

/**
 * Drop an Unplaced-tray Frame onto the plan (Bead 11, design J3/§12): PATCH the
 * frame with a distinct mm ORIGIN inverted from the drop point, reusing
 * {@link dragToPlacement} for the px->mm inversion and {@link moveFrame} for the
 * write. Only `surface_id`/`x_mm`/`y_mm` are sent — the PATCH is partial, so the
 * stored `width_mm`/`height_mm` (and thus orientation coherence with the profile)
 * are preserved; the drop merely gives the frame a non-origin position so
 * `isUnplaced` no longer routes it to the tray. Wrap in `useMutate()` so the frame
 * leaves the tray and renders on the plan from the next snapshot.
 *
 * @param {string} frameId
 * @param {import("./projection.js").Rect} pxRect drop rectangle in viewBox px
 * @param {{width: number, height: number}} viewport the plan viewport in px
 * @param {string} surfaceId the Surface the frame is dropped onto
 * @returns {Promise<{ok:true, frame:object}|{ok:false, error:string}>}
 */
export function dropFromTray(frameId, pxRect, viewport, surfaceId) {
  const placement = dragToPlacement(pxRect, viewport, surfaceId);
  return moveFrame(frameId, {
    surface_id: placement.surface_id,
    x_mm: placement.x_mm,
    y_mm: placement.y_mm,
  });
}

/**
 * Name a Frame (#94): PUT /v1/operator/frames/{id}/name. Names are unique ignoring case: a taken
 * one answers 409 `name_taken`.
 *
 * @param {string} frameId
 * @param {string} name
 * @returns {Promise<{ok: true, renamed: object}|{ok: false, error: string}>}
 */
export function renameFrame(frameId, name) {
  return callApi("renamed", `/v1/operator/frames/${encodeURIComponent(frameId)}/name`, { method: "PUT", body: { name } });
}

/**
 * Name a wall and label its room (#92): PUT /v1/operator/walls/{surface_id}.
 *
 * @param {string} surfaceId
 * @param {{name: string, room: string}} naming
 * @returns {Promise<{ok: true, wall: object}|{ok: false, error: string}>}
 */
export function nameWall(surfaceId, naming) {
  return callApi("wall", `/v1/operator/walls/${encodeURIComponent(surfaceId)}`, { method: "PUT", body: naming });
}
