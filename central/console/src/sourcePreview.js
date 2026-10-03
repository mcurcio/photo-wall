import { fact, words as codeWords } from "./facts.js";
import {
  libraryName,
  LIBRARY,
  OVER_LIMIT,
  previewHandling,
  refusalIssue,
  SOURCE_REFUSALS,
} from "./sourceWords.js";
import { captureDay } from "./timeWords.js";

/**
 * What a Source draft selects, as the photo library reported it (console DDD §39, §40). Pure:
 * no React; usePreview.js runs the requests and tests/test_console_sources.py drives these
 * under Node.
 *
 * A PREVIEW is the panel's state (usePreview.js): `phase` is one of
 *   idle         nothing asked (no announced connection, or the step shows no panel)
 *   looking      a request is in flight; `stillLooking` after {@link STILL_LOOKING_MS}
 *   complete     `answer` is the served `GET /v1/operator/source-previews/{id}` body
 *   retrying     a transient failure, `code` names it; the panel asks again. Its owner and
 *                words are the code's row in sourceWords.js `SOURCE_REFUSALS`, so a stopped
 *                worker never reads as the library being unreachable
 *   key          the library refused the connection's key
 *   failed       another failure, `code` names it (its row's words when it has one)
 * and `previous` is the last complete answer of an earlier request (criteria change
 * supersede a request, but its answer stays on screen beside "Updating…" or a failure).
 * The age of an answer is the database's read time (`read_at`) minus the database's
 * answer time (`observed_at`): one clock (G11).
 *
 * @typedef {{count: number, image_count: number, video_count: number, limited?: boolean,
 *            shown?: Array<{asset_id: string, kind: string, captured_at: number,
 *            duration_seconds?: number|null}>, observed_at?: number, read_at?: number}} Answer
 * @typedef {{phase: "idle"|"looking"|"complete"|"retrying"|"key"|"failed",
 *            answer: Answer|null, previous: Answer|null, stillLooking?: boolean,
 *            code?: string|null, connection?: string}} Preview
 */

/** The panel polls a request at 2 s, doubling to at most 30 s (§40). */
export const FIRST_POLL_MS = 2000;
export const LONGEST_POLL_MS = 30000;
/** After this long without an answer, the panel says it is still looking (§40). */
export const STILL_LOOKING_MS = 120000;
/** A criteria change waits this long for the next before it asks (one request per pause). */
export const SETTLE_MS = 400;
/** The newest members a preview serves (the worker's sample). */
export const SHOWN_LIMIT = 24;

/**
 * The wait before poll number `attempt` (0-based): 2 s, 4 s, 8 s, 16 s, then 30 s.
 *
 * @param {number} attempt
 * @returns {number}
 */
export function pollDelay(attempt) {
  return Math.min(LONGEST_POLL_MS, FIRST_POLL_MS * 2 ** Math.max(0, attempt));
}

/**
 * Which phase a served failure code puts the panel in, read from the code's one row
 * (sourceWords.js `SOURCE_REFUSALS`): "retrying" for a transient code (the library's or
 * Photo Wall's), "key" for the library refusing the key, otherwise "failed". `owner_mismatch`
 * is not "key": more permissions cannot fix a key that is another user's.
 *
 * @param {string|null|undefined} code
 * @returns {"retrying"|"key"|"failed"}
 */
export function failurePhase(code) {
  const handling = previewHandling(code);
  return handling === "retry" ? "retrying" : handling;
}

export { libraryName };

export const CANT_REACH = "Photo Wall can't reach your photo library right now.";
export const KEY_NOT_ALLOWED =
  "Your library connection's key isn't allowed to list tags or show previews. Add the " +
  "permissions in the setup guide's library key step.";
export const LOOKING = "Looking…";
export const STILL_LOOKING = "Still looking. Photo Wall will keep trying.";
export const UPDATING = "Updating…";

/**
 * What the panel says (§39): its facts, then its plain statements, in that order. A failure
 * never reads as "nothing matches": with no earlier answer it is `unknown`; with one, the
 * earlier answer stays, followed by what went wrong.
 *
 * @param {Preview} preview
 * @param {string[]} connections the announced connections
 * @returns {{facts: import("./facts.js").Fact[], notes: string[], answer: Answer|null}}
 *   `answer` is the answer on screen (its tiles are shown)
 */
export function previewFacts(preview, connections = []) {
  const source = libraryName(preview?.connection, connections);
  const phase = preview?.phase ?? "idle";
  const shown = phase === "complete" ? preview.answer : preview?.previous ?? null;
  const facts = [];
  const notes = [];
  if (shown) {
    const said = answerFact(shown, source);
    facts.push(said.fact);
    notes.push(...said.notes);
  }
  if (phase === "looking") {
    notes.push(preview.stillLooking ? STILL_LOOKING : shown ? UPDATING : LOOKING);
  } else if (phase === "retrying") {
    const row = owned(preview.code);
    if (row === null || row.owner === LIBRARY) {
      // The library's transient answer (or no code): only then "can't reach your photo library".
      if (shown) notes.push(CANT_REACH);
      else facts.push(fact({ kind: "unknown", why: "Photo Wall can't reach your photo library right now; retrying" }));
    } else if (shown) {
      notes.push(`${row.state} · retrying.`);
    } else {
      facts.push(fact({ kind: "unknown", why: `${row.state} · retrying` }));
    }
  } else if (phase === "key") {
    notes.push(KEY_NOT_ALLOWED);
  } else if (phase === "failed") {
    const code = preview.code ?? "unknown_error";
    const row = owned(code);
    // A code with a row says that row's sentence (the Source card's); any other, the code.
    const why = row === null ? `the preview failed (${codeWords(code)})` : "the preview failed";
    if (shown) notes.push(row === null ? `The preview failed: ${codeWords(code)}.` : "The preview failed.");
    else facts.push(fact({ kind: "unknown", why }));
    if (row !== null) notes.push(refusalIssue(code));
  }
  return { facts, notes, answer: shown };
}

/** A served code's row in the one refusal table, or null. */
function owned(code) {
  return code && Object.hasOwn(SOURCE_REFUSALS, code) ? SOURCE_REFUSALS[code] : null;
}

/** "1,280 photos": digits grouped by three (not a locale formatter: timeWords.js owns those). */
const counted = (n, one, many) =>
  `${String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",")} ${n === 1 ? one : many}`;

/** One complete answer as a `reported` fact with a `first` receipt, and its statements. */
function answerFact(answer, source) {
  const { value, notes } = selects(answer);
  const shownCount = answer?.shown?.length ?? 0;
  if (shownCount > 0 && (answer.limited || answer.count > shownCount)) {
    notes.push(`Showing the newest ${shownCount}.`);
  }
  return {
    fact: fact({
      kind: "reported",
      source,
      receipt: "first",
      value,
      receivedAt: answer?.observed_at,
      readAt: answer?.read_at,
      field: "the time of your photo library's answer",
    }),
    notes,
  };
}

function selects(answer) {
  if (answer?.limited) {
    // Over the limit the counts are lower bounds; 1,000 is the media worker's current
    // ceiling, not a product rule (§44 open gaps), and a refresh over it refuses the Source.
    return { value: "more than 1,000 matches", notes: [OVER_LIMIT] };
  }
  const images = answer?.image_count ?? 0;
  const videos = answer?.video_count ?? 0;
  if (images + videos === 0) {
    return { value: "nothing matching yet", notes: ["New matches appear automatically once saved."] };
  }
  const parts = [
    images > 0 ? counted(images, "photo", "photos") : null,
    videos > 0 ? counted(videos, "video", "videos") : null,
  ].filter(Boolean);
  return { value: parts.join(" and "), notes: [] };
}

/** "0:32", "12:05", "1:02:03": a video's length. */
function lengthWords(seconds) {
  const whole = Math.round(seconds);
  const pad = (value) => String(value).padStart(2, "0");
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(whole % 60)}` : `${minutes}:${pad(whole % 60)}`;
}

/**
 * A tile's alt text (§39): "Photo dated 12 Dec 2024", "Video, 0:32, dated 12 Dec 2024", or
 * "Video dated 12 Dec 2024" when the library served no usable length.
 *
 * @param {{kind: string, captured_at: number, duration_seconds?: number|null}} member
 * @returns {string}
 */
export function tileWords(member) {
  const day = captureDay(member.captured_at);
  if (member.kind !== "video") return `Photo dated ${day}`;
  return Number.isFinite(member.duration_seconds) && member.duration_seconds > 0
    ? `Video, ${lengthWords(member.duration_seconds)}, dated ${day}`
    : `Video dated ${day}`;
}

/** A tile's image: Central's re-encoded thumbnail of a live preview's member (§40). */
export function thumbnailPath(assetId, attempt = 0) {
  const path = `/v1/operator/library/thumbnails/${encodeURIComponent(assetId)}`;
  return attempt === 0 ? path : `${path}?attempt=${attempt}`;
}

/** A tile retries once, this long after its first miss (§40). */
export const TILE_RETRY_MS = 5000;
export const TILE_NOT_READY = "Preview not ready yet";
