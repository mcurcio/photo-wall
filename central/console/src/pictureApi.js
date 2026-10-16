import { apiWrite } from "./apiWrite.js";

/**
 * The Picture tab's read and writes (roadmap 1c's Live model; decision 0022, the picture split,
 * slices S2 and S5; display tuning's signal range and test pattern; decision 0028's light): the
 * routes of central/display_routes.py `mount_picture_routes`, answered with the shapes of
 * central/displays/views.py (their one home). Each returns `{ok: true, …}` or `{ok: false, error}`:
 * a refusal answers its code (`unknown_frame`, `frame_unbound`, `no_display`, `session_open`,
 * `unknown_session`, `session_ended`, `stale_sequence`, `not_offered`, `out_of_range`,
 * `not_presented_yet`, and a Restore's `unknown_entry`, `other_display`); a lost request
 * answers `unreachable`.
 *
 * @typedef {string} VcpCode   An MCCS code, two upper-case hex digits ("10" brightness).
 * @typedef {"contrast"|"colour-temperature"|"gamma"} SoftwareSetting
 *   Contrast is a percent 0..150 (100 changes nothing), colour temperature whole kelvin (3000..9300
 *   in 100), gamma in tenths (18..26).
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
 * @typedef {{contrast_percent: number, colour_temperature_kelvin: number, gamma_tenths: number}} SoftwarePictureView
 * @typedef {{matched_lux: number, black: number}} DisplayLightView   The Display's light (decision
 *   0028, contracts/node_picture.py `DisplayLight`): the lux at the Frame when the TV's full white
 *   matched a sheet of paper, and the panel's black as a share of its white.
 * @typedef {{session_id: string, sequence: number, idle_until: number}} PictureSessionView
 * @typedef {"auto"|"full"|"limited"} SignalRange   Which numbers the Pi's HDMI signal uses for black
 *   and white (display tuning): Auto the Pi's choice, Full 0-255, Limited 16-235.
 * @typedef {"applied"|"unsupported"|"failed"} SignalResult
 * @typedef {"white"|"grey-ramp"|"black-level"|"target-pre-software"|"target-post-software"} TestPattern
 *   The two targets are the screen target (calibration targets, decision 0026): drawn raw, through no
 *   Photo Wall step (pre-software), or through them (post-software); either, in any order.
 * @typedef {"letter"|"a4"|"4x6"} PaperSize   The paper the paper target is printed on (4x6: a 4 x 6 in
 *   photo print, carrying fewer squares of the same size).
 * @typedef {"waiting"|"shown"|"ended"} TestPatternState
 * @typedef {{in_force: SignalRange, result: SignalResult, answered: boolean}} SignalView   The Pi's
 *   answer: the range its connector holds, what it made of the wanted one, and whether that answer
 *   is for the latest change.
 * @typedef {{pattern: TestPattern, state: TestPatternState}} TestPatternView   The live session's
 *   test pattern: waiting for the Pi, shown, or ended (the Pi took it off at its bound).
 * @typedef {{frame_id: string, status: "no-display"|"waiting"|"shown", display_id: string|null,
 *            connected: boolean|null, offered_known: boolean, hardware: HardwareSettingView[],
 *            software: SoftwarePictureView, light: DisplayLightView, session: PictureSessionView|null,
 *            signal_range: SignalRange, signal: SignalView|null, test_pattern: TestPatternView|null,
 *            test_pattern_seconds: number}} FramePictureView
 *   `signal_range` is the session's while one is live, else the stored one; `signal` null: no answer
 *   since the Pi's picture controller started; `test_pattern_seconds`: how long the Pi keeps a test
 *   pattern up.
 * @typedef {{sequence: number}} PictureSequence
 * @typedef {"copied"|"no-display"|"same-display"|"session-open"} CopyOutcome
 * @typedef {{frame_id: string, outcome: CopyOutcome, hardware_skipped: VcpCode[]}} PictureCopyResult
 * @typedef {{entry_id: string, expected_sequence: number}} PictureRestore   A picture history entry
 *   (historyApi.js) to load into the session's draft.
 * @typedef {{sequence: number, hardware_skipped: VcpCode[]}} PictureRestoreResult
 *   An accepted Restore: the sequence the next edit names; the entry's display settings this display
 *   does not offer now (not in the draft).
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
 * The Display's light, both values at once (POST …/light-edits; decision 0028): the matched lux
 * (MATCHED_LUX_MIN..MAX) and the panel's black (a share of white, PANEL_BLACK_MIN..MAX).
 *
 * @param {string} sessionId
 * @param {{matched_lux: number, black: number, expected_sequence: number}} edit
 * @returns {Promise<{ok: true, sequence: PictureSequence}|{ok: false, error: string}>}
 */
export function editLight(sessionId, edit) {
  return call("sequence", `${session(sessionId)}/light-edits`, { method: "POST", body: edit });
}

/**
 * The Display's signal range (POST …/signal-range-edits): kept by Done, put back by Revert.
 *
 * @param {string} sessionId
 * @param {{signal_range: SignalRange, expected_sequence: number}} edit
 * @returns {Promise<{ok: true, sequence: PictureSequence}|{ok: false, error: string}>}
 */
export function editSignalRange(sessionId, edit) {
  return call("sequence", `${session(sessionId)}/signal-range-edits`, { method: "POST", body: edit });
}

/**
 * The test pattern on every Frame of the Display, or null for none (POST …/test-pattern-edits);
 * never kept: Done, Revert and the session's end take it off.
 *
 * @param {string} sessionId
 * @param {{test_pattern: TestPattern|null, expected_sequence: number}} edit
 * @returns {Promise<{ok: true, sequence: PictureSequence}|{ok: false, error: string}>}
 */
export function editTestPattern(sessionId, edit) {
  return call("sequence", `${session(sessionId)}/test-pattern-edits`, { method: "POST", body: edit });
}

/**
 * The paper target to print (GET /v1/operator/calibration-target/paper.pdf; calibration targets,
 * decision 0026): the path's one home in the console, for a link the browser downloads (the session
 * cookie goes with it; Central answers it as an attachment).
 *
 * @param {PaperSize} paper
 * @returns {string}
 */
export function paperTargetUrl(paper) {
  return `/v1/operator/calibration-target/paper.pdf?paper=${encodeURIComponent(paper)}`;
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
 * Restore: load a picture history entry into the session's draft, live on the display (POST
 * …/restores; configuration snapshots). The operator still presses Done. Refused `unknown_entry`,
 * `other_display` (another display's values cross only by Copy), `session_ended`, `stale_sequence`.
 *
 * @param {string} sessionId
 * @param {PictureRestore} body
 * @returns {Promise<{ok: true, result: PictureRestoreResult}|{ok: false, error: string}>}
 */
export function restorePicture(sessionId, body) {
  return call("result", `${session(sessionId)}/restores`, { method: "POST", body });
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
