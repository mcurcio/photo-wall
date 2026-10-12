import { apiWrite } from "./apiWrite.js";

/**
 * The one candidates read (pass 2 slice 3 §6, §14): `GET /v1/operator/sources/
 * {ref}/candidates?frame_id=<frame>&crop_limit=<fraction>`, which Central
 * HARD-FILTERS by the photo fit rule on the frame (central/wall_plan/fit.py,
 * roadmap 5a) at the Scene's crop limit, and marks each candidate with the
 * planner's `standing` for that frame (central/planner.py `candidate_standing`:
 * "usable", "preparing", "failed_to_prepare" or "no_compatible_variant") and its
 * `fit` ("fits" or "not_measured"). The authoring choosers, "Check this frame"
 * and the Frame page's Photos read through here, so none imports another.
 * Throws on a refusal, with the served error code.
 *
 * `skipped` (roadmap 5a, run ledger K2) is how many photos Central left out for
 * each fit refusal (`beyond_crop_limit`, `below_frame_pixels`, `face_cut`,
 * `interest_cut`, `no_video_here`), plus `not_measured`, how many of the listed
 * candidates are not measured yet (central/media_repository.py
 * `source_candidates`).
 *
 * @param {string} sourceRef
 * @param {string} frameId
 * @param {number|null} [cropLimit] the Scene's crop limit, a fraction (0.35, never
 *   35); the house crop limit when not given or null (the Scene follows it, follow-ups B1)
 * @returns {Promise<{ready: boolean, candidates: Array<object>,
 *            skipped: Record<string, number>}>} whether the Source's pool has a
 *   complete check, its candidates for this frame, and the skipped counts
 */
export async function readCandidates(sourceRef, frameId, cropLimit) {
  // frame_id makes Central drop every asset that does not fit THIS frame — the
  // hard filter. Dropping it would offer photos that do not fit, which is exactly
  // what the chooser's mutation probe attacks; so would dropping crop_limit,
  // which judges the Scene's photos at another limit than the Scene's.
  const query = new URLSearchParams({ frame_id: frameId });
  if (cropLimit !== undefined && cropLimit !== null) {
    query.set("crop_limit", String(cropLimit));
  }
  const result = await apiWrite(
    `/v1/operator/sources/${encodeURIComponent(sourceRef)}/candidates?${query}`,
    { method: "GET" },
  );
  if (!result.ok) {
    throw new Error(result.error ?? `HTTP ${result.status}`);
  }
  return {
    ready: result.data?.ready === true,
    candidates: result.data?.candidates ?? [],
    skipped: result.data?.skipped ?? {},
  };
}
