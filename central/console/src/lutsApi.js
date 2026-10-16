import { apiWrite } from "./apiWrite.js";

/**
 * The LUTs list's routes (print look, design "The LUT"): central/lut_routes.py, answered with the
 * shapes of central/displays/views.py (their one home). Settings › LUTs lists, adds and removes;
 * a Frame's lighting picks its paper profile from them (decision 0028).
 *
 * @typedef {{profile_sha256: string, recipe_id: string, name: string, added_at: number, used_by: string[]}} LutView
 *   One LUT: its paper profile's digest (its name on the wire), the recipe Central bakes it with
 *   (a Frame's paper names both, contracts/lut.py `LutRef`), the profile's own name, when it was
 *   added (Central's clock) and the Frames whose paper uses it, by name.
 */

const LUTS = "/v1/operator/luts";

/** An ICC profile's media type: the upload's body is the file as chosen. */
const ICC_PROFILE = "application/vnd.iccprofile";

/**
 * Every LUT on the list (GET /v1/operator/luts).
 *
 * @returns {Promise<{ok: true, luts: LutView[]}|{ok: false, error: string}>}
 */
export async function listLuts() {
  try {
    const result = await apiWrite(LUTS, { method: "GET" });
    return result.ok ? { ok: true, luts: result.data?.luts ?? [] } : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

/**
 * Add a paper profile (POST /v1/operator/luts, the .icc file's bytes): Central checks it and bakes
 * its LUT once. `added` is false when the same profile was already on the list (200). A refusal
 * is Central's code: not_a_colour_profile, not_a_printer_profile or too_large.
 *
 * @param {Blob} profile the file the operator chose
 * @returns {Promise<{ok: true, lut: LutView, added: boolean}|{ok: false, error: string}>}
 */
export async function addLut(profile) {
  try {
    const result = await apiWrite(LUTS, { method: "POST", body: profile, contentType: ICC_PROFILE });
    return result.ok ? { ok: true, lut: result.data, added: result.status === 201 }
      : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

/**
 * Remove a LUT nothing uses (DELETE /v1/operator/luts/{profile_sha256}). Central refuses
 * lut_in_use with the Frames whose paper uses it, or lut_unknown when it is not on the list.
 *
 * @param {string} profileSha256
 * @returns {Promise<{ok: true}|{ok: false, error: string, frames: string[]}>}
 */
export async function removeLut(profileSha256) {
  try {
    const result = await apiWrite(`${LUTS}/${encodeURIComponent(profileSha256)}`, { method: "DELETE" });
    return result.ok ? { ok: true }
      : { ok: false, error: result.error ?? `http_${result.status}`, frames: result.data?.frames ?? [] };
  } catch {
    return { ok: false, error: "unreachable", frames: [] };
  }
}
