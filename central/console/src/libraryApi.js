import { useCallback, useMemo } from "react";

import { apiWrite } from "./apiWrite.js";
import { usePolledRead } from "./polledRead.js";

/**
 * Settings › Photo library's reads and writes (roadmap 4, slice K1): the routes of
 * central/photo_library_routes.py, answered with the shapes of central/photo_library/views.py
 * (their one home). Each call returns `{ok: true, …}` or `{ok: false, error}`: a refusal answers
 * its code (`connection_unknown`, `connection_name_exists`, `invalid_url`, `key_required`,
 * `sources_use_connection`); a lost request answers `unreachable`. The key is sent by add and
 * change and is never answered: a row carries only `key_hint` ("…1234").
 *
 * @typedef {{user_ref: string, name: string, email: string}} ImmichUser
 * @typedef {{photos: number, videos: number, albums: number, people: number}} LibraryCounts
 * @typedef {{kind: "key_rejected"} | {kind: "missing_permissions", missing: string[]}
 *   | {kind: "immich_too_old", found: string, oldest: string} | {kind: "unreachable"}
 *   | {kind: "unexpected_answer", detail: string}
 *   | {kind: "other_user", expected: string, found: string}} LibraryProblem
 * @typedef {{kind: "untested_version", found: string} | {kind: "no_full_size_copies"}} LibraryNote
 * @typedef {{state: "not_checked"} | {state: "ok", checked_at: number, notes: LibraryNote[]}
 *   | {state: "needs_a_look", problem: LibraryProblem, since: number, checked_at: number}} LibraryHealth
 * @typedef {{connection_ref: string, name: string, url: string|null, key_hint: string|null,
 *            immich_user: ImmichUser|null, health: LibraryHealth, counts: LibraryCounts|null,
 *            required_permissions: string[]}} LibraryConnectionView
 * @typedef {{name: string, url: string, key?: string}} ConnectionWrite
 * @typedef {{album_ref: string, name: string, photos: number}} LibraryAlbum
 * @typedef {{person_ref: string, name: string}} LibraryPerson
 * @typedef {{city: string|null, state: string|null, country: string}} Place
 * @typedef {{tag_ref: string, path: string, name: string, parent_ref: string|null}} LibraryTag
 * @template Item
 * @typedef {{connection_ref: string, status: string, observed_at: number|null, read_at: number,
 *            total_matches: number, items: Item[], absent: string[]}} ListAnswer
 */

const LIBRARY = "/v1/operator/library";
const CONNECTIONS = `${LIBRARY}/connections`;
const connection = (ref) => `${CONNECTIONS}/${encodeURIComponent(ref)}`;

/**
 * One call through apiWrite, its answer folded into the shape above under `key`.
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

/** @returns {Promise<{ok: true, connections: LibraryConnectionView[]}|{ok: false, error: string}>} */
export function listConnections() {
  return call("connections", CONNECTIONS, { method: "GET" });
}

/**
 * @param {{name: string, url: string, key: string}} write
 * @returns {Promise<{ok: true, saved: {connection_ref: string}}|{ok: false, error: string}>}
 */
export function addConnection({ name, url, key }) {
  return call("saved", CONNECTIONS, { method: "POST", body: { name, url, key } });
}

/**
 * Change a connection's name and address; a key only when one is pasted (none keeps the stored
 * key; a cut-over row takes its address and key together, else `key_required`).
 *
 * @param {string} ref
 * @param {ConnectionWrite} write
 */
export function changeConnection(ref, { name, url, key }) {
  const body = key ? { name, url, key } : { name, url };
  return call("saved", connection(ref), { method: "PUT", body });
}

/** @param {string} ref refused with `sources_use_connection` while a Source uses it */
export function removeConnection(ref) {
  return call("removed", connection(ref), { method: "DELETE" });
}

/** Check now: queues a connection check; its answer lands on the row's health. */
export function checkConnection(ref) {
  return call("requested", `${connection(ref)}/checks`, { method: "POST" });
}

/** A picker read: `q` searches; `ids` names exactly these items (never both). */
function list(kind, ref, { q, ids }) {
  const params = new URLSearchParams({ connection: ref });
  if (ids !== undefined) {
    for (const id of ids) params.append("ids", id);
  } else {
    params.set("q", q ?? "");
  }
  return call("answer", `${LIBRARY}/${kind}?${params}`, { method: "GET" });
}

/** @returns {Promise<{ok: true, answer: ListAnswer<LibraryAlbum>}|{ok: false, error: string}>} */
export const searchAlbums = (ref, q) => list("albums", ref, { q });
export const albumsById = (ref, ids) => list("albums", ref, { ids });
/** @returns {Promise<{ok: true, answer: ListAnswer<LibraryPerson>}|{ok: false, error: string}>} */
export const searchPeople = (ref, q) => list("people", ref, { q });
export const peopleById = (ref, ids) => list("people", ref, { ids });
/** @returns {Promise<{ok: true, answer: ListAnswer<Place>}|{ok: false, error: string}>} */
export const searchPlaces = (ref, q) => list("places", ref, { q });
/** @returns {Promise<{ok: true, answer: ListAnswer<LibraryTag>}|{ok: false, error: string}>} */
export const searchTags = (ref, q) => list("tags", ref, { q });
export const tagsById = (ref, ids) => list("tags", ref, { ids });

const NOT_READ = Object.freeze({ connections: null, failed: false });

/**
 * The connections, re-read while the page that shows them is mounted: every `cadenceMs`, and at
 * once after a write (`refresh`). A failed read keeps the last rows and says it failed.
 *
 * @param {{cadenceMs: number, skip?: boolean}} options
 * @returns {{connections: LibraryConnectionView[]|null, failed: boolean, busy: boolean,
 *            refresh: () => Promise<void>}}
 */
export function useLibraryConnections({ cadenceMs, skip = false }) {
  const load = useCallback(async (current) => {
    const result = await listConnections();
    return result.ok
      ? Object.freeze({ connections: result.connections, failed: false })
      : Object.freeze({ connections: current.connections, failed: true });
  }, []);
  const { value, busy, refresh } = usePolledRead(load, { cadenceMs, skip, initial: NOT_READ });
  return useMemo(() => ({ ...value, busy, refresh }), [value, busy, refresh]);
}
