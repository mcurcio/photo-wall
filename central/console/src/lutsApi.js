import { apiWrite } from "./apiWrite.js";

/**
 * The LUTs list's read (print look, design "The LUT"): central/lut_routes.py, answered with the
 * shapes of central/displays/views.py (their one home). The Picture tab's LUT picker lists them.
 *
 * @typedef {{profile_sha256: string, name: string, added_at: number, used_by: string[]}} LutView
 *   One LUT: its paper profile's digest (its name on the wire), the profile's own name, when it
 *   was added (Central's clock) and the Displays that use it, by name.
 */

/**
 * Every LUT on the list (GET /v1/operator/luts).
 *
 * @returns {Promise<{ok: true, luts: LutView[]}|{ok: false, error: string}>}
 */
export async function listLuts() {
  try {
    const result = await apiWrite("/v1/operator/luts", { method: "GET" });
    return result.ok ? { ok: true, luts: result.data?.luts ?? [] } : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}
