import { apiWrite } from "./apiWrite.js";

/**
 * The one candidates read (pass 2 slice 3 §6, §14): `GET /v1/operator/sources/
 * {ref}/candidates?frame_id=<frame>`, which Central HARD-FILTERS by the frame's
 * profile (design J4) and marks each candidate with the planner's `standing`
 * for that frame (central/planner.py `candidate_standing`: "usable",
 * "preparing", "failed_to_prepare" or "no_compatible_variant"). The authoring
 * choosers and "Check this frame" both read through here, so neither region
 * imports the other. Throws on a refusal, with the served error code.
 *
 * @param {string} sourceRef
 * @param {string} frameId
 * @returns {Promise<{status: string, candidates: Array<object>}>} the Source's
 *   served refresh status and its candidates for this frame
 */
export async function readCandidates(sourceRef, frameId) {
  // frame_id makes Central drop every asset ineligible for THIS frame's
  // profile — the profile hard-filter. Dropping it would offer incompatible
  // assets, which is exactly what the chooser's mutation probe attacks.
  const result = await apiWrite(
    `/v1/operator/sources/${encodeURIComponent(sourceRef)}/candidates?frame_id=${encodeURIComponent(frameId)}`,
    { method: "GET" },
  );
  if (!result.ok) {
    throw new Error(result.error ?? `HTTP ${result.status}`);
  }
  return { status: result.data?.status ?? null, candidates: result.data?.candidates ?? [] };
}
