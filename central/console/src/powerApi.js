import { apiWrite } from "./apiWrite.js";

/**
 * The Power tab's read and writes (roadmap 1b, slice K2): the four routes of
 * central/display_routes.py, answered with the shapes of central/displays/views.py (their one
 * home). Each returns `{ok: true, …}` or `{ok: false, error}`: a refusal answers its code
 * (`unknown_frame`, `frame_unbound`, `unknown_display`); a lost request answers `unreachable`.
 *
 * @typedef {"hdmi-cec"|"ddc-ci"|"signal-off"} PowerMethod
 * @typedef {{power_method: PowerMethod|null, switch_input_on_power_on: boolean,
 *            never_off_on_other_input: boolean}} DisplayPowerSettings
 *   `power_method` null is best detected.
 * @typedef {{request_id: string, for_seconds: number}} PowerTestAccepted
 */

/**
 * One call through apiWrite, its answer folded into the shape above under `key`.
 *
 * @param {string} key
 * @param {string} path
 * @param {{method: string, body?: any}} options
 */
async function call(key, path, options) {
  try {
    const result = await apiWrite(path, options);
    return result.ok ? { ok: true, [key]: result.data } : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

const frame = (frameId) => `/v1/operator/frames/${encodeURIComponent(frameId)}`;

/**
 * Read one Frame's Power tab (GET …/power, `FramePowerView`).
 *
 * @param {string} frameId
 * @returns {Promise<{ok: true, view: object}|{ok: false, error: string}>}
 */
export function readFramePower(frameId) {
  return call("view", `${frame(frameId)}/power`, { method: "GET" });
}

/**
 * Test: turn off (POST …/power-tests): a new test replaces the Frame's last one.
 *
 * @param {string} frameId
 * @returns {Promise<{ok: true, accepted: PowerTestAccepted}|{ok: false, error: string}>}
 */
export function requestPowerTest(frameId) {
  return call("accepted", `${frame(frameId)}/power-tests`, { method: "POST" });
}

/**
 * Turn on (DELETE …/power-tests): end the Frame's test, so the request underneath takes over
 * (owner q1, 2026-10-10). Nothing is put on top; with no test there is nothing to end.
 *
 * @param {string} frameId
 * @returns {Promise<{ok: true, ended: null}|{ok: false, error: string}>}
 */
export function endPowerTest(frameId) {
  return call("ended", `${frame(frameId)}/power-tests`, { method: "DELETE" });
}

/**
 * Save a Display's power settings (PUT /v1/operator/displays/{id}/power-settings, #116-#118).
 *
 * @param {string} displayId
 * @param {DisplayPowerSettings} settings
 * @returns {Promise<{ok: true, settings: DisplayPowerSettings}|{ok: false, error: string}>}
 */
export function savePowerSettings(displayId, settings) {
  return call("settings", `/v1/operator/displays/${encodeURIComponent(displayId)}/power-settings`,
    { method: "PUT", body: settings });
}
