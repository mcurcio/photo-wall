import { useCallback, useMemo, useRef } from "react";

import { apiWrite } from "./apiWrite.js";
import { readReleases, setWallVersion } from "./domain/releases/api";
import { deployable, refusalWords, wallOn } from "./domain/releases/model";
import { usePolledRead } from "./polledRead.js";
import { answerOutcome, answerUnknown, UNKNOWN_MESSAGE } from "./sendOutcome.js";

/**
 * Fleet › Releases: the releases Central observed and a version per Player (Q2 A, 2026-10-16),
 * with the requests the Releases page and Update the wall send. Central keeps no wall-wide
 * selection: each Player boots its own version, and one with none boots the newest ready stable
 * release. The pure words are domain/releases/model.ts; the routes are domain/releases/api.ts.
 *
 * THE READ (`useReleaseRead`): GET /v1/operator/releases (ReleasesView), through the shared
 * polled read every 30 s while visible, when the tab becomes visible, and after every write this
 * page sends. A failed read keeps the last answer and names its error beside it.
 *
 * ONE SEND RULE PER VERB (R13). `sendWallVersion` judges its rule on `releases.latest()` — the
 * newest settled read at the moment of sending — refuses without a request when it fails, and is
 * the ONLY console caller of Update the wall's PUT; `sendCatalogCheck` likewise for "Check GitHub
 * releases now". A refusal is said in its own words; nothing is re-sent automatically.
 *
 * @typedef {{read: import("./generated/contract").ReleasesView|null, readAt: number|null,
 *            error: {code: string}|null, seq: number}} ReleaseRead
 *   `seq` numbers the read in the order reads STARTED on this page, so "the next read after
 *   an answer" is the first whose `seq` exceeds `startedReads()` taken at that answer.
 * @typedef {import("./sendOutcome.js").Outcome} Outcome
 * @typedef {{get: () => boolean, set: (held: boolean) => void}} HeldCheck
 *   whether this page holds an unanswered "Check GitHub releases now"
 */

const CADENCE_MS = 30000;
const NOT_READ = Object.freeze({ read: null, readAt: null, error: null, seq: 0 });

/**
 * The release read, while a Releases page is open.
 *
 * @returns {ReleaseRead & {busy: boolean, refresh: () => Promise<void>, latest: () => ReleaseRead,
 *           startedReads: () => number}} `busy`: a read is in flight; it clears only once that
 *   read is settled, so `latest()` already returns it
 */
export function useReleaseRead() {
  const started = useRef(0);
  const load = useCallback(async (current) => {
    const seq = ++started.current;
    const answer = await readReleases();
    if (answer.ok) return { read: answer.view, readAt: answer.view.read_at, error: null, seq };
    return { ...current, error: { code: answer.error }, seq };
  }, []);
  const { value, busy, refresh, latest } = usePolledRead(load, { cadenceMs: CADENCE_MS, initial: NOT_READ });
  const startedReads = useCallback(() => started.current, []);
  return useMemo(() => ({ ...value, busy, refresh, latest, startedReads }), [value, busy, refresh, latest, startedReads]);
}

/**
 * Whether a release write's answer was lost (Update the wall, Install): no answer, or a 5xx that
 * carries no code of Central's (a gateway). The rule is sendOutcome.js `answerUnknown`'s, read on the
 * domains' Answer (`unreachable`, or `http_<status>` for a body outside its shape).
 *
 * @param {string} error
 * @returns {boolean}
 */
export function answerLost(error) {
  if (error === "unreachable") return true;
  const status = /^http_(\d{3})$/.exec(error);
  return status !== null && answerUnknown({ ok: false, status: Number(status[1]), error: null });
}

// --- Update the wall's one write.

/** What Update the wall did, in its one wording: every Player's version is `version`. */
export const wallVersionWords = (version) => `Every Player boots ${version} from its next boot on.`;

/** Update the wall's scope: every Player's own version, no reboot by itself. */
export const WALL_SCOPE = "Every Player Central knows boots this release from its next boot on. A Player that "
  + "enrols later boots the newest ready release until it is given one.";

/**
 * Why `version` may not be put on the wall on `read`, or null: not read, not listed, or rejected.
 *
 * @param {import("./generated/contract").ReleasesView|null} read
 * @param {string} version
 * @returns {string|null}
 */
export function wallRefusal(read, version) {
  if (read == null) return "the release read has not answered";
  const release = read.releases.find((row) => row.version === version);
  if (release === undefined) return "Central no longer lists this release";
  return deployable(release) ? null : "Central refused this release's upload";
}

/**
 * THE one send path for Update the wall: judge the version on `releases.latest()` at the
 * moment of sending; PUT only when `wallRefusal` allows it. Every Player's version becomes
 * `version`; nothing is rebooted by this request.
 *
 * @param {string} version
 * @param {{latest: () => ReleaseRead}} releases the `useReleaseRead` hook
 * @returns {Promise<Outcome>}
 */
export async function sendWallVersion(version, releases) {
  const refusal = wallRefusal(releases.latest().read, version);
  if (refusal !== null) return { outcome: "changed", message: `${refusal}.`, code: null };
  const answer = await setWallVersion({ version });
  if (answer.ok) return { outcome: "done", message: wallVersionWords(version), code: null };
  if (answerLost(answer.error)) return { outcome: "unknown", message: UNKNOWN_MESSAGE, code: null };
  return { outcome: "refused", message: refusalWords(answer.error), code: answer.error };
}

/**
 * How an Update the wall whose answer was lost settled, on the first read started after the
 * answer: every Player on `version` is done; anything else is changed.
 *
 * @param {string} version
 * @param {import("./generated/contract").ReleasesView|null} read
 * @returns {Outcome}
 */
export function wallSettled(version, read) {
  return read != null && wallOn(read, version)
    ? { outcome: "done", message: wallVersionWords(version), code: null }
    : { outcome: "changed", message: "The Players' versions changed meanwhile; review them.", code: null };
}

// --- Check GitHub releases now.

/** What a queued check means, and never a claim that a release arrived. */
export const CHECK_QUEUED = "Central queued a check of GitHub releases; new releases appear here when the "
  + "media worker records them.";

// "Check GitHub releases now" words no code: any refusal takes the default.
const CHECK_CODES = Object.freeze({});

/**
 * THE one send path for "Check GitHub releases now": the media worker's release sync, queued
 * now (Central merges it with a pending tick, so it is safe to repeat). Sends nothing while
 * this page holds an unanswered check; the hold ends with any answer.
 *
 * @param {HeldCheck} held
 * @returns {Promise<Outcome>}
 */
export async function sendCatalogCheck(held) {
  if (held.get()) return { outcome: "changed", message: "This page's check is still unanswered." };
  held.set(true);
  let result;
  try {
    result = await apiWrite("/v1/operator/app/releases/refresh", { method: "POST" });
  } catch {
    result = null;
  } finally {
    held.set(false);
  }
  return answerOutcome(result, () => CHECK_QUEUED, CHECK_CODES);
}
