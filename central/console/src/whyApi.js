import { callApi } from "./weekApi.js";

/**
 * A Frame's Why (Power and the week, E6; central/why_routes.py, central/why/model.py `FrameWhy`,
 * the one home of its shape): what plays and what it beat, and its display's power.
 *
 * @param {string} frameId
 * @returns {Promise<{ok: true, why: object}|{ok: false, error: string}>}
 */
export function readFrameWhy(frameId) {
  return callApi("why", `/v1/operator/frames/${encodeURIComponent(frameId)}/why`, { method: "GET" });
}
