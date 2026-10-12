import { callApi } from "./weekApi.js";

/**
 * The Power lane's reads and writes (roadmap 2a; central/power_routes.py, central/power/views.py,
 * the one home of their shapes): Night off, the live holds, All off and All on, and Resume
 * schedule. Each returns `{ok: true, …}` or `{ok: false, error, current?}`: a refusal answers its
 * code (`changed`, `no-days`, `needs-place`, `unknown-frame`); a lost request answers
 * `unreachable`. 1b's per-Frame Test stays in powerApi.js.
 */

const POWER = "/v1/operator/power/";


/** @returns {Promise<{ok: true, view: object}|{ok: false, error: string}>} the NightOffView */
export function readNightOff() {
  return callApi("view", `${POWER}night-off`, { method: "GET" });
}

/**
 * Save Night off from the revision the editor loaded; a newer save answers `changed` (409).
 *
 * @param {number} expectedRevision
 * @param {object} nightOff the NightOffDraft
 * @returns {Promise<{ok: true, view: object}|{ok: false, error: string}>}
 */
export function saveNightOff(expectedRevision, nightOff) {
  return callApi("view", `${POWER}night-off`, {
    method: "PUT", body: { expected_revision: expectedRevision, night_off: nightOff },
  });
}

/** @returns {Promise<{ok: true, holds: object[]}|{ok: false, error: string}>} the live HoldViews */
export function readHolds() {
  return callApi("holds", `${POWER}holds`, { method: "GET" });
}

/**
 * All off or All on (F5) on every current Frame. `requestId` is fresh per press and reused on a
 * retry of that press, so a retried or doubled press makes one hold.
 *
 * @param {"off"|"on"} power
 * @param {string} requestId
 * @returns {Promise<{ok: true, hold: object}|{ok: false, error: string}>}
 */
export function hold(power, requestId) {
  return callApi("hold", `${POWER}all-${power}`, { method: "POST", body: { request_id: requestId } });
}

/**
 * Resume schedule (#91): end the holds on these Frames.
 *
 * @param {string[]} frameIds
 * @returns {Promise<{ok: true, done: null}|{ok: false, error: string}>}
 */
export function resumeSchedule(frameIds) {
  return callApi("done", `${POWER}resume`, { method: "POST", body: { frame_ids: frameIds } });
}
