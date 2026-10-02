import { useCallback, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { UNKNOWN_MESSAGE } from "./equipmentApi.js";
import { fact, words } from "./facts.js";
import { auditRef, frozen } from "./frozenRequest.js";
import { gigabytes } from "./health.js";
import { readError } from "./nodeRead.js";
import { usePolledRead } from "./polledRead.js";

/**
 * Fleet › Releases (console DDD Part E §25-§28, beads NR1 and NR2): the fleet-wide release
 * aggregates — the release catalog, the deployments published from it and the one boot
 * selection — as pure functions from Central's release read to facts, offers and the requests
 * the console sends.
 *
 * THE READ (`useReleaseRead`): `GET /v1/operator/node/releases`, one read-only snapshot on
 * Central (G1: selection, deployments, releases), through the shared polled read every 30 s
 * while visible, when the tab becomes visible, and after every write this page sends. A failed
 * read keeps the last answer and names its error beside it.
 *
 * ONE SEND RULE PER VERB (R13). `sendSelection` and `sendPublish` judge their rule on
 * `releases.latest()` — the newest settled read at the moment of sending, never the read the
 * dialog opened on — refuse without a request when it fails, and are the ONLY console callers
 * of their routes (a source-scan test holds it); `sendCatalogCheck` likewise for "Check GitHub
 * releases now", judged on this page's held check. Each verb has its own code table (§26); any
 * refusal code not in it reads "Central refused: <code>" and is refused: nothing is re-sent
 * automatically.
 *
 * DEPLOYMENT IDENTITY (§28). A console publish's deployment id is DERIVED from the release's
 * content (`deploymentIdFor`: its manifest digest laid out as a UUIDv8, one bit for "with
 * app"), so a retry or a second page cannot make twin deployments, and "Published from" is
 * shown only when a listed deployment has that id AND the release's base and app.
 *
 * @typedef {{read: object|null, readAt: number|null,
 *            error: import("./nodeRead.js").NodeReadError|null, seq: number}} ReleaseRead
 *   `seq` numbers the read in the order reads STARTED on this page, so "the next read after
 *   an answer" is the first whose `seq` exceeds `startedReads()` taken at that answer.
 * @typedef {{deployment_id: string, published_at: number, base_tag: string,
 *            app_environment_sha256: string|null}} Deployment
 * @typedef {{manifest_sha256: string, tag: string, revision: string, discovered_at: number,
 *            verified_at: number|null, base_tag: string, app_environment_sha256: string|null,
 *            download_bytes: number}} Release
 * @typedef {{outcome: "done"|"already"|"changed"|"refused"|"unknown", message: string, code?: string|null}} Outcome
 *   `code`: Central's served error code (null on success, a lost answer or a refusal before any
 *   request), so a caller that branches on a refusal keys on it, never on the words (§26)
 * @typedef {"in_flight"|"recorded"|"unknown"} HeldState
 *   a publish this page holds: sent and unanswered; answered published, not listed yet; or
 *   its answer lost (Central may still be verifying)
 * @typedef {{get: (deploymentId: string) => HeldState|null,
 *            frozen: (deploymentId: string) => object|null,
 *            set: (deploymentId: string, state: HeldState|null, request?: object) => void}} HeldPublishes
 *   `frozen` is the request this page sent for the id, so "Send again" re-sends that body
 * @typedef {{get: () => boolean, set: (held: boolean) => void}} HeldCheck
 *   whether this page holds an unanswered "Check GitHub releases now"
 */

const PATH = "/v1/operator/node/releases";
const CADENCE_MS = 30000;
const NOT_READ = Object.freeze({ read: null, readAt: null, error: null, seq: 0 });

/** A publish downloads and hash-checks every release asset inside its request (§28). */
export const PUBLISH_TIMEOUT_MS = 30 * 60 * 1000;

/**
 * The release read, while the Releases page is open.
 *
 * @param {{skip?: boolean}} [options] `skip` while node control is not known to be on
 * @returns {ReleaseRead & {refresh: () => Promise<void>, latest: () => ReleaseRead,
 *           startedReads: () => number}}
 */
export function useReleaseRead({ skip = false } = {}) {
  const started = useRef(0);
  const load = useCallback(async (current) => {
    const seq = ++started.current;
    const result = await apiWrite(PATH, { method: "GET" }).catch(() => null);
    if (result?.ok) return { read: result.data, readAt: result.data?.read_at ?? null, error: null, seq };
    return { ...current, error: readError(result), seq };
  }, []);
  const { value, refresh, latest } = usePolledRead(load, { cadenceMs: CADENCE_MS, skip, initial: NOT_READ });
  const startedReads = useCallback(() => started.current, []);
  return useMemo(() => ({ ...value, refresh, latest, startedReads }), [value, refresh, latest, startedReads]);
}

/**
 * The publishes one page holds (`HeldPublishes`): a ref, so a send marks its request held in
 * the same step as its check, and a render after each change. Releases and Update the wall
 * each hold their own.
 *
 * @returns {HeldPublishes}
 */
export function useHeldPublishes() {
  const heldRef = useRef(/** @type {Map<string, {state: HeldState, request: object|null}>} */ (new Map()));
  const [, setVersion] = useState(0);
  return useMemo(() => ({
    get: (id) => heldRef.current.get(id)?.state ?? null,
    frozen: (id) => heldRef.current.get(id)?.request ?? null,
    set: (id, state, request = null) => {
      if (state === null) heldRef.current.delete(id);
      else heldRef.current.set(id, { state, request });
      setVersion((version) => version + 1);
    },
  }), []);
}

// --- Identity and contents.

/**
 * The deployment id this console publishes a release under: the first 128 bits of its
 * manifest digest with the UUID version set to 8 and the RFC 4122 variant, and the variant
 * nibble's next bit set for "with app". Pure, and no hashing in the browser, so it works on a
 * plain-http console.
 *
 * @param {string} manifestSha256 64 lowercase hex digits
 * @param {boolean} withApp
 * @returns {string}
 */
export function deploymentIdFor(manifestSha256, withApp) {
  if (typeof manifestSha256 !== "string" || !/^[0-9a-f]{64}$/.test(manifestSha256)) {
    throw new Error(`not a manifest digest: ${String(manifestSha256)}`);
  }
  const hex = manifestSha256.slice(0, 32).split("");
  hex[12] = "8";
  hex[16] = (0x8 | (withApp ? 0x2 : 0) | (parseInt(hex[16], 16) & 0x1)).toString(16);
  const h = hex.join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/** "7c41…": a deployment id's handle. */
export const deploymentHandle = (id) => `${String(id).slice(0, 4)}…`;
/** "9f8e7d…": an app environment digest's handle. */
export const appHandle = (sha) => `${String(sha).slice(0, 6)}…`;

/** "Base v0.15.0 · app 9f8e7d…" / "Base v0.15.0 · no app". */
function contents(record) {
  const app = record.app_environment_sha256 == null ? "no app" : `app ${appHandle(record.app_environment_sha256)}`;
  return `Base ${record.base_tag} · ${app}`;
}

/** Whether a listed deployment carries exactly what publishing `release` (with or without its app) makes. */
function sameContents(deployment, release, withApp) {
  return deployment.base_tag === release.base_tag
    && deployment.app_environment_sha256 === (withApp ? release.app_environment_sha256 : null);
}

/** The deployment this console's publish of `release` made, when it is listed with matching contents. */
function publishedAs(read, release) {
  for (const withApp of [true, false]) {
    if (withApp && release.app_environment_sha256 == null) continue;
    const id = deploymentIdFor(release.manifest_sha256, withApp);
    const listed = (read?.deployments ?? []).find((row) => row.deployment_id === id);
    if (listed !== undefined && sameContents(listed, release, withApp)) return listed;
  }
  return null;
}

/** "1.2 GB", or "less than 0.1 GB". */
export function downloadSize(bytes) {
  return Number(bytes) < 1e8 ? "less than 0.1 GB" : `${gigabytes(bytes)} GB`;
}

// --- The read model.

/**
 * @typedef {{deploymentId: string, deployment: import("./facts.js").Fact, contents: string,
 *            from: import("./facts.js").Fact|null, selected: boolean}} DeploymentRow
 * @typedef {{release: Release, catalog: import("./facts.js").Fact,
 *            verified: import("./facts.js").Fact|null, contents: import("./facts.js").Fact,
 *            deploymentId: string|null}} ReleaseRow
 */

/**
 * Every line of Releases as a fact (§26), from one read.
 *
 * @param {object} read the release read
 * @returns {{selection: import("./facts.js").Fact, deployments: DeploymentRow[], releases: ReleaseRow[]}}
 */
export function releaseHome(read) {
  const readAt = read.read_at;
  const selection = read.selection;
  const releases = read.releases ?? [];
  const deployments = read.deployments ?? [];
  return {
    selection: selection?.deployment_id == null
      ? fact({ kind: "set", value: "No boot selection · Central refuses every boot" })
      : fact({ kind: "set", receivedAt: selection.changed_at, readAt,
        value: `Selected for every boot from now on: deployment ${deploymentHandle(selection.deployment_id)} (revision ${selection.revision})` }),
    deployments: deployments.map((row) => {
      const source = releases.find((release) => publishedAs(read, release)?.deployment_id === row.deployment_id);
      return {
        deploymentId: row.deployment_id,
        deployment: fact({ kind: "set", receivedAt: row.published_at, readAt,
          value: `Deployment ${deploymentHandle(row.deployment_id)} published` }),
        contents: contents(row),
        from: source === undefined ? null : fact({ kind: "derived",
          value: `Published from release ${source.tag}`,
          basis: "the deployment id is the one this console derives from that release, and its contents match" }),
        selected: selection?.deployment_id === row.deployment_id,
      };
    }),
    releases: releases.map((release) => ({
      release,
      catalog: fact({ kind: "reported", receipt: "first", source: "GitHub releases", receivedAt: release.discovered_at,
        readAt, field: "the catalog's discovery time",
        value: `release ${release.tag} (rev ${String(release.revision).slice(0, 7)})` }),
      verified: release.verified_at == null ? null
        : fact({ kind: "set", value: "Bytes verified by Central at publish", receivedAt: release.verified_at, readAt }),
      contents: fact({ kind: "set", value: contents(release) }),
      deploymentId: publishedAs(read, release)?.deployment_id ?? null,
    })),
  };
}

// --- Outcomes.

/** Select's own refusal and Central's 409 for it are the same outcome (§27). */
export const SELECTION_CHANGED = "The boot selection changed meanwhile; review it";
const IDENTITY_CONFLICT = "A different deployment already uses this release's id (published by hand)";

const UNREADABLE_REQUEST = "Central could not read this request";
const refused = (message) => ({ outcome: "refused", message });

// Central's codes each verb words (§26); every other code takes the fail-closed default.
const SELECT_CODES = Object.freeze({
  node_boot_policy_conflict: { outcome: "changed", message: SELECTION_CHANGED },
  node_deployment_unknown: refused("Central has no such deployment"),
  invalid_node_boot_selection: refused(UNREADABLE_REQUEST),
});

// The GitHub origin's failure reasons, which Central's publish serves as codes
// (central/origins/github.py; node_routes.py `publish_release`): unavailable is transient (the
// download may succeed if sent again), rejected is terminal.
const ORIGIN_UNAVAILABLE = ["origin_unreachable", "origin_error", "rate_limited", "manifest_unavailable",
  "download_truncated", "download_corrupt"];
const ORIGIN_REJECTED = ["download_not_found", "download_encoding", "download_too_large", "download_rejected",
  "list_invalid", "node_release_invalid"];
const PROVENANCE = refused("Central cannot match this release's bytes to its catalog");

const PUBLISH_CODES = Object.freeze({
  node_release_unknown: refused("Central no longer lists this release"),
  node_release_app_unconfigured: refused("This release has no app"),
  node_release_app_selection_invalid: refused(UNREADABLE_REQUEST),
  node_release_publication_invalid: refused(UNREADABLE_REQUEST),
  node_release_locator_mismatch: refused("This release's asset locations disagree with its manifest"),
  node_release_verification_storage_unavailable: refused(
    "Central lacks scratch space to verify this release (another publish may be running)"),
  node_base_release_provenance_unavailable: PROVENANCE,
  node_environment_release_provenance_unavailable: PROVENANCE,
  node_base_manager_pins_immutable: refused(
    "This base already has different App Manager pins; this release can never be published"),
  node_deployment_identity_conflict: refused(IDENTITY_CONFLICT),
  node_environment_identity_conflict: refused("Central already describes this app environment differently"),
  ...Object.fromEntries(ORIGIN_UNAVAILABLE.map((code) => [code,
    { outcome: "unknown", message: "GitHub releases did not answer; send again" }])),
  ...Object.fromEntries(ORIGIN_REJECTED.map((code) => [code,
    refused(`GitHub releases refused the download: ${words(code)}`)])),
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
  if (result == null) return { outcome: "unknown", message: UNKNOWN_MESSAGE, code: null };
  if (result.ok) {
    return result.data?.duplicate === true
      ? { outcome: "already", message: `Already recorded. ${done(result.data)}`, code: null }
      : { outcome: "done", message: done(result.data), code: null };
  }
  const code = result.error;
  if (typeof code !== "string" || code === "") {
    return result.status >= 500 ? { outcome: "unknown", message: UNKNOWN_MESSAGE, code: null }
      : { outcome: "refused", message: `Central refused: ${result.status}.`, code: null };
  }
  const known = Object.hasOwn(codes, code) ? codes[code] : undefined;
  return known === undefined ? { outcome: "refused", message: `Central refused: ${code}.`, code }
    : { outcome: known.outcome, message: `${known.message}.`, code };
}

// --- Select (R13, R17).

/**
 * What a deployment row offers now: Select, "Selected", or nothing (with why).
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
  if (!(read.deployments ?? []).some((row) => row.deployment_id === deploymentId)) {
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
  const row = read.deployments.find((entry) => entry.deployment_id === deploymentId);
  return frozen({
    body: { deployment_id: deploymentId, expected_revision: read.selection.revision },
    contents: contents(row),
    noApp: row.app_environment_sha256 == null,
  });
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

// --- Publish (§27, §28).

/**
 * What a release offers now for one app choice: Publish; "Published" (its derived deployment
 * is listed with matching contents); this page's own request held; or nothing (with why).
 *
 * @param {object|null} read
 * @param {Release} release
 * @param {boolean} withApp
 * @param {HeldPublishes} held
 * @returns {{offer: "publish", deploymentId: string} | {offer: "published", deploymentId: string}
 *         | {offer: HeldState, deploymentId: string} | {offer: "blocked", reason: string}}
 */
export function publishOffer(read, release, withApp, held) {
  if (withApp && release.app_environment_sha256 == null) return { offer: "blocked", reason: "This release has no app" };
  const deploymentId = deploymentIdFor(release.manifest_sha256, withApp);
  const listed = (read?.deployments ?? []).find((row) => row.deployment_id === deploymentId);
  if (listed !== undefined) {
    return sameContents(listed, release, withApp) ? { offer: "published", deploymentId }
      : { offer: "blocked", reason: IDENTITY_CONFLICT };
  }
  const mine = held.get(deploymentId);
  if (mine !== null) return { offer: mine, deploymentId };
  if (read == null) return { offer: "blocked", reason: "the release read has not answered" };
  if (!(read.releases ?? []).some((row) => row.manifest_sha256 === release.manifest_sha256)) {
    return { offer: "blocked", reason: "Central no longer lists this release" };
  }
  return { offer: "publish", deploymentId };
}

/**
 * Freeze a publish when its dialog opens: the body sent, the download it starts and the
 * permanence it fixes.
 *
 * @param {object|null} read
 * @param {Release} release
 * @param {boolean} withApp
 * @param {HeldPublishes} held
 * @returns {{release: Release, withApp: boolean,
 *            body: {deployment_id: string, select_app: boolean, operator_audit_ref: string},
 *            size: string, permanence: string} | {refused: string}}
 */
export function publishRequest(read, release, withApp, held) {
  const offer = publishOffer(read, release, withApp, held);
  if (offer.offer !== "publish") return { refused: offer.offer === "blocked" ? offer.reason : "already sent from this page" };
  if (typeof read.read_at !== "number") return { refused: "Central's read time is not served" };
  return frozen({
    release: { ...release },
    withApp,
    body: { deployment_id: offer.deploymentId, select_app: withApp, operator_audit_ref: auditRef(read.read_at) },
    size: downloadSize(release.download_bytes),
    permanence: `Permanent: Central cannot remove a deployment. If no deployment on base ${release.base_tag} exists `
      + "yet, this also fixes that base's App Manager pins, and a later release with different App Manager pins "
      + "on the same base can never be published.",
  });
}

/** What a page says while it holds a publish (`HeldState`). */
export const PUBLISH_HELD_WORDS = Object.freeze({
  in_flight: "Publishing: Central is downloading and verifying this release",
  recorded: "Published; the next read lists its deployment",
  unknown: "Outcome unknown: Central may still be verifying; the next read that lists its deployment settles it",
});

/**
 * What "Send again" says before it re-sends a publish whose answer was lost (§27): the same
 * body, and the whole download again.
 *
 * @param {object} request the frozen publish this page holds
 * @returns {string}
 */
export const sendAgainWords = (request) => "Sends the identical request again. Central downloads and verifies "
  + `${request.size} from GitHub releases again, even if its first download is still running.`;

/**
 * The Publish confirmation (§26, §27), first send or "Send again": its words and its run, the
 * one home both Releases and Update the wall render, so the verb's words cannot drift between
 * its pages. `lines` are the body's paragraphs; a page may add its own detail below them.
 *
 * @param {object} request `publishRequest`'s frozen request, or, with `again`, `held.frozen(id)`
 * @param {boolean} again
 * @param {{releases: object, held: HeldPublishes}} hooks
 * @returns {{title: string, lines: string[], confirmLabel: string, progress: string,
 *            run: () => Promise<{state: Outcome["outcome"], message: string}>}}
 */
export function publishConfirmation(request, again, { releases, held }) {
  const { release, withApp } = request;
  const choice = withApp ? "with its app" : "without its app";
  return {
    title: again ? `Send the publish of release ${release.tag} ${choice} again?` : `Publish release ${release.tag} ${choice}?`,
    lines: [
      `Base ${release.base_tag} · ${withApp ? `app ${release.app_environment_sha256}` : "no app"}`,
      again ? sendAgainWords(request) : `Central downloads and verifies ${request.size} from GitHub releases before it answers.`,
      request.permanence,
    ],
    confirmLabel: again ? "Send again" : "Publish",
    progress: `Central is downloading and verifying ${request.size} from GitHub releases. Keep this page open.`,
    run: async () => {
      const outcome = await sendPublish(request, releases, held, { again });
      void releases.refresh();
      return { state: outcome.outcome, message: outcome.message };
    },
  };
}

/**
 * THE one send path for a publish: judge the frozen request on `releases.latest()` and this
 * page's held requests, mark it held in the same step (so nothing on this page can send it
 * again while it is held), then POST with the long budget. The held state follows the answer:
 * recorded until a read lists the deployment, unknown when the answer was lost, released when
 * Central refused. `again` is the explicit "Send again" of a publish held unknown: it sends
 * only the very request this page holds for the id (`held.frozen`), never a new body.
 *
 * @param {object} request `publishRequest`'s frozen request, or, with `again`, `held.frozen(id)`
 * @param {{latest: () => ReleaseRead}} releases the `useReleaseRead` hook
 * @param {HeldPublishes} held this page's held publishes
 * @param {{again?: boolean}} [options]
 * @returns {Promise<Outcome>}
 */
export async function sendPublish(request, releases, held, { again = false } = {}) {
  const read = releases.latest().read;
  const offer = publishOffer(read, request.release, request.withApp, held);
  const id = request.body.deployment_id;
  if (offer.offer === "published") return { outcome: "already", message: `Already published as deployment ${deploymentHandle(id)}.` };
  if (offer.offer === "blocked") return { outcome: "changed", message: `${offer.reason}.` };
  const resend = again && offer.offer === "unknown" && held.frozen(id) === request;
  if (resend && !(read?.releases ?? []).some((row) => row.manifest_sha256 === request.release.manifest_sha256)) {
    return { outcome: "changed", message: "Central no longer lists this release." };
  }
  if (offer.offer !== "publish" && !resend) {
    return { outcome: "changed", message: "This page already sent this publish; the next read settles it." };
  }
  held.set(id, "in_flight", request);
  let result;
  try {
    result = await apiWrite(`/v1/operator/node/releases/${request.release.manifest_sha256}/deployments`,
      { method: "POST", body: request.body, timeoutMs: PUBLISH_TIMEOUT_MS });
  } catch {
    result = null;
  }
  const outcome = releaseResult(result, () => `Published as deployment ${deploymentHandle(id)}.`, PUBLISH_CODES);
  held.set(id, outcome.outcome === "done" || outcome.outcome === "already" ? "recorded"
    : outcome.outcome === "unknown" ? "unknown" : null, request);
  return outcome;
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
