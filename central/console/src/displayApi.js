import { apiWrite } from "./apiWrite.js";

/**
 * The Hardware tab's one read of a Frame's Display (roadmap 1b, slice K1): GET
 * /v1/operator/frames/{frame_id}/display (central/display_routes.py), answered with
 * `FrameDisplayView` (central/displays/views.py, its one home).
 *
 * @typedef {{width: number, height: number, refresh_millihertz: number, preferred: boolean}} ModeView
 * @typedef {{id: string, maker: string, product: number, name: string, serial: string|null,
 *            tied_to_frame: boolean, modes: ModeView[], power_method: string|null,
 *            switch_input_on_power_on: boolean, never_off_on_other_input: boolean}} DisplayView
 * @typedef {{frame_id: string, readiness: "unbound"|"display-changed"|"position-needed"|"ready",
 *            player_id: string|null, output_id: string|null, display: DisplayView|null,
 *            position_display: DisplayView|null, connected: boolean|null,
 *            reported_at: number|null}} FrameDisplayView
 */

/**
 * Read one Frame's Display. A refusal answers its code (`unknown_frame`); a lost request
 * answers `unreachable`.
 *
 * @param {string} frameId
 * @returns {Promise<{ok: true, view: FrameDisplayView}|{ok: false, error: string}>}
 */
export async function readFrameDisplay(frameId) {
  try {
    const result = await apiWrite(`/v1/operator/frames/${encodeURIComponent(frameId)}/display`, { method: "GET" });
    return result.ok ? { ok: true, view: result.data } : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}
