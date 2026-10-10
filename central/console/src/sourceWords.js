/**
 * The one home of a Source's selection words (console DDD §39; after-5 bead C5): the pieces
 * the Source flow's selection summary (`sourceFlowModel.js` `selectionWords`) and a Source's
 * state label (`mediaHealth.js` `sourceState`) are both said in, so one Source never reads
 * two ways on one screen. Dates are the library's own dates, so they read "dated", never
 * "taken". Pure, and it imports only `timeWords.js` and `facts.js`, so the Show side that
 * reads `mediaHealth.js` reaches nothing of the Source flow through it.
 *
 * Who refused (owner, 2026-10-09: "it depends where the error is coming from. If Immich
 * rejects the grants on the API key, then its an 'immich' error. But if photo wall is
 * rejecting sources that produce more than 1000 photos, then its a 'photo wall' error."):
 * every failure's words name their origin, the library by its provider
 * ({@link LIBRARY_PROVIDER}, the one place the vendor's name is written) or Photo Wall. The
 * origin is each code's row owner in {@link SOURCE_REFUSALS}, never a sentence's choice.
 * Selection and report words stay neutral ("your photo library").
 */

import { fact, words } from "./facts.js";
import { captureDay } from "./timeWords.js";

const LIBRARY_NAME = "Your photo library";

/** "Your photo library", naming the connection when more than one is announced (§39). */
export function libraryName(connection, connections) {
  return (connections?.length ?? 0) > 1 && typeof connection === "string" && connection !== ""
    ? `${LIBRARY_NAME} (connection ${connection})` : LIBRARY_NAME;
}

/**
 * When a Source last refreshed successfully, as the photo library's `reported` fact (§39):
 * "Your photo library last reported 2 min ago · the media worker accepted 132 in that
 * refresh". The one home of a Source's refresh, read by its state label (`mediaHealth.js`
 * `sourceState`) and so by its card, so one Source never states its refresh twice. The count
 * is the worker's own acceptance, never the library's report, so it names the worker. Its
 * age is the database's read time minus the database's write time (`media.read_at`, G11).
 *
 * @param {object} source a `/v1/operator/media` `sources` row
 * @param {number} now `media.read_at`
 * @param {string[]} [connections] the announced connections
 */
export function refreshFact(source, now, connections = []) {
  return fact({
    kind: "reported",
    source: libraryName(source.spec?.connection_ref, connections),
    receipt: "latest",
    value: `the media worker accepted ${Number(source.counts?.valid ?? 0)} in that refresh`,
    receivedAt: source.last_success,
    readAt: now,
    field: "the time of the last refresh",
  });
}

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

/** What an untagged Source takes, before {@link TIMELINE_ONLY} narrows it. */
export const UNTAGGED_SELECTION = "everything";

/**
 * What every Source is narrowed to, tagged or not: the media worker's search asks for the
 * library's timeline only (media/immich.py `_walk`, `_head`), and drops archived, hidden,
 * locked, trashed, offline and other users' media, so a selection without it would claim
 * more than it selects. Said once per selection, after its other parts.
 */
export const TIMELINE_ONLY = "on your library's timeline only (not archived, hidden or other users' media)";

// --- What a Source's own failures say, one home for the preview, the Source card and the
// --- media pipeline.

/**
 * The photo library's provider: the media worker's one adapter (media/immich.py). The console
 * is not told a connection's kind, so this is the one place the vendor's name is written
 * (tests/test_console_sources.py's scan allows it here only).
 */
export const LIBRARY_PROVIDER = "Immich";

/**
 * Who produced a refusal (§39, R21: the library is the origin only of what it reports).
 * `LIBRARY`: the photo library answered it. `PHOTO_WALL`: Central or its media worker
 * produced it (its limits, its configuration, its version), never worded as the library's.
 */
export const LIBRARY = "library";
export const PHOTO_WALL = "photo-wall";

/** Each origin's name, as a failure's words say it. */
export const ORIGIN_NAMES = Object.freeze({ [LIBRARY]: LIBRARY_PROVIDER, [PHOTO_WALL]: "Photo Wall" });

/**
 * A preview that counted more than 1,000 matches (`limited`, media/immich.py `_walk`'s
 * count ceiling): a refresh over it REFUSES the whole Source (`source_limit`, status
 * incompatible), so it selects nothing; only a preview cuts the list short. The worker's
 * current behaviour, not a product rule (§44, Q8b). Said only where the walk counted more.
 */
export const OVER_LIMIT =
  "Photo Wall currently refuses a Source with more than 1,000 matches; saved like this it " +
  "selects nothing. Narrow it with tags or dates.";

/**
 * A row: its owner, its state label, the Source card's sentence where it differs (`issue`),
 * and what a preview does with it (`preview`): "retry" for the library's transient answers
 * or Photo Wall's (the panel says who, in the row's words, and asks again; "can't reach" the
 * library only for the library's own), "key" for the library refusing the key; otherwise
 * the preview fails with the row's own words. `{origin}` in its words is its owner's name
 * ({@link ORIGIN_NAMES}); a row whose words do not name its owner throws as the module loads,
 * so no failure can be worded without saying who refused.
 */
const row = (owner) => (state, issue = undefined, preview = undefined) => {
  const named = (text) => {
    if (text === undefined) return undefined;
    if (!text.includes("{origin}")) throw new Error(`refusal words must name their origin: ${text}`);
    return text.replaceAll("{origin}", ORIGIN_NAMES[owner]);
  };
  return Object.freeze({ owner, state: named(state), issue: named(issue), preview });
};
const library = row(LIBRARY);
const photoWall = row(PHOTO_WALL);

const UNREACHABLE = library("{origin} is unreachable", undefined, "retry");
const REFUSED_ACCESS = library("{origin} refused access",
  `{origin} refused access. Check the permissions of Photo Wall's ${LIBRARY_PROVIDER} key.`, "key");
const UNREADABLE = library("{origin} sent an answer Photo Wall can't read");
const ITEM = (state) => library(state);
const CONNECTION = photoWall(
  "{origin}'s media worker has invalid library connection settings · check its configuration");
const WORKER_FAILED = photoWall("{origin}'s media worker failed during the refresh · check its logs");

/**
 * The one closed table from a served refusal code to its owner and words: `state` is the
 * Source's state label, `issue` (when it differs) the Source card's sentence. Every code a
 * refresh can record is here (tests/test_console_sources.py harvests them from
 * media/immich.py, media/worker.py and central/media_repository.py); an unknown code reads
 * neutrally ({@link refusalState}), never as the library's fault.
 */
export const SOURCE_REFUSALS = Object.freeze({
  // The library's answers.
  upstream_unavailable: UNREACHABLE,
  upstream_timeout: UNREACHABLE,
  upstream_permission: REFUSED_ACCESS,
  asset_permission: REFUSED_ACCESS,
  upstream_schema: UNREADABLE,
  upstream_pagination: UNREADABLE,
  upstream_encoding: UNREADABLE,
  upstream_integrity: UNREADABLE,
  upstream_redirect: library("{origin} redirected Photo Wall's request"),
  tag_missing: library("{origin} no longer has a tag this Source uses · edit its tags",
    "A tag this Source uses no longer exists in {origin}."),
  metadata_invalid: ITEM("{origin} sent an item Photo Wall can't read"),
  metadata_mismatch: ITEM("{origin} sent an item that doesn't match its listing"),
  metadata_pending_or_changed: ITEM("{origin}'s item details are still settling"),
  asset_missing: ITEM("{origin} no longer has an item"),
  asset_unavailable: ITEM("{origin} could not serve an item"),
  asset_changed: ITEM("An item changed in {origin} while it was fetched"),
  asset_integrity: ITEM("An item from {origin} arrived damaged"),
  unsupported_media: ITEM("{origin} has an item Photo Wall can't show"),
  unsupported_color: ITEM("{origin} has an item in colours Photo Wall can't show"),
  thumbnail_unsupported: ITEM("{origin} sent a thumbnail Photo Wall can't show"),
  thumbnail_invalid: ITEM("{origin} sent a thumbnail Photo Wall can't read"),
  thumbnail_not_ready: ITEM("{origin}'s thumbnail isn't ready yet"),
  // Photo Wall's own: its limits, its configuration, its version, its worker.
  // Its supported-version list (media/immich.py `_SUPPORTED_VERSIONS`), not the library's.
  unsupported_version: photoWall(
    `This {origin} release doesn't support your ${LIBRARY_PROVIDER} version · check the supported versions`),
  // Every item a refresh found was rejected or still pending; each item's own code says why
  // (the library's answer, or Photo Wall's limits), so this Source-level row blames no item.
  metadata_pending_or_invalid: photoWall("{origin} could use no item this Source found · see each item's reason",
    "{origin} could use no item this Source found; each item's reason follows."),
  // A well-formed item over Photo Wall's dimension, pixel or video-length limits.
  item_over_limits: photoWall("An item is over {origin}'s current size or length limits"),
  // Photo Wall's own time limit for one whole refresh, preview or tag listing.
  time_budget: photoWall("Over {origin}'s current time limit for one refresh · narrow it with tags or dates"),
  // Written by Central when a pending preview passes its expiry with no answer from the
  // worker (central/media_repository.py): the worker never answered, the library is not named.
  preview_expired: photoWall("{origin}'s media worker hasn't answered this preview · check that it is running",
    undefined, "retry"),
  // The worker raises it for its match ceiling and for its per-refresh byte and request
  // bounds alike, so it never claims the count alone.
  source_limit: photoWall(
    "Over {origin}'s current size limits for one Source (at most 1,000 matches) · narrow it with tags or dates",
    "{origin} currently refuses a Source this large (at most 1,000 matches, within its size " +
    "limits); saved like this it selects nothing. Narrow it with tags or dates."),
  tag_limit: photoWall("Over {origin}'s current limits on library tags"),
  asset_oversize: photoWall("An item is over {origin}'s current size limit"),
  thumbnail_oversize: photoWall("A thumbnail is over {origin}'s current size limit"),
  preparation_limit: photoWall("An item is over {origin}'s current preparation limits"),
  preparation_invalid: photoWall("{origin} could not prepare an item"),
  preparation_io: WORKER_FAILED,
  preparation_timeout: photoWall("{origin} ran out of time preparing an item"),
  preparation_tool_missing: photoWall("{origin}'s media worker is missing a preparation tool · check its install"),
  preparation_build_changed: photoWall("{origin}'s media worker's preparation changed while it ran"),
  recipe_changed: photoWall("{origin}'s preparation recipe changed while it ran"),
  destination_exists: WORKER_FAILED,
  staging_io: WORKER_FAILED,
  staging_cleanup: WORKER_FAILED,
  spec_unsupported: photoWall(
    "{origin}'s media worker can't read this Source's settings · update the media worker",
    "{origin}'s media worker is older than the Source's settings. Update the media worker."),
  connection_mismatch: photoWall(
    "{origin}'s media worker has a library connection that doesn't match this Source · check the worker's connections"),
  connection_unknown: photoWall(
    "{origin}'s media worker has no library connection for this Source · check the worker's connections",
    "{origin}'s media worker has no such connection configured."),
  connection_config: CONNECTION,
  connection_file: CONNECTION,
  worker_config: photoWall("{origin}'s media worker settings are invalid · check its configuration"),
  owner_mismatch: photoWall(
    `{origin} refused its ${LIBRARY_PROVIDER} key: it belongs to a different user · check the worker's library key`,
    `{origin} refused its ${LIBRARY_PROVIDER} key: it belongs to a different ${LIBRARY_PROVIDER} user.`),
  worker_timeout: photoWall("{origin}'s media worker ran out of time refreshing", undefined, "retry"),
  worker_cancelled: photoWall("{origin}'s media worker stopped during the refresh", undefined, "retry"),
  worker_exited: WORKER_FAILED,
  completion_not_recorded: WORKER_FAILED,
  worker_io: WORKER_FAILED,
  worker_internal: WORKER_FAILED,
  clock_invalid: photoWall("The clock of {origin}'s media worker is invalid · check its host"),
});

/** What a Source card says when a tag it selects by is no longer in the library (§39). */
export const TAG_GONE = SOURCE_REFUSALS.tag_missing.issue;

/** The origin's name of a served refusal code ("Immich", "Photo Wall"), or null when unknown. */
export function refusalOrigin(code) {
  return code && Object.hasOwn(SOURCE_REFUSALS, code) ? ORIGIN_NAMES[SOURCE_REFUSALS[code].owner] : null;
}

/**
 * The connection's stored tag list was refused for its key (`library_tags` status
 * `permission`): the library refused it, so it names the library. The picker cannot list
 * tags, so a Source saved meanwhile has none. Names the permission the media docs list for
 * the tag list (docs/module-media.md, `GET /tags`).
 */
export const TAGS_KEY_MISSING =
  `${ORIGIN_NAMES[LIBRARY]} refused the tag list: Photo Wall's ${LIBRARY_PROVIDER} key is missing ` +
  "the tag.read permission, so tags can't be picked until it is added (the setup guide's " +
  "library key step).";

/**
 * What a refused Source alert says: its title (who refused, and what) and its lines (why,
 * and the one fix). `source_limit` names the filters the Source actually has, so "too large"
 * is never a mystery, and when the connection's stored tag list is refused for its key
 * ({@link TAGS_KEY_MISSING}) says why tags could not narrow it. Any other code says its
 * row's state and, where it differs, the row's sentence; both name the row's origin.
 *
 * @param {string|null|undefined} code the Source-level refusal code
 * @param {string|null|undefined} status the Source's status, said only when there is no code
 * @param {string[]} filters the Source's filters in words (mediaHealth.js `sourceFilters`)
 * @param {string|null} tagListStatus the connection's stored tag-list status, when read
 * @returns {{title: string, lines: string[]}}
 */
export function refusalProblem(code, status, filters, tagListStatus = null) {
  if (code === "source_limit") {
    const why = filters.length === 0
      ? `It has no tags and no dates, so it asks for your whole ${LIBRARY_PROVIDER} library.`
      : `Its only filters are ${filters.join(", ")}, and that still matches too much.`;
    const keyMissing = tagListStatus === "permission";
    return {
      title: `${refusalOrigin(code)} refused this Source as too large (its current limit is at most ` +
        "1,000 matches), so it selects nothing",
      lines: [
        why,
        ...(keyMissing
          ? [TAGS_KEY_MISSING, "Until then, narrow it with dates: edit it in Sources."]
          : ["Narrow it with tags or dates: edit it in Sources."]),
      ],
    };
  }
  const title = refusalState(code, status);
  const issue = code ? refusalIssue(code) : null;
  return { title, lines: issue && issue !== title ? [issue] : [] };
}

/**
 * A failing Source's state words from its refusal code; with no code, or a code the table
 * does not hold, a neutral "Refresh failed (…)" that blames no one.
 *
 * @param {string|null|undefined} code the refusal's diagnostic code
 * @param {string|null|undefined} status the Source's status, said only when there is no code
 * @returns {string}
 */
export function refusalState(code, status = null) {
  if (code && Object.hasOwn(SOURCE_REFUSALS, code)) return SOURCE_REFUSALS[code].state;
  return `Refresh failed (${words(code || status || "unknown")})`;
}

/**
 * A Source card's sentence for a reported code: the row's own sentence where it differs, else
 * its state words, so the card's Status and Issue never name two owners; the code in words
 * only for a code the table does not hold.
 */
export function refusalIssue(code) {
  if (!code || !Object.hasOwn(SOURCE_REFUSALS, code)) return words(code);
  const entry = SOURCE_REFUSALS[code];
  return entry.issue ?? entry.state;
}

/**
 * What a preview does with a served failure code: "retry" and "key" from its row, otherwise
 * "failed" (its row's words, or the code in words for a code the table does not hold).
 *
 * @param {string|null|undefined} code
 * @returns {"retry"|"key"|"failed"}
 */
export function previewHandling(code) {
  return (code && Object.hasOwn(SOURCE_REFUSALS, code) && SOURCE_REFUSALS[code].preview) || "failed";
}
