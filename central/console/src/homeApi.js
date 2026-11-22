import { CONSTANTS } from "./generated/contract.ts";
import { CONSOLE_HEADER } from "./session.js";

/**
 * Home's read (decision 0036, "Home's read"): `GET /v1/operator/home`, Central's one document of
 * the house word, every Frame's status on its wall, the overrides and now and next
 * (central/frame_status/views.py `HomeView`, the generated contract's type).
 *
 * The document carries a change number and Central answers with it as an `ETag`; the read sends
 * the last one back as `If-None-Match`, so an unchanged wall costs no body (304) and keeps the
 * SAME object: nothing that depends on it renders again. Nothing here assumes polling: a push
 * stream would hand over the same document.
 *
 * @typedef {import("./generated/contract").HomeView} HomeView
 * @typedef {{view: HomeView|null, etag: string|null, failed: boolean, unauthorized?: boolean}} HomeRead
 *   `view`: the last document read (kept through a failed read); `failed`: the newest read did not
 *   answer with one; `unauthorized`: it was refused 401, so the session has ended.
 */

/** Before the first read. */
export const HOME_NOT_READ = Object.freeze({ view: null, etag: null, failed: false });

/**
 * Read Home once, from the read before it. Never throws: a failed read keeps the last document and
 * says it failed. A 401 says so too (`unauthorized`): the session has ended, whichever of the two
 * reads Central refuses first (in provider mode one of two concurrent reads can be the one that
 * finds the provider's session ended), and the snapshot read's caller signs the tab out on it.
 *
 * @param {HomeRead} previous
 * @returns {Promise<HomeRead>}
 */
export async function readHome(previous) {
  try {
    const headers = { ...CONSOLE_HEADER };
    if (previous.view !== null && previous.etag !== null) {
      headers["If-None-Match"] = previous.etag;
    }
    const response = await fetch(CONSTANTS.HOME_PATH, { headers, signal: AbortSignal.timeout(15000) });
    if (response.status === 304 && previous.view !== null) {
      return previous.failed ? Object.freeze({ view: previous.view, etag: previous.etag, failed: false }) : previous;
    }
    if (!response.ok) {
      const { view, etag } = previous;
      return Object.freeze(response.status === 401 ? { view, etag, failed: true, unauthorized: true }
        : { view, etag, failed: true });
    }
    const view = await response.json();
    if (!view?.house || !Array.isArray(view.walls) || !Array.isArray(view.overrides) || !Array.isArray(view.now_next)) {
      return Object.freeze({ view: previous.view, etag: previous.etag, failed: true });
    }
    return Object.freeze({ view, etag: response.headers.get("ETag"), failed: false });
  } catch {
    return Object.freeze({ view: previous.view, etag: previous.etag, failed: true });
  }
}
