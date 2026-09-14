import { apiWrite } from "./apiWrite.js";
import { dragToPlacement } from "./projection.js";

/**
 * Low-level Frame write module (bead R-apiwrite). Holds the four Frame mutations
 * that both {@link module:Plan} and {@link module:UnplacedTray} issue —
 * `createFrame`/`moveFrame`/`deleteFrame`/`dropFromTray` — so the tray no longer
 * imports them from its sibling `Plan.jsx`. Each write goes through the shared
 * {@link apiWrite} helper and keeps its own result shape and message table; wrap
 * the calls in `useMutate()` at the call site so the plan refreshes after a write.
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
 * Create a Frame (Bead 10): POST /v1/operator/frames with the drag placement +
 * the operator-supplied display profile. `FrameCreate` requires an `id` that the
 * design/POST body do not carry, so a client id is generated here (matching the
 * `Identifier` pattern `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`) — see the report.
 * The server re-runs the orientation-coherence guard and 422s an incoherent
 * profile. Wrap in `useMutate()` at the call site so the plan refreshes.
 *
 * @param {{surface_id: string, x_mm: number, y_mm: number, width_mm: number, height_mm: number}} placement
 * @param {{width_px: number, height_px: number, diagonal_inches: number, video: boolean}} profile
 * @returns {Promise<{ok:true, frame:object}|{ok:false, error:string}>}
 */
export async function createFrame(placement, profile) {
  const id = `frame-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const result = await apiWrite("/v1/operator/frames", {
    method: "POST",
    body: { id, ...placement, profile },
  });
  return interpretFrame(result);
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
 * @returns {Promise<{ok:true}|{ok:false, message:string}>}
 */
export async function deleteFrame(frameId) {
  const result = await apiWrite(`/v1/operator/frames/${frameId}`, {
    method: "DELETE",
  });
  if (result.ok) {
    return { ok: true };
  }
  return { ok: false, message: DELETE_MESSAGES[result.error] ?? "Could not delete the frame." };
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
