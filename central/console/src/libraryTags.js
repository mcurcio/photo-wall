import { apiWrite } from "./apiWrite.js";
import { fact, words } from "./facts.js";
import { KEY_NOT_ALLOWED } from "./sourcePreview.js";

/**
 * The library's tags as the Source flow uses them (console DDD §37, §39, §40; PR 37 §8).
 * The rules are pure and run under Node (tests/test_console_sources.py); `readTags` is the
 * one read of `GET /v1/operator/library/tags`, which serves the list the media worker
 * stored (it asks the library nothing).
 *
 * A served tag is `{tag_ref, path, name, parent_ref}` (B5-L2-3). A Source's tags are ALL
 * required, and each includes everything nested under it, so a tag and one nested under it
 * select exactly what the nested one selects: choosing the nested one replaces its
 * ancestor, and an ancestor of a chosen tag is refused with that reason.
 *
 * @typedef {{tag_ref: string, path: string, name: string, parent_ref?: string|null}} Tag
 * @typedef {{status: string, error?: string, observed_at: number|null, read_at: number,
 *            total_matches: number, tags: Tag[], connection_ref: string}} TagList
 */

/** A Source takes at most this many tags (media/models.py `SourceQuery.tags`). */
export const MAX_TAGS = 4;
/** A tag list older than this is worth an age fact (§39: the worker re-lists every 5 min). */
export const TAG_LIST_STALE_SECONDS = 300;

/** Whether tag path `outer` contains tag path `inner` (strictly). */
export function nestedUnder(inner, outer) {
  return typeof inner === "string" && typeof outer === "string" && inner.startsWith(`${outer}/`);
}

/**
 * Add `tag` to the chosen ids (§37). Returns the next ids and what to say:
 *   - already chosen: unchanged;
 *   - an ancestor of a chosen tag: refused, with its reason;
 *   - nested under chosen tags: it replaces them;
 *   - at {@link MAX_TAGS}: refused.
 *
 * @param {string[]} chosen tag ids
 * @param {Tag} tag
 * @param {Readonly<Record<string, string|null>>} paths id -> path of what is known
 * @returns {{chosen: string[], refused: string|null, replaced: string[]}}
 */
export function chooseTag(chosen, tag, paths) {
  if (chosen.includes(tag.tag_ref)) return { chosen, refused: null, replaced: [] };
  const inside = chosen.find((ref) => nestedUnder(paths[ref], tag.path));
  if (inside !== undefined) {
    return {
      chosen,
      refused: `${tag.path} is not added: ${paths[inside]} is already chosen and is nested under it, ` +
        `so adding ${tag.path} would change nothing.`,
      replaced: [],
    };
  }
  const replaced = chosen.filter((ref) => nestedUnder(tag.path, paths[ref]));
  const kept = chosen.filter((ref) => !replaced.includes(ref));
  if (kept.length >= MAX_TAGS) {
    return { chosen, refused: `A Source takes at most ${MAX_TAGS} tags. Remove one to choose another.`, replaced: [] };
  }
  return { chosen: [...kept, tag.tag_ref], refused: null, replaced };
}

/**
 * The paths a served list teaches, merged into `known` (id -> path). Only a lookup by id's
 * `absent` ids are gone (null): Central names them against the library's whole stored list,
 * which keeps every listed id. A search never proves a tag gone, even an unfiltered one:
 * it hides tags with nothing visible to pick them by (B5-FC1).
 *
 * @param {Readonly<Record<string, string|null>>} known
 * @param {TagList|null} list
 * @returns {Record<string, string|null>}
 */
export function learnPaths(known, list) {
  const next = { ...known };
  for (const tag of list?.tags ?? []) next[tag.tag_ref] = tag.path;
  // A lookup by id (`readTagsById`) names what an `ok` list lacks; Central names nothing
  // absent from a pending or failed list, so this is never a guess.
  for (const ref of list?.absent ?? []) next[ref] = null;
  return next;
}

/** What the picker says while the library has not listed its tags. */
export const TAGS_PENDING = "Your photo library has not reported its tags yet.";
/** Why the picker cannot say which tags match when a read failed. */
export const TAGS_UNREAD = "Photo Wall could not read your library's tags; it asks again as you type";

/**
 * What the picker announces to a screen reader for a typed search (R25): the count only for
 * an `ok` list read by this search; otherwise the same sentence the screen shows, so a
 * failure or an unlisted library never reads as "No tags match".
 *
 * @param {TagList|null} list the newest list answered
 * @param {string|null} failed the newest read's failure, when it failed
 * @returns {string}
 */
export function pickerAnnouncement(list, failed) {
  if (failed !== null) return `${TAGS_UNREAD}.`;
  if (list === null) return "";
  if (list.status === "pending") return TAGS_PENDING;
  if (list.status === "permission") return KEY_NOT_ALLOWED;
  if (list.status !== "ok" && list.observed_at == null) {
    return `Your photo library has not reported its tags (${words(list.error ?? list.status)}).`;
  }
  const shown = list.tags?.length ?? 0;
  const total = list.total_matches ?? 0;
  if (total > shown) return `${shown} of ${total} tags; keep typing`;
  return total === 0 ? "No tags match" : `${total} ${total === 1 ? "tag" : "tags"}`;
}

/**
 * The tag list's age as a `reported` fact (§39), only when it is older than
 * {@link TAG_LIST_STALE_SECONDS}; an unread list with a failure is `unknown`. Null when
 * nothing needs saying.
 *
 * @param {TagList|null} list
 * @returns {import("./facts.js").Fact|null}
 */
export function tagListFact(list) {
  if (list == null || list.status === "pending" || list.status === "permission") return null;
  if (list.observed_at == null) {
    return list.status === "ok" ? null
      : fact({ kind: "unknown", why: `your photo library has not reported its tags (${words(list.error ?? list.status)})` });
  }
  if (!(list.read_at - list.observed_at > TAG_LIST_STALE_SECONDS)) return null;
  return fact({
    kind: "reported",
    source: "Your photo library",
    receipt: "latest",
    value: null,
    receivedAt: list.observed_at,
    readAt: list.read_at,
    field: "the time of your photo library's tag list",
  });
}

/**
 * Read the stored tags of `connection` matching `q` (at most 20). Never throws: a refusal
 * or a lost request answers `{failed: code}`.
 *
 * @param {string} connection
 * @param {string} q
 * @returns {Promise<TagList|{failed: string}>}
 */
export async function readTags(connection, q = "") {
  try {
    const result = await apiWrite(
      `/v1/operator/library/tags?connection=${encodeURIComponent(connection)}&q=${encodeURIComponent(q)}`,
      { method: "GET" },
    );
    return result.ok ? result.data : { failed: result.error ?? `http_${result.status}` };
  } catch {
    return { failed: "unreachable" };
  }
}

/** At most this many ids per lookup (`GET …/library/tags?ids=`, central/library_routes.py). */
const IDS_PER_LOOKUP = MAX_TAGS;

/**
 * Look up tags by id in `connection`'s stored list (C5), however long that list is: one
 * read per {@link IDS_PER_LOOKUP} ids. Each answer carries the named tags and `absent`
 * (ids an `ok` list lacks). A refused or lost read is left out, so its ids stay unknown.
 *
 * @param {string} connection
 * @param {string[]} ids canonical tag ids
 * @returns {Promise<TagList[]>}
 */
export async function readTagsById(connection, ids) {
  const unique = [...new Set(ids)];
  const reads = [];
  for (let start = 0; start < unique.length; start += IDS_PER_LOOKUP) {
    const query = unique.slice(start, start + IDS_PER_LOOKUP)
      .map((id) => `&ids=${encodeURIComponent(id)}`).join("");
    reads.push(apiWrite(`/v1/operator/library/tags?connection=${encodeURIComponent(connection)}${query}`,
      { method: "GET" }).then((result) => (result.ok ? result.data : null), () => null));
  }
  return (await Promise.all(reads)).filter((answer) => answer !== null);
}
