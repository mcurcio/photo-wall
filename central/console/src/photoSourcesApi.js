import { apiWrite } from "./apiWrite.js";

/**
 * The Photo source editor's reads and writes (roadmap 4, slice K2): the draft, pool and check
 * routes of central/photo_library_routes.py, answered with the shapes of
 * central/photo_library/views.py (their one home). A draft is a Source not yet saved (C8): it has
 * a real check, and its photos join its pool as Immich is read, before and after saving.
 *
 * Each call returns `{ok: true, …}` or `{ok: false, error}`: a refusal answers its code
 * (`connection_unknown`, `draft_unknown`, `source_unknown`, `source_name_exists`,
 * `invalid_source_name`, `cursor_invalid`); a lost request answers `unreachable`.
 *
 * @typedef {{kind: "between", start: number|null, end: number|null}
 *   | {kind: "last", years: number, months: number}} TakenRange
 * @typedef {{albums: string[], people: string[], tags: string[], screenshots: boolean}} Exclusions
 * @typedef {{connection_ref: string, albums: string[], people: string[],
 *            place: {city: string|null, state: string|null, country: string}|null,
 *            taken: TakenRange|null, favourites_only: boolean, tags: string[],
 *            media_types: Array<"photo"|"video"|"live_photo">, partners: boolean,
 *            exclude: Exclusions}} SourceQuery
 * @typedef {{state: "never_checked"}
 *   | {state: "checking", seen_so_far: number, started_at: number, last_complete_at: number|null}
 *   | {state: "complete", at: number}
 *   | {state: "incomplete", reason: "over_1000_at_one_instant", last_complete_at: number|null}
 *   | {state: "failed", problem: "connection"|"chosen_item_gone", last_complete_at: number|null}} CheckState
 * @typedef {{asset_id: string, kind: "image"|"video", taken_at: number, width: number,
 *            height: number, thumbnail: string}} PoolItem
 * @typedef {{check: CheckState, in_pool: {photos: number, videos: number}, waiting: number,
 *            left_out: Array<{reason: "unreadable_answer"|"over_item_limits", count: number}>,
 *            immich_up_to: number|null, items: PoolItem[], next: string|null,
 *            read_at: number}} SourcePoolView
 */

const DRAFTS = "/v1/operator/photo-drafts";
const draft = (ref) => `${DRAFTS}/${encodeURIComponent(ref)}`;
const source = (ref) => `/v1/operator/sources/${encodeURIComponent(ref)}`;

async function call(key, path, options) {
  try {
    const result = await apiWrite(path, options);
    return result.ok ? { ok: true, [key]: result.data } : { ok: false, error: result.error ?? `http_${result.status}` };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

const page = (path, after) => (after ? `${path}?${new URLSearchParams({ after })}` : path);

/**
 * A new draft of `query`; `editing` names the saved Source it would replace.
 *
 * @param {SourceQuery} query
 * @param {string|null} [editing]
 * @returns {Promise<{ok: true, created: {draft_ref: string}}|{ok: false, error: string}>}
 */
export function createDraft(query, editing = null) {
  return call("created", DRAFTS, { method: "POST", body: editing ? { query, editing } : { query } });
}

/** Point a draft at a changed query (its pool is the new query's). */
export function setDraftQuery(draftRef, query) {
  return call("set", `${draft(draftRef)}/query`, { method: "PUT", body: { query } });
}

/**
 * Save a draft as the Source `name` (a new one, or the next revision of the one it edits).
 *
 * @returns {Promise<{ok: true, saved: {source_ref: string}}|{ok: false, error: string}>}
 */
export function saveDraft(draftRef, name) {
  return call("saved", `${draft(draftRef)}/save`, { method: "POST", body: { name } });
}

/** @returns {Promise<{ok: true, pool: SourcePoolView}|{ok: false, error: string}>} */
export function draftPool(draftRef, after = null) {
  return call("pool", page(`${draft(draftRef)}/pool`, after), { method: "GET" });
}

/** @returns {Promise<{ok: true, pool: SourcePoolView}|{ok: false, error: string}>} */
export function sourcePool(sourceRef, after = null) {
  return call("pool", page(`${source(sourceRef)}/pool`, after), { method: "GET" });
}

/**
 * Check now: ask for a check of a saved Source (202). Answers apiWrite's own result
 * (`useSourceCheck.js` `sourceCheckResult` reads it), a lost request as status 0.
 *
 * @param {string} sourceRef
 * @returns {Promise<{ok: boolean, status: number, error: string|null, data: any}>}
 */
export async function checkSource(sourceRef) {
  try {
    return await apiWrite(`${source(sourceRef)}/checks`, { method: "POST" });
  } catch {
    return { ok: false, status: 0, error: null, data: null };
  }
}
