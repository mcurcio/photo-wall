import { apiWrite } from "./apiWrite.js";

/**
 * The Picture tab's read and writes (roadmap 1c's Live model; decision 0022, the picture split,
 * slices S2 and S5; the print look's LUT choice): the routes of central/display_routes.py
 * `mount_picture_routes`, answered with the
 * shapes of central/displays/views.py (their one home). Each returns `{ok: true, …}` or
 * `{ok: false, error}`: a refusal answers its code (`unknown_frame`, `frame_unbound`, `no_display`,
 * `session_open`, `unknown_session`, `session_ended`, `stale_sequence`, `not_offered`,
 * `out_of_range`, `not_presented_yet`, `unknown_lut`); a lost request answers `unreachable`.
 *
 * @typedef {string} VcpCode   An MCCS code, two upper-case hex digits ("10" brightness).
 * @typedef {"brightness"|"contrast"|"colour-temperature"|"gamma"|"saturation"|"highlight-rolloff"
 *            |"black-floor"|"lut-amount"} SoftwareSetting
 *   Brightness and contrast are percents 0..150 (100 changes nothing), colour temperature whole
 *   kelvin (3000..9300 in 100), gamma in tenths (18..26); the print look's: saturation a percent
 *   0..150 (100 changes nothing), highlight rolloff a percent 0..100 (0 changes nothing), black
 *   floor in tenths of a percent of white 0..100 (0 changes nothing), LUT amount a percent 0..100.
 * @typedef {"taken"|"not-taken"|"no-answer"} WriteResult
 * @typedef {"level"|"choice"|"read-only-choice"} RangeKind
 * @typedef {{value: number, name: string}} ChoiceView
 * @typedef {{kind: RangeKind, maximum: number|null, choices: ChoiceView[]}} HardwareRangeView
 * @typedef {{range: HardwareRangeView, current: number|null}} OfferedView
 * @typedef {{result: WriteResult, before: number|null, after: number|null}} HardwareResultView
 * @typedef {{code: VcpCode, name: string, offered: OfferedView|null, value: number|null,
 *            result: HardwareResultView|null, changed_on_buttons: boolean}} HardwareSettingView
 *   One row of Display (hardware): every offered setting and every set code. `offered` null: set
 *   here, not offered at the last check; `value` null: as the display has it; `result` only for the
 *   latest picture change; `changed_on_buttons`: the display's latest read differs from the value
 *   set here (someone used its own buttons).
 * @typedef {{profile_sha256: string, name: string}} LutChoiceView   The LUT in force: its paper
 *   profile's digest and name.
 * @typedef {{brightness_percent: number, contrast_percent: number, colour_temperature_kelvin: number,
 *            gamma_tenths: number, saturation_percent: number, highlight_rolloff_percent: number,
 *            black_floor_tenths: number, lut_amount_percent: number, lut: LutChoiceView|null}} SoftwarePictureView
 * @typedef {{session_id: string, sequence: number, idle_until: number}} PictureSessionView
 * @typedef {{frame_id: string, status: "no-display"|"waiting"|"shown", display_id: string|null,
 *            connected: boolean|null, offered_known: boolean, hardware: HardwareSettingView[],
 *            software: SoftwarePictureView, session: PictureSessionView|null}} FramePictureView
 * @typedef {{sequence: number}} PictureSequence
 * @typedef {"copied"|"no-display"|"same-display"|"session-open"} CopyOutcome
 * @typedef {{frame_id: string, outcome: CopyOutcome, hardware_skipped: VcpCode[]}} PictureCopyResult
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
 * One display setting at one value, or null to hand it back (POST …/hardware-edits).
 *
 * @param {string} sessionId
 * @param {{code: VcpCode, value: number|null, expected_sequence: number}} edit
 * @returns {Promise<{ok: true, sequence: PictureSequence}|{ok: false, error: string}>}
 */
export function editHardware(sessionId, edit) {
  return call("sequence", `${session(sessionId)}/hardware-edits`, { method: "POST", body: edit });
}

/**
 * One Photo Wall setting at one value (POST …/software-edits).
 *
 * @param {string} sessionId
 * @param {{setting: SoftwareSetting, value: number, expected_sequence: number}} edit
 * @returns {Promise<{ok: true, sequence: PictureSequence}|{ok: false, error: string}>}
 */
export function editSoftware(sessionId, edit) {
  return call("sequence", `${session(sessionId)}/software-edits`, { method: "POST", body: edit });
}

/**
 * The LUT choice: a listed paper profile's digest, or null for no LUT (POST …/lut-edits). Refused
 * `unknown_lut` for a profile no longer on the LUTs list.
 *
 * @param {string} sessionId
 * @param {{profile_sha256: string|null, expected_sequence: number}} edit
 * @returns {Promise<{ok: true, sequence: PictureSequence}|{ok: false, error: string}>}
 */
export function editLut(sessionId, edit) {
  return call("sequence", `${session(sessionId)}/lut-edits`, { method: "POST", body: edit });
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
 * Copy this Frame's Display values to other Frames' Displays (POST …/picture-copies, #119): Photo
 * Wall's settings, and the display settings set here where each target offers them; never position.
 *
 * @param {string} frameId
 * @param {string[]} toFrames
 * @returns {Promise<{ok: true, results: PictureCopyResult[]}|{ok: false, error: string}>}
 */
export function copyPicture(frameId, toFrames) {
  return call("results", `${frame(frameId)}/picture-copies`, { method: "POST", body: { to_frames: toFrames } });
}
