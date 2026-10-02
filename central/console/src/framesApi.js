import { apiWrite } from "./apiWrite.js";
import { dragToPlacement, orientationCoherent } from "./projection.js";
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
 * Replace a Frame's persistent Frame profile. The generation is captured when
 * the editor opens so a concurrent equipment change cannot silently authorize
 * this edit. Bound Frames and Frames targeted by a live Run are refused by the
 * server. A successful change also invalidates the committed calibration.
 *
 * @param {string} frameId
 * @param {{width_px:number, height_px:number, diagonal_inches:number, video:boolean}} profile
 * @param {number} expectedGeneration
 * @returns {Promise<{ok:true, changed:boolean, frame:object}|{ok:false, code:string, status:number}>}
 */
export async function updateFrameProfile(frameId, profile, expectedGeneration) {
  const result = await apiWrite(`/v1/operator/frames/${frameId}/profile`, {
    method: "PUT",
    body: { profile, expected_generation: expectedGeneration },
  });
  if (result.ok) {
    return { ok: true, changed: result.data?.changed === true, frame: result.data };
  }
  return { ok: false, code: result.error ?? String(result.status), status: result.status };
}

/** Validate the shared FrameProfile contract and its orientation against a Frame. */
export function frameProfileProblem(profile, frame) {
  const width = Number(profile.width_px);
  const height = Number(profile.height_px);
  const diagonal = Number(profile.diagonal_inches);
  if (!Number.isInteger(width) || width < 1 || width > 16384 ||
      !Number.isInteger(height) || height < 1 || height > 16384) {
    return "Pixel width and height must be whole numbers from 1 to 16384.";
  }
  if (!Number.isFinite(diagonal) || diagonal <= 0) {
    return "Diagonal must be a positive number.";
  }
  if (!orientationCoherent(frame.width_mm, frame.height_mm, width, height)) {
    return "Frame profile must match the frame's orientation.";
  }
  return null;
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
