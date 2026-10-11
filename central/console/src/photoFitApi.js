import { apiWrite } from "./apiWrite.js";
import { CONSOLE_HEADER } from "./session.js";

/**
 * The photo fit routes (follow-ups 2026-10-11, slices B1 and B2; central/photo_fit_routes.py,
 * the one home of their shapes): the fit analysis every Fit board reads, and Settings › Photo
 * fit's read and write. Each call returns `{ok: true, …}` or `{ok: false, error}`: a refusal
 * answers its code (`source_unknown`, `draft_unknown`, `frame_unknown`,
 * `draft_frame_not_judged`, `invalid_request`); a lost request answers `unreachable`.
 */

export const ANALYSIS_PATH = "/v1/operator/photo-fit/analysis";
export const SETTINGS_PATH = "/v1/operator/settings/photo-fit";

async function call(key, path, options) {
  try {
    const result = await apiWrite(path, options);
    return result.ok ? { ok: true, [key]: result.data } : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

const ANALYSIS_TIMEOUT_MS = 15000;

/**
 * How a selection fits the Frames (`FitAnalysisRequest` → `FitAnalysis`). A read: it changes
 * nothing, and is a POST only because its question is a body. So it does not go through
 * apiWrite, whose write fence would drop every snapshot read that overlaps it (a board asks
 * again on every change of a draft). `signal` aborts a question a newer one replaced.
 *
 * @param {object} request
 * @param {AbortSignal} [signal]
 * @returns {Promise<{ok: true, analysis: object}|{ok: false, error: string}>}
 */
export async function analyseFit(request, signal) {
  const timeout = AbortSignal.timeout(ANALYSIS_TIMEOUT_MS);
  try {
    const response = await fetch(ANALYSIS_PATH, {
      method: "POST",
      headers: { ...CONSOLE_HEADER, "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal: signal === undefined ? timeout : AbortSignal.any([signal, timeout]),
    });
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    return response.ok ? { ok: true, analysis: data } : { ok: false, error: data?.error ?? `http_${response.status}` };
  } catch {
    return { ok: false, error: signal?.aborted ? "aborted" : "unreachable" };
  }
}

/** @returns {Promise<{ok: true, view: object}|{ok: false, error: string}>} the FitSettingsView */
export function readFitSettings() {
  return call("view", SETTINGS_PATH, { method: "GET" });
}

/**
 * Save every tuning value at once (a PUT of the complete `FitSettings`).
 *
 * @param {object} settings
 * @returns {Promise<{ok: true, view: object}|{ok: false, error: string}>}
 */
export function saveFitSettings(settings) {
  return call("view", SETTINGS_PATH, { method: "PUT", body: settings });
}
