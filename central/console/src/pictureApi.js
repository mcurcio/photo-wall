import { apiWrite } from "./apiWrite.js";

/**
 * The Picture tab's read and writes (roadmap 1c, slice K1): the eight routes of
 * central/display_routes.py `mount_picture_routes`, answered with the shapes of
 * central/displays/views.py (their one home). Each returns `{ok: true, …}` or `{ok: false, error}`:
 * a refusal answers its code (`unknown_frame`, `frame_unbound`, `no_display`, `session_open`,
 * `unknown_session`, `session_ended`, `stale_sequence`, `not_placed_yet`, `out_of_range`,
 * `display_did_not_answer`, `not_presented_yet`); a lost request answers `unreachable`.
 *
 * @typedef {"brightness"|"contrast"|"colour-temperature"|"gamma"} Setting
 *   Brightness and contrast are percents, colour temperature whole kelvin (3000..9300 in 100),
 *   gamma in tenths (18..26).
 * @typedef {"display"|"picture-adjustment"} ActsOn
 * @typedef {"taken"|"not-taken"|"no-answer"} WriteResult
 * @typedef {{acts_on: ActsOn|null, value: number|null, answered: boolean|null, read: number|null,
 *            result: WriteResult|null}} PictureSettingView
 *   `acts_on` null: not placed yet (the display has not been seen); `value` null: as the display
 *   has it; `answered`, `read` null: no report; `result` only for the latest picture change.
 * @typedef {{session_id: string, sequence: number, idle_until: number}} PictureSessionView
 * @typedef {{frame_id: string, status: "no-display"|"waiting"|"shown", display_id: string|null,
 *            brightness: PictureSettingView, contrast: PictureSettingView,
 *            colour_temperature_kelvin: number, gamma_tenths: number, connected: boolean|null,
 *            session: PictureSessionView|null}} FramePictureView
 * @typedef {{sequence: number}} PictureSequence
 * @typedef {"copied"|"no-display"|"same-display"|"session-open"} CopyOutcome
 * @typedef {{frame_id: string, outcome: CopyOutcome}} PictureCopyResult
 */

/**
 * One call through apiWrite, its answer folded into `{ok: true, [key]: data}` or `{ok: false, error}`.
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
const session = (sessionId) => `/v1/operator/picture-sessions/${encodeURIComponent(sessionId)}`;

/**
 * Read one Frame's Picture tab (GET …/picture).
 *
 * @param {string} frameId
 * @returns {Promise<{ok: true, view: FramePictureView}|{ok: false, error: string}>}
 */
export function readFramePicture(frameId) {
  return call("view", `${frame(frameId)}/picture`, { method: "GET" });
}

/**
 * Begin a picture session on the Frame's Display (POST …/picture-sessions).
 *
 * @param {string} frameId
 * @returns {Promise<{ok: true, session: PictureSessionView}|{ok: false, error: string}>}
 */
export function beginPicture(frameId) {
  return call("session", `${frame(frameId)}/picture-sessions`, { method: "POST" });
}

/**
 * One setting at one value (POST …/edits).
 *
 * @param {string} sessionId
 * @param {{setting: Setting, value: number, expected_sequence: number}} edit
 * @returns {Promise<{ok: true, sequence: PictureSequence}|{ok: false, error: string}>}
 */
export function editPicture(sessionId, edit) {
  return call("sequence", `${session(sessionId)}/edits`, { method: "POST", body: edit });
}

/**
 * Move brightness or contrast to where it acts (POST …/places).
 *
 * @param {string} sessionId
 * @param {{setting: "brightness"|"contrast", acts_on: ActsOn, expected_sequence: number}} place
 * @returns {Promise<{ok: true, sequence: PictureSequence}|{ok: false, error: string}>}
 */
export function placePicture(sessionId, place) {
  return call("sequence", `${session(sessionId)}/places`, { method: "POST", body: place });
}

/**
 * Keep the session (POST …/keepalive).
 *
 * @param {string} sessionId
 * @returns {Promise<{ok: true, session: PictureSessionView}|{ok: false, error: string}>}
 */
export function keepPictureAlive(sessionId) {
  return call("session", `${session(sessionId)}/keepalive`, { method: "POST" });
}

/**
 * Done: store the session's values (POST …/done); refused until the Pi shows the latest change.
 *
 * @param {string} sessionId
 * @returns {Promise<{ok: true, done: null}|{ok: false, error: string}>}
 */
export function pictureDone(sessionId) {
  return call("done", `${session(sessionId)}/done`, { method: "POST" });
}

/**
 * Revert: end the session and put back what was in force before it (DELETE the session).
 *
 * @param {string} sessionId
 * @returns {Promise<{ok: true, reverted: null}|{ok: false, error: string}>}
 */
export function revertPicture(sessionId) {
  return call("reverted", session(sessionId), { method: "DELETE" });
}

/**
 * Copy this Frame's Display values to other Frames' Displays (POST …/picture-copies, #119): the
 * four values only, never where they act, never position.
 *
 * @param {string} frameId
 * @param {string[]} toFrames
 * @returns {Promise<{ok: true, results: PictureCopyResult[]}|{ok: false, error: string}>}
 */
export function copyPicture(frameId, toFrames) {
  return call("results", `${frame(frameId)}/picture-copies`, { method: "POST", body: { to_frames: toFrames } });
}
