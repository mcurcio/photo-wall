/**
 * The one home of a Source's selection words (console DDD §39; after-5 bead C5): the pieces
 * the Source flow's selection summary (`sourceFlowModel.js` `selectionWords`) and a Source's
 * state label (`mediaHealth.js` `sourceState`) are both said in, so one Source never reads
 * two ways on one screen. Dates are the library's own dates, so they read "dated", never
 * "taken". Pure, and it imports only `timeWords.js`, so the Show side that reads
 * `mediaHealth.js` reaches nothing of the Source flow through it. Nothing here names a vendor.
 */

import { captureDay } from "./timeWords.js";

/** A tag the library lists whose name has nothing visible once stripped (PR 37 §9). */
export const UNNAMED_TAG = "a tag with no visible name in your library";

/**
 * A tag in words: its path when known; "a tag with no visible name in your library" when
 * its path has nothing visible (`""`, media/models.py `TagText`); "a tag that no longer
 * exists in your library" when a lookup by id named it absent (`null`); otherwise (not
 * looked up) "a tag Photo Wall has not looked up yet". Never the library's tag id.
 *
 * @param {string} tagRef
 * @param {Readonly<Record<string, string|null>>} tagPaths id -> path, or null when gone
 * @returns {string}
 */
export function tagWords(tagRef, tagPaths) {
  if (!Object.hasOwn(tagPaths ?? {}, tagRef)) return "a tag Photo Wall has not looked up yet";
  const path = tagPaths[tagRef];
  if (path === "") return UNNAMED_TAG;
  return path ?? "a tag that no longer exists in your library";
}

/**
 * A short phrase for a Source's tags: their paths when every one is known ("tagged
 * Family/Christmas", "tagged Family and Pets"), otherwise how many ("2 tags"); null when
 * the Source has none.
 *
 * @param {string[]|undefined} tags
 * @param {Readonly<Record<string, string|null>>|null} [tagPaths]
 * @returns {string|null}
 */
export function tagCountWords(tags, tagPaths = null) {
  const list = tags ?? [];
  if (list.length === 0) return null;
  const paths = list.map((tag) => tagPaths?.[tag] ?? null);
  if (paths.every((path) => path !== null && path !== "")) return `tagged ${paths.join(" and ")}`;
  return `${list.length} tag${list.length === 1 ? "" : "s"}`;
}

/** "favourites only", "no favourites", or null when favourites do not narrow it. */
export function favouritesWords(favorites) {
  if (favorites === true) return "favourites only";
  if (favorites === false) return "no favourites";
  return null;
}

/** "photos only", "videos only", or "photos and videos". */
export function kindsWords(mediaTypes) {
  const kinds = mediaTypes ?? ["image", "video"];
  if (kinds.length === 1) return kinds[0] === "image" ? "photos only" : "videos only";
  return "photos and videos";
}

/** Whether an instant is local midnight on 1 January. */
function newYear(epochSeconds) {
  const date = new Date(epochSeconds * 1000);
  return date.getMonth() === 0 && date.getDate() === 1 && date.getHours() === 0 &&
    date.getMinutes() === 0 && date.getSeconds() === 0;
}

/**
 * A stored capture window in words ("until" is exclusive): "dated 2024" for one whole local
 * year, "dated 3 Mar 2025 to 5 Mar 2025", "dated from …", "dated before …"; null when open.
 * The last day named is the one holding the window's last included second, so a day made
 * 23 or 25 h long by DST is still whole.
 *
 * @param {number|null|undefined} from
 * @param {number|null|undefined} until
 * @returns {string|null}
 */
export function datedWords(from, until) {
  const start = from ?? null;
  const end = until ?? null;
  if (start !== null && end !== null) {
    const year = new Date(start * 1000).getFullYear();
    const wholeYear = newYear(start) && newYear(end) && new Date(end * 1000).getFullYear() === year + 1;
    return wholeYear ? `dated ${year}` : `dated ${captureDay(start)} to ${captureDay(end - 1)}`;
  }
  if (start !== null) return `dated from ${captureDay(start)}`;
  if (end !== null) return `dated before ${captureDay(end)}`;
  return null;
}

// --- What a Source's own failures say, one home for the preview and the Source card.

/**
 * The media worker's current membership ceiling (media/immich.py `_walk`): a refresh over it
 * REFUSES the whole Source (`source_limit`, status incompatible), so it selects nothing; only
 * a preview cuts the list short. The worker's current behaviour, not a product rule (§44, Q8b).
 */
export const OVER_LIMIT =
  "Photo Wall currently refuses a Source with more than 1,000 matches; saved like this it " +
  "selects nothing. Narrow it with tags or dates.";

/** A saved Source refused at that ceiling: Photo Wall's limit, never a fault in the library. */
export const OVER_LIMIT_STATE = "Over Photo Wall's current 1,000-match limit · narrow it with tags or dates";

/** The library key belongs to another library user (`owner_mismatch`). */
export const OWNER_MISMATCH = "The library key belongs to a different user.";

/** What a Source card says when a tag it selects by is no longer in the library (§39). */
export const TAG_GONE = "A tag this Source uses no longer exists in your library.";

/** A saved Source refused because a tag it uses is gone (`tag_missing`, the worker's check). */
export const TAG_MISSING_STATE = "Your photo library no longer has a tag this Source uses · edit its tags";
