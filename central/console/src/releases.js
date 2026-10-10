import { useCallback, useMemo, useRef } from "react";

import { apiWrite } from "./apiWrite.js";
import { answerUnknown, UNKNOWN_MESSAGE } from "./sendOutcome.js";
import { fact, words } from "./facts.js";
import { frozen } from "./frozenRequest.js";
import { gigabytes } from "./health.js";
import { readError } from "./nodeRead.js";
import { usePolledRead } from "./polledRead.js";

/**
 * Fleet › Releases (console DDD Part E §25-§28, beads NR1, NR2, B7): the fleet-wide release
 * aggregates — the releases Central observed, the deployment each valid one became, whether
 * Central has downloaded it, and the one boot selection — as pure functions from Central's
 * release read to facts, offers and the requests the console sends.
 *
 * THE READ (`useReleaseRead`): `GET /v1/operator/node/releases`, one read-only snapshot on
 * Central (G1: selection, deployments, releases), through the shared polled read every 30 s
 * while visible, when the tab becomes visible, and after every write this page sends. A failed
 * read keeps the last answer and names its error beside it.
 *
 * CENTRAL INGESTS, THE CONSOLE NEVER PUBLISHES. The release sync turns every valid release into
 * its deployment by itself and downloads the newest stable ones in the background; a release
 * row carries its own `deployment_id` (null while the tag has no good manifest), its `problem`
 * and its readiness, derived by Central on read. The console derives no id and sends no publish.
 *
 * ONE SEND RULE PER VERB (R13). `sendSelection` judges its rule on `releases.latest()` — the
 * newest settled read at the moment of sending, never the read the dialog opened on — refuses
 * without a request when it fails, and is the ONLY console caller of its route (a source-scan
 * test holds it); `sendCatalogCheck` likewise for "Check GitHub releases now", judged on this
 * page's held check. Each verb has its own code table (§26); any refusal code not in it reads
 * "Central refused: <code>" and is refused: nothing is re-sent automatically.
 *
 * @typedef {{read: object|null, readAt: number|null,
 *            error: import("./nodeRead.js").NodeReadError|null, seq: number}} ReleaseRead
 *   `seq` numbers the read in the order reads STARTED on this page, so "the next read after
 *   an answer" is the first whose `seq` exceeds `startedReads()` taken at that answer.
 * @typedef {{deployment_id: string, published_at: number, base_tag: string,
 *            app_environment_sha256: string|null}} Deployment
 * @typedef {"ready"|"downloading"|"not_downloaded"|"failed"} Readiness
 * @typedef {{tag: string, stable: boolean, problem: string|null, manifest_sha256: string|null,
 *            deployment_id: string|null, revision: string|null, discovered_at: number|null,
 *            base_tag: string|null, app_environment_sha256: string|null, in_window: boolean,
 *            readiness: Readiness|null, readiness_reason: string|null,
 *            missing_bytes: number|null}} Release
 *   one observed tag. `deployment_id` null: Rejected (`problem` says why). With a deployment,
 *   `problem` is a newer upload of the tag that Central refused. `readiness` null: this
 *   Central did not serve it (no cache mounted), never guessed
 * @typedef {{outcome: "done"|"already"|"changed"|"refused"|"unknown", message: string, code?: string|null}} Outcome
 *   `code`: Central's served error code (null on success, a lost answer or a refusal before any
 *   request), so a caller that branches on a refusal keys on it, never on the words (§26)
 * @typedef {{get: () => boolean, set: (held: boolean) => void}} HeldCheck
 *   whether this page holds an unanswered "Check GitHub releases now"
 */

const PATH = "/v1/operator/node/releases";
const CADENCE_MS = 30000;
const NOT_READ = Object.freeze({ read: null, readAt: null, error: null, seq: 0 });

/**
 * The release read, while the Releases page is open.
 *
 * @param {{skip?: boolean}} [options] `skip` while node control is not known to be on
 * @returns {ReleaseRead & {busy: boolean, refresh: () => Promise<void>, latest: () => ReleaseRead,
 *           startedReads: () => number}} `busy`: a read is in flight; it clears only once that
 *   read is settled, so `latest()` already returns it
 */
export function useReleaseRead({ skip = false } = {}) {
  const started = useRef(0);
  const load = useCallback(async (current) => {
    const seq = ++started.current;
    const result = await apiWrite(PATH, { method: "GET" }).catch(() => null);
    if (result?.ok) return { read: result.data, readAt: result.data?.read_at ?? null, error: null, seq };
    return { ...current, error: readError(result), seq };
  }, []);
  const { value, busy, refresh, latest } = usePolledRead(load, { cadenceMs: CADENCE_MS, skip, initial: NOT_READ });
  const startedReads = useCallback(() => started.current, []);
  return useMemo(() => ({ ...value, busy, refresh, latest, startedReads }), [value, busy, refresh, latest, startedReads]);
}

// --- Identity and contents.

/** "7c41…": a deployment id's handle. */
export const deploymentHandle = (id) => `${String(id).slice(0, 4)}…`;
/** "9f8e7d…": an app environment digest's handle. */
export const appHandle = (sha) => `${String(sha).slice(0, 6)}…`;

/** "Base v0.15.0 · app 9f8e7d…" / "Base v0.15.0 · no app". */
function contents(record) {
  const app = record.app_environment_sha256 == null ? "no app" : `app ${appHandle(record.app_environment_sha256)}`;
  return `Base ${record.base_tag} · ${app}`;
}

/**
 * What the read says a deployment id carries: its Deployments row, else the release row that
 * became it. A release row names its own deployment, so a release beyond the capped
 * Deployments list can still be put on the wall.
 *
 * @param {object|null} read
 * @param {string|null} deploymentId
 * @returns {{deployment_id: string, base_tag: string, app_environment_sha256: string|null}|null}
 */
export function deploymentRecord(read, deploymentId) {
  if (deploymentId == null) return null;
  return (read?.deployments ?? []).find((row) => row.deployment_id === deploymentId)
    ?? (read?.releases ?? []).find((row) => row.deployment_id === deploymentId) ?? null;
}

/** The release row that became `deploymentId`, or null (a deployment published by hand). */
export function releaseOf(read, deploymentId) {
  if (deploymentId == null) return null;
  return (read?.releases ?? []).find((row) => row.deployment_id === deploymentId) ?? null;
}

/**
 * The release "Update the wall…" opens by default: the newest stable release that became a
 * deployment (the read lists releases newest first), or null.
 *
 * @param {object|null} read
 * @returns {Release|null}
 */
export function newestStable(read) {
  return (read?.releases ?? []).find((row) => row.stable && row.deployment_id != null) ?? null;
}

/** "1.2 GB", or "less than 0.1 GB". */
export function downloadSize(bytes) {
  return Number(bytes) < 1e8 ? "less than 0.1 GB" : `${gigabytes(bytes)} GB`;
}

// --- Readiness (derived by Central on read; a selection is not "on the wall").

/**
 * One release row's state in one phrase: Rejected (no deployment), else its readiness; null
 * when Central did not serve the readiness.
 *
 * @param {Release} row
 * @returns {string|null}
 */
export function readinessWords(row) {
  if (row.deployment_id == null) return `Rejected: ${words(row.problem)}`;
  if (row.readiness === "ready") return "Ready";
  if (row.readiness === "downloading") return `Downloading · ${downloadSize(row.missing_bytes)} left`;
  if (row.readiness === "not_downloaded") return "Not downloaded";
  if (row.readiness === "failed") return `Failed: ${words(row.readiness_reason)}`;
  return null;
}

/** A release row's readiness as a fact: Central's own record of its cache, or unknown. */
function readinessFact(row) {
  const value = readinessWords(row);
  return value === null ? fact({ kind: "unknown", why: "Central did not serve whether it is downloaded" })
    : fact({ kind: "set", value });
}

/**
 * The readiness line every confirmation that selects a release carries: what a Player that
 * boots it meets now.
 *
 * @param {Release} row
 * @returns {string}
 */
export function readinessLine(row) {
  const tag = row.tag;
  if (row.readiness === "ready") return `Ready: Central has every file of ${tag}.`;
  if (row.readiness === "downloading") {
    return `Downloading: Central still has ${downloadSize(row.missing_bytes)} of ${tag} to download; a Player that `
      + "boots it before then waits for the download.";
  }
  if (row.readiness === "not_downloaded") {
    return `Not downloaded: a Player that boots ${tag} waits while Central downloads it.`;
  }
  if (row.readiness === "failed") {
    return `Failed: Central could not download ${tag} (${words(row.readiness_reason)}); a Player that boots it waits `
      + "while Central tries again.";
  }
  return `Central did not serve whether ${tag} is downloaded.`;
}

// --- The read model.

/**
 * @typedef {{deploymentId: string, deployment: import("./facts.js").Fact, contents: string,
 *            from: import("./facts.js").Fact|null, selected: boolean, previous: boolean}} DeploymentRow
 * @typedef {{release: Release, catalog: import("./facts.js").Fact,
 *            contents: import("./facts.js").Fact|null, readiness: import("./facts.js").Fact,
 *            newerRejected: string|null, prerelease: boolean, label: string|null,
 *            deploymentId: string|null}} ReleaseRow
 *   `label`: "Selected for every boot" or "Previous selection" (a commitment, never "on the wall")
 */

/**
 * The boot selection in one fact. With no selection ever made, what Central will do by itself
 * (first run, §6.4): select the newest stable release once it is downloaded, or why it cannot.
 */
function selectionFact(read) {
  const selection = read.selection;
  if (selection?.deployment_id != null) {
    return fact({ kind: "set", receivedAt: selection.changed_at, readAt: read.read_at,
      value: `Selected for every boot from now on: deployment ${deploymentHandle(selection.deployment_id)} (revision ${selection.revision})` });
  }
  const auto = selection?.auto ?? null;
  if (auto == null) return fact({ kind: "set", value: "No boot selection · Central refuses every boot" });
  if (auto.readiness === "failed") {
    return fact({ kind: "set", value: "No boot selection · Central refuses every boot. Central cannot select "
      + `${auto.tag} by itself: Failed: ${words(auto.readiness_reason)} · choose a release` });
  }
  if (auto.readiness === "ready") {
    return fact({ kind: "set", value: `No selection yet · Central selects ${auto.tag} at its next release check` });
  }
  return fact({ kind: "set", value: `No selection yet · Central selects ${auto.tag} when its download finishes `
    + `(${downloadSize(auto.missing_bytes)} left)` });
}

/**
 * Every line of Releases as a fact (§26), from one read.
 *
 * @param {object} read the release read
 * @returns {{selection: import("./facts.js").Fact, previous: import("./facts.js").Fact|null,
 *            deployments: DeploymentRow[], releases: ReleaseRow[]}}
 */
export function releaseHome(read) {
  const readAt = read.read_at;
  const selection = read.selection;
  const selected = selection?.deployment_id ?? null;
  const previous = selection?.previous_deployment_id ?? null;
  const releases = read.releases ?? [];
  const deployments = read.deployments ?? [];
  const previousRelease = releaseOf(read, previous);
  return {
    selection: selectionFact(read),
    previous: previous === null ? null : fact({ kind: "set", value: `Previous selection: deployment `
      + `${deploymentHandle(previous)}${previousRelease === null ? "" : ` (release ${previousRelease.tag})`}` }),
    deployments: deployments.map((row) => {
      const source = releaseOf(read, row.deployment_id);
      return {
        deploymentId: row.deployment_id,
        deployment: fact({ kind: "set", receivedAt: row.published_at, readAt,
          value: `Deployment ${deploymentHandle(row.deployment_id)} recorded` }),
        contents: contents(row),
        from: source === null ? null : fact({ kind: "set", value: `From release ${source.tag}` }),
        selected: selected === row.deployment_id,
        previous: previous === row.deployment_id,
      };
    }),
    releases: releases.map((release) => ({
      release,
      catalog: release.deployment_id == null ? fact({ kind: "set", value: `release ${release.tag}` })
        : fact({ kind: "reported", receipt: "first", source: "GitHub releases", receivedAt: release.discovered_at,
          readAt, field: "the catalog's discovery time",
          value: `release ${release.tag} (rev ${String(release.revision).slice(0, 7)})` }),
      contents: release.deployment_id == null ? null : fact({ kind: "set", value: contents(release) }),
      readiness: readinessFact(release),
      newerRejected: release.deployment_id != null && release.problem != null
        ? `Newer upload rejected: ${words(release.problem)}` : null,
      prerelease: release.stable === false,
      label: release.deployment_id == null ? null
        : release.deployment_id === selected ? "Selected for every boot"
        : release.deployment_id === previous ? "Previous selection" : null,
      deploymentId: release.deployment_id,
    })),
  };
}

// --- Outcomes.

/** Select's own refusal and Central's 409 for it are the same outcome (§27). */
export const SELECTION_CHANGED = "The boot selection changed meanwhile; review it";

const UNREADABLE_REQUEST = "Central could not read this request";
const refused = (message) => ({ outcome: "refused", message });

// Central's codes each verb words (§26); every other code takes the fail-closed default.
const SELECT_CODES = Object.freeze({
  node_boot_policy_conflict: { outcome: "changed", message: SELECTION_CHANGED },
  node_deployment_unknown: refused("Central has no such deployment"),
  invalid_node_boot_selection: refused(UNREADABLE_REQUEST),
});

// "Check GitHub releases now" words no code: any refusal takes the default.
const CHECK_CODES = Object.freeze({});

/**
 * Central's answer to a release write as an outcome. The console keys on the code, never the
 * status (Central re-maps codes onto statuses, §26). An unanswered request, or a 5xx with no
 * code (a gateway, a lost answer), is unknown; any code not listed is refused. The served code
 * rides on the outcome (`code`), so a caller branches on it, never on the words.
 *
 * @param {{ok: boolean, status: number, error: string|null, data: any}|null} result
 * @param {(data: any) => string} done the verb's words for a recorded write
 * @param {Readonly<Record<string, {outcome: Outcome["outcome"], message: string}>>} codes the
 *   verb's own code table
 * @returns {Outcome}
 */
export function releaseResult(result, done, codes) {
  // Any served code at 5xx is Central's own refusal (§26 re-maps codes onto statuses).
  if (answerUnknown(result, (code) => typeof code === "string" && code !== "")) {
    return { outcome: "unknown", message: UNKNOWN_MESSAGE, code: null };
  }
  if (result.ok) {
    return result.data?.duplicate === true
      ? { outcome: "already", message: `Already recorded. ${done(result.data)}`, code: null }
      : { outcome: "done", message: done(result.data), code: null };
  }
  const code = result.error;
  if (typeof code !== "string" || code === "") {
    return { outcome: "refused", message: `Central refused: ${result.status}.`, code: null };
  }
  const known = Object.hasOwn(codes, code) ? codes[code] : undefined;
  return known === undefined ? { outcome: "refused", message: `Central refused: ${code}.`, code }
    : { outcome: known.outcome, message: `${known.message}.`, code };
}

// --- Select (R13, R17).

/**
 * What a deployment offers now: Select, "Selected", or nothing (with why). The deployment is
 * listed when the read's Deployments or a release row names it (`deploymentRecord`).
 *
 * @param {object|null} read
 * @param {string} deploymentId
 * @returns {{offer: "select"} | {offer: "selected"} | {offer: "blocked", reason: string}}
 */
export function selectionOffer(read, deploymentId) {
  if (read == null) return { offer: "blocked", reason: "the release read has not answered" };
  if (!Number.isInteger(read.selection?.revision)) {
    return { offer: "blocked", reason: "Central did not serve the boot selection's revision" };
  }
  if (deploymentRecord(read, deploymentId) === null) {
    return { offer: "blocked", reason: "Central no longer lists this deployment" };
  }
  return read.selection.deployment_id === deploymentId ? { offer: "selected" } : { offer: "select" };
}

/**
 * Freeze a selection when its dialog opens: the body sent (`expected_revision` is the read's
 * revision, 0 with no selection), the deployment's contents, and whether it has no app.
 *
 * @param {object|null} read
 * @param {string} deploymentId
 * @returns {{body: {deployment_id: string, expected_revision: number}, contents: string,
 *            noApp: boolean} | {refused: string}}
 */
export function selectionRequest(read, deploymentId) {
  const offer = selectionOffer(read, deploymentId);
  if (offer.offer !== "select") return { refused: offer.offer === "selected" ? "already selected" : offer.reason };
  const row = deploymentRecord(read, deploymentId);
  return frozen({
    body: { deployment_id: deploymentId, expected_revision: read.selection.revision },
    contents: contents(row),
    noApp: row.app_environment_sha256 == null,
  });
}

/** Select's scope (§26, R17): fleet-wide, and wider than any list the console could show. */
export const SELECT_SCOPE = "Every Player that boots by node path from now on is offered this deployment, including "
  + "Players Central has not seen. Central cannot list which Players will boot.";
/** Central's offer when the selected deployment has no app (R17). */
export const SELECT_NO_APP = "This deployment has no app: every boot from now on is offered no app.";

/**
 * Select's confirmation words (R17), shared by every page that sends a selection, so no page can
 * drop the scope or the no-app consequence: the deployment's contents, the fleet-wide scope and,
 * when it has no app, what every boot is then offered. A page may add its own lines below them.
 *
 * @param {{contents: string, noApp: boolean}} request `selectionRequest`'s frozen request
 * @returns {string[]}
 */
export function selectionConfirmation(request) {
  return [request.contents, SELECT_SCOPE, ...(request.noApp ? [SELECT_NO_APP] : [])];
}

/**
 * Why a frozen selection may not be sent on `read`, or null: the revision moved, the
 * deployment is no longer listed, or it is already selected.
 */
export function selectionRefusal(request, read) {
  const offer = selectionOffer(read, request.body.deployment_id);
  return offer.offer === "select" && read.selection.revision === request.body.expected_revision
    ? null : SELECTION_CHANGED;
}

/**
 * THE one send path for a boot selection: judge the frozen request on `releases.latest()` at
 * the moment of sending; PUT only when `selectionRefusal` allows it.
 *
 * @param {object} request `selectionRequest`'s frozen request
 * @param {{latest: () => ReleaseRead}} releases the `useReleaseRead` hook
 * @returns {Promise<Outcome>}
 */
export async function sendSelection(request, releases) {
  const refusal = selectionRefusal(request, releases.latest().read);
  if (refusal !== null) return { outcome: "changed", message: `${refusal}.` };
  let result;
  try {
    result = await apiWrite("/v1/operator/node/boot-policy", { method: "PUT", body: request.body });
  } catch {
    result = null;
  }
  return releaseResult(result, (data) => `Selected for every boot from now on at revision ${data?.revision ?? "not served"}.`,
    SELECT_CODES);
}

/**
 * How a selection whose answer was lost settled, on the first read started after the answer
 * (§27): selected at the next revision is done; anything else is changed.
 *
 * @param {object} request the frozen selection
 * @param {object|null} read
 * @returns {Outcome}
 */
export function selectionSettled(request, read) {
  const selection = read?.selection;
  return selection?.revision === request.body.expected_revision + 1
      && selection?.deployment_id === request.body.deployment_id
    ? { outcome: "done", message: `Selected for every boot from now on at revision ${selection.revision}.` }
    : { outcome: "changed", message: `${SELECTION_CHANGED}.` };
}

// --- Check GitHub releases now (§25, §28).

/** What a queued check means, and never a claim that a release arrived (§26). */
export const CHECK_QUEUED = "Central queued a check of GitHub releases; new releases appear here when the "
  + "media worker records them.";

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
  return releaseResult(result, () => CHECK_QUEUED, CHECK_CODES);
}
