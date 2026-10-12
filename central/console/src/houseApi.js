import { callApi } from "./weekApi.js";

/**
 * Settings › House's read and write (Power and the week; central/house_routes.py, the one home of
 * their shapes, `HouseSettings` and `HouseSettingsDraft` in central/house_time/model.py). Each
 * returns `{ok: true, settings}` or `{ok: false, error}`: a refusal answers its code
 * (`unknown-zone`); a lost request answers `unreachable`.
 *
 * @typedef {{zone: string, place: {latitude: number, longitude: number}|null}} HouseTime
 * @typedef {{time: HouseTime, units: "cm"|"in", clock: "12h"|"24h"}} HouseSettingsDraft
 */

export const HOUSE_PATH = "/v1/operator/house";


/** @returns {Promise<{ok: true, settings: object}|{ok: false, error: string}>} the HouseSettings */
export function readHouse() {
  return callApi("settings", HOUSE_PATH, { method: "GET" });
}

/**
 * Save Settings › House (#2-#5). Central refuses a zone the IANA database does not know.
 *
 * @param {HouseSettingsDraft} draft
 */
export function saveHouse(draft) {
  return callApi("settings", HOUSE_PATH, { method: "PUT", body: draft });
}
