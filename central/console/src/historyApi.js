import { apiWrite } from "./apiWrite.js";

/**
 * The tuning history reads (configuration snapshots; owner, 2026-10-13: "configuration snapshots"
 * that keep "a history of what has been done in the past"): the routes of
 * central/tuning_history_routes.py, answered with the shapes of central/displays/views.py (their
 * one home). Each returns `{ok: true, history}` or `{ok: false, error}`: a refusal answers its code
 * (`unknown_display`, `bad_time`, `bad_range`); a lost request answers `unreachable`. Every `at` is
 * UTC (ISO 8601 with its zone), PostgreSQL's clock when the entry was written.
 *
 * @typedef {import("./pictureApi.js").SoftwarePictureView} SoftwarePictureView
 * @typedef {import("./pictureApi.js").SignalRange} SignalRange
 * @typedef {import("./pictureApi.js").TestPattern} TestPattern
 * @typedef {import("./pictureApi.js").VcpCode} VcpCode
 * @typedef {{id: string, name: string}} DisplayRefView   A Display by id and its name today.
 * @typedef {{id: string, name: string}} FrameRefView   A Frame by id and its name today.
 * @typedef {"kept-done"|"kept-copied"|"kept-unrecorded"|"sent-draft"|"sent-back"|"sent-kept"} PictureEntryKind
 *   Why an entry was written: kept by Done, by Copy from another display, or found changed outside
 *   Photo Wall; sent as a session's draft (not kept), put back by Revert or a lapse, or the kept
 *   values sent anew.
 * @typedef {{hardware: Record<VcpCode, number>, software: SoftwarePictureView, signal_range: SignalRange,
 *            lut_recipe_id: string|null}} PictureValuesView
 *   The hardware codes set in Photo Wall (a code absent is "as the display has it"), the Photo Wall
 *   values, the signal range, and the recipe its LUT was made with then (null: no LUT).
 * @typedef {{entry_id: string, display: DisplayRefView, kind: PictureEntryKind, number: number, at: string,
 *            frame: FrameRefView|null, session_id: string|null, restored_from: string|null,
 *            picture: PictureValuesView, test_pattern: TestPattern|null}} PictureEntryView
 * @typedef {{display: DisplayRefView, unrecorded_change: boolean, entries: PictureEntryView[]}} PictureHistoryView
 *   Newest first by `at`, then number. `unrecorded_change`: the display's stored values differ from
 *   its newest kept entry.
 */

/**
 * One GET through apiWrite (a read: the write fence does not move), folded into
 * `{ok: true, history}` or `{ok: false, error}`.
 *
 * @param {string} path
 * @param {{before?: string, limit?: number}} page
 */
async function readHistory(path, { before, limit } = {}) {
  const query = new URLSearchParams();
  if (before !== undefined) query.set("before", before);
  if (limit !== undefined) query.set("limit", String(limit));
  const search = query.toString() === "" ? "" : `?${query}`;
  try {
    const result = await apiWrite(`${path}${search}`, { method: "GET" });
    return result.ok ? { ok: true, history: result.data } : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

/**
 * One Display's picture history, newest first (GET …/displays/{id}/picture-history). `before`: an
 * entry's `at` (exclusive), for the page after it; `limit` 1..500 (Central's default 100).
 *
 * @param {string} displayId
 * @param {{before?: string, limit?: number}} [page]
 * @returns {Promise<{ok: true, history: PictureHistoryView}|{ok: false, error: string}>}
 */
export function readPictureHistory(displayId, { before, limit } = {}) {
  return readHistory(`/v1/operator/displays/${encodeURIComponent(displayId)}/picture-history`, { before, limit });
}
