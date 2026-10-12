import { apiWrite } from "./apiWrite.js";

/**
 * The week page's read and the Default Scene's write (Power and the week; central/why_routes.py
 * `Week`, central/program_routes.py `DefaultSceneRequest`, the one homes of their shapes). Central
 * works out every window and power band on the house clock; the console draws them.
 */

/**
 * One operator call folded into `{ok: true, [key]: data}` or `{ok: false, error}` (the refusal's
 * code, else `http_<status>`; `unreachable` for a lost request). The Power and the week modules'
 * one fold (houseApi.js, whyApi.js, powerScheduleApi.js, the names in framesApi.js).
 *
 * @param {string} key
 * @param {string} path
 * @param {{method: string, body?: any}} options
 */
export async function callApi(key, path, options) {
  try {
    const result = await apiWrite(path, options);
    return result.ok ? { ok: true, [key]: result.data } : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

/**
 * Seven house days from `firstDay` ("2026-10-12", a Monday on the week page).
 *
 * @param {string} firstDay
 * @returns {Promise<{ok: true, week: object}|{ok: false, error: string}>}
 */
export function readWeek(firstDay) {
  return callApi("week", `/v1/operator/week?first_day=${encodeURIComponent(firstDay)}`, { method: "GET" });
}

/**
 * The Scene that plays wherever no Schedule does (E5); null: nothing.
 *
 * @param {string|null} sceneId
 * @returns {Promise<{ok: true, done: null}|{ok: false, error: string}>}
 */
export function setDefaultScene(sceneId) {
  return callApi("done", "/v1/operator/default-scene", { method: "PUT", body: { scene_id: sceneId } });
}
