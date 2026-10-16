import { useCallback, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { usePolledRead } from "./polledRead.js";

/**
 * The tuning history reads (configuration snapshots; owner, 2026-10-13: "configuration snapshots"
 * that keep "a history of what has been done in the past"): the routes of
 * central/tuning_history_routes.py, answered with the shapes of central/displays/views.py and
 * central/fleet/position_views.py (their one homes). Each returns `{ok: true, history}` or
 * `{ok: false, error}`: a refusal answers its code (`unknown_display`, `unknown_frame`, `bad_time`,
 * `bad_range`); a lost request answers `unreachable`. Every `at` is
 * UTC (ISO 8601 with its zone), PostgreSQL's clock when the entry was written.
 *
 * @typedef {import("./pictureApi.js").SoftwarePictureView} SoftwarePictureView
 * @typedef {import("./pictureApi.js").SignalRange} SignalRange
 * @typedef {import("./pictureApi.js").TestPattern} TestPattern
 * @typedef {import("./pictureApi.js").VcpCode} VcpCode
 * @typedef {{id: string, name: string}} DisplayRefView   A Display by id and its name today.
 * @typedef {{id: string, name: string}} FrameRefView   A Frame by id and its name today.
 * @typedef {"kept-done"|"kept-copied"|"kept-unrecorded"|"sent-draft"|"sent-back"|"sent-kept"} PictureEntryKind
 *   Why an entry was written: kept by Done, by Copy from another display, or found changed outside
 *   Photo Wall; sent as a session's draft (not kept), put back by Revert or a lapse, or the kept
 *   values sent anew.
 * @typedef {import("./pictureApi.js").DisplayLightView} DisplayLightView
 * @typedef {{hardware: Record<VcpCode, number>, software: SoftwarePictureView, signal_range: SignalRange,
 *            light: DisplayLightView}} PictureValuesView
 *   The hardware codes set in Photo Wall (a code absent is "as the display has it"), the Photo Wall
 *   values, the signal range and the Display's light (decision 0028).
 * @typedef {{entry_id: string, display: DisplayRefView, kind: PictureEntryKind, number: number, at: string,
 *            frame: FrameRefView|null, session_id: string|null, restored_from: string|null,
 *            picture: PictureValuesView, test_pattern: TestPattern|null}} PictureEntryView
 * @typedef {{display: DisplayRefView, unrecorded_change: boolean, entries: PictureEntryView[]}} PictureHistoryView
 *   Newest first by `at`, then number. `unrecorded_change`: the display's stored values differ from
 *   its newest kept entry.
 * @typedef {"kept-saved"|"kept-profile-changed"|"kept-unrecorded"|"sent-draft"|"sent-back"} PositionEntryKind
 *   Why a Position entry was written: kept by Done, kept when the Frame's screen size changed, found
 *   changed outside Photo Wall; a trial's draft handed to the Pi (not kept), or the kept values handed
 *   back once a draft went.
 * @typedef {"other_frame"|"made_for_other_profile"|"made_for_other_display"} PositionRestoreRefusal
 * @typedef {import("./useDraft.js").Fit} Fit
 * @typedef {import("./useDraft.js").Lighting} Lighting
 * @typedef {{profile_px: {width: number, height: number}, display_id: string|null}} MadeForView
 *   The profile an entry's corners are in, and the Display seen on the Frame's port then.
 * @typedef {{entry_id: string, frame: FrameRefView, kind: PositionEntryKind, number: number, at: string,
 *            session_id: string|null, restored_from: string|null, fit: Fit, lighting: Lighting, made_for: MadeForView,
 *            restore_refusal: PositionRestoreRefusal|null}} PositionEntryView
 *   `restore_refusal`: what Restore would answer for it on this Frame now (null: it may be restored).
 * @typedef {{frame: FrameRefView, unrecorded_change: boolean, entries: PositionEntryView[]}} PositionHistoryView
 *   Newest first by `at`, then number. `unrecorded_change`: the Frame's stored fit and lighting differ
 *   from its newest kept entry.
 */

/**
 * One GET through apiWrite (a read: the write fence does not move), folded into
 * `{ok: true, history}` or `{ok: false, error}`.
 *
 * @param {string} path
 * @param {{before?: string, limit?: number}} page
 */
async function readHistory(path, { before, limit } = {}) {
  const query = new URLSearchParams();
  if (before !== undefined) query.set("before", before);
  if (limit !== undefined) query.set("limit", String(limit));
  const search = query.toString() === "" ? "" : `?${query}`;
  try {
    const result = await apiWrite(`${path}${search}`, { method: "GET" });
    return result.ok ? { ok: true, history: result.data } : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

/**
 * One Display's picture history, newest first (GET …/displays/{id}/picture-history). `before`: an
 * entry's `at` (exclusive), for the page after it; `limit` 1..500 (Central's default 100).
 *
 * @param {string} displayId
 * @param {{before?: string, limit?: number}} [page]
 * @returns {Promise<{ok: true, history: PictureHistoryView}|{ok: false, error: string}>}
 */
export function readPictureHistory(displayId, { before, limit } = {}) {
  return readHistory(`/v1/operator/displays/${encodeURIComponent(displayId)}/picture-history`, { before, limit });
}

/**
 * One Frame's Position history (fit, corners, rotation and lighting), newest first (GET
 * …/frames/{id}/position-history); `before` and `limit` as `readPictureHistory`.
 *
 * @param {string} frameId
 * @param {{before?: string, limit?: number}} [page]
 * @returns {Promise<{ok: true, history: PositionHistoryView}|{ok: false, error: string}>}
 */
export function readPositionHistory(frameId, { before, limit } = {}) {
  return readHistory(`/v1/operator/frames/${encodeURIComponent(frameId)}/position-history`, { before, limit });
}

/** How often a History is read while its tab is shown. */
export const HISTORY_CADENCE_MS = 2000;
/** Entries per read: the newest page, and each "Show older". */
export const HISTORY_PAGE = 50;

/**
 * @template H
 * @typedef {{history: H|null, error: string|null, full: boolean}|null} HistoryRead
 */

/**
 * One History as a tab shows it: the newest page read every HISTORY_CADENCE_MS while the browser
 * tab is shown and on `refresh`, and older pages loaded on request (`older`), each the page before
 * the last entry shown. A failed read keeps the last history beside its code (`error`). `more`: the
 * last page read was full, so an older one may exist. A poll refreshes the newest page only; a
 * consumer shows `history.entries` then `olderEntries`, skipping any already shown.
 *
 * @template {{entries: {entry_id: string, at: string}[]}} H
 * @param {(page: {before?: string, limit: number}) => Promise<{ok: true, history: H}|{ok: false, error: string}>} read
 *   a stable callback (useCallback)
 * @returns {{history: H|null, error: string|null, loading: boolean, more: boolean,
 *            olderEntries: H["entries"], older: () => Promise<void>, refresh: () => Promise<void>}}
 */
export function useHistoryPages(read) {
  const load = useCallback(async (previous) => {
    const result = await read({ limit: HISTORY_PAGE });
    return result.ok ? { history: result.history, error: null, full: result.history.entries.length === HISTORY_PAGE }
      : { history: previous?.history ?? null, error: result.error, full: previous?.full ?? false };
  }, [read]);
  const { value, refresh } = usePolledRead(load, { cadenceMs: HISTORY_CADENCE_MS, initial: /** @type {HistoryRead<H>} */ (null) });
  const [older, setOlder] = useState(/** @type {{entries: H["entries"], full: boolean}|null} */ (null));
  const [loadingOlder, setLoadingOlder] = useState(false);
  const shown = [...(value?.history?.entries ?? []), ...(older?.entries ?? [])];
  const last = shown.at(-1);
  const more = older === null ? value?.full === true : older.full;
  const loadOlder = async () => {
    if (last === undefined || loadingOlder) return;
    setLoadingOlder(true);
    const result = await read({ before: last.at, limit: HISTORY_PAGE });
    setLoadingOlder(false);
    if (result.ok) {
      setOlder({ entries: [...(older?.entries ?? []), ...result.history.entries], full: result.history.entries.length === HISTORY_PAGE });
    }
  };
  return {
    history: value?.history ?? null, error: value?.error ?? null, loading: value === null || loadingOlder, more,
    olderEntries: older?.entries ?? [], older: loadOlder, refresh,
  };
}
