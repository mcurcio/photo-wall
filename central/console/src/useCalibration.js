import { useCallback, useEffect, useRef, useState } from "react";

import { getToken, useSnapshot } from "./useSnapshot.js";
import { useMutate } from "./useMutate.js";

/**
 * @typedef {"committed"|"previewing"|"expired"|"overtaken"|"conflict"} CalibrationStatus
 * @typedef {"revision"|"generation"|"unbound"|"error"} CalibrationConflict
 */

// Cadence of the overtake poll while the facet is mounted (design §6b: poll
// /inventory ~5s so a committed-elsewhere change or an expired lease surfaces
// without operator action). The poll refreshes Plane A wholesale; useDraft
// (Plane B) is refresh-proof, so an in-progress edit is never clobbered.
const POLL_MS = 5000;

/**
 * POST one calibration op against the EXISTING route. On a non-2xx the server
 * body is `{"error": "<code>"}` (central/app.py:288); the code is re-thrown so
 * {@link useCalibration} can map the two 409 conflict codes precisely:
 *   - `calibration_revision_conflict` (stale expected_revision)
 *   - `binding_generation_conflict`  (stale expected_generation)
 *   - `frame_unbound`                (preview/commit on an unbound frame)
 *
 * @param {string} frameId
 * @param {object} body the CalibrationRequest payload
 * @returns {Promise<object>} the parsed success body (preview: {calibration, expires_at})
 */
async function postCalibration(frameId, body) {
  const response = await fetch(`/v1/operator/frames/${frameId}/calibration`, {
    method: "POST",
    headers: {
      Authorization: "Bearer " + getToken(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    let code = "request_failed";
    try {
      const data = await response.json();
      code = data?.error ?? code;
    } catch {
      // A non-JSON error body leaves the generic code in place.
    }
    const error = new Error(code);
    error.code = code;
    error.httpStatus = response.status;
    throw error;
  }
  return response.json();
}

/**
 * Calibration control hook for the open Commissioning facet (Bead 8, design
 * §4b/§6b/J2). HIGH-RISK concurrency surface — the load-bearing guarantees:
 *
 *  1. EVERY op carries BOTH optimistic tokens — `expected_revision` AND
 *     `expected_generation` — read from a BASELINE captured when the facet opened
 *     (seeded from Plane A's committed calibration/generation), NOT from the
 *     perpetually-refreshed live snapshot. Sending the baseline is what makes a
 *     stale commit 409 instead of silently succeeding against state the operator
 *     never reviewed: if committed advanced underneath, the baseline token no
 *     longer matches the server and the write is refused.
 *  2. The countdown is derived from the SERVER's `expires_at` (from the preview
 *     response), never a per-tab claim; there is NO client auto-renew and NO
 *     silent re-preview. The countdown reaching zero does NOT flip the panel to
 *     "expired" — expiry is authoritative SERVER state, detected by the poll (see
 *     3), so a skewed or slow client clock can never fake or hide expiry.
 *  3. A ~5s poll of /inventory (Plane A refresh) detects, against the baseline:
 *     a committed-elsewhere `revision` advance or a `generation` change →
 *     "overtaken"; and, while previewing, the frame's `preview` reverting to null
 *     (the lease lapsed server-side) → "expired". The trying values stay in Plane
 *     B (useDraft is never cleared here) so "Re-preview" re-issues the same op.
 *  4. Only `commit` wraps {@link useMutate} (refresh Plane A once on success);
 *     baseline.revision is advanced to the committed revision INSIDE that write,
 *     before the refresh, so the post-commit snapshot never reads as an overtake
 *     of our own commit.
 *
 * The `trying` calibration is supplied by the caller's {@link useDraft} (Plane B).
 * The frozen sketch wrote `useCalibration(frameId)`; the draft it must preview/
 * commit lives in the sibling hook, so it is threaded in as a second argument —
 * `calibrate(op)` itself keeps the frozen `(op) => Promise` shape.
 *
 * @param {string} frameId
 * @param {{corners:number[][], crop:number[], rotation:number, gain:number}} trying
 * @returns {{
 *   calibrate: (op: "preview"|"commit"|"revert") => Promise<{ok:true}|{ok:false, conflict: CalibrationConflict}>,
 *   countdown: number|null,
 *   status: CalibrationStatus
 * }}
 */
export function useCalibration(frameId, trying) {
  const { snapshot, refresh } = useSnapshot();
  const mutate = useMutate();

  const [status, setStatus] = useState(/** @type {CalibrationStatus} */ ("committed"));
  const [countdown, setCountdown] = useState(/** @type {number|null} */ (null));

  // Refs the async ops and the detection effect read synchronously without
  // re-binding on every render.
  const baselineRef = useRef(/** @type {{revision:number, generation:number, configuration_revision:number}|null} */ (null));
  const seededForRef = useRef(/** @type {string|null} */ (null));
  const expiresAtRef = useRef(/** @type {number|null} */ (null));
  // The configuration_revision our OWN preview is expected to reach. A preview
  // bumps configuration_revision by exactly one (registry.py:339) and touches
  // neither revision nor generation, so a second tab overtaking the single
  // preview slot is invisible to the token check — it shows ONLY as a FURTHER
  // configuration_revision advance beyond this recorded self-bump while the
  // preview slot stays occupied. Null whenever we are not the previewing owner.
  const previewConfigRevisionRef = useRef(/** @type {number|null} */ (null));
  const statusRef = useRef(status);
  statusRef.current = status;
  const tryingRef = useRef(trying);
  tryingRef.current = trying;

  const frame = (snapshot?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);

  // Seed the baseline ONCE per frame identity, from the committed calibration in
  // Plane A. Keyed on frameId alone (the useDraft pattern): a later Plane A
  // refresh moves committed underneath, but the tokens we send stay the values
  // the operator opened on — a refresh can never quietly re-baseline a stale
  // commit into a fresh one.
  if (frame && seededForRef.current !== frameId) {
    seededForRef.current = frameId;
    baselineRef.current = {
      revision: frame.calibration?.revision ?? 1,
      generation: frame.generation ?? 0,
      configuration_revision: frame.configuration_revision ?? 1,
    };
    expiresAtRef.current = null;
    previewConfigRevisionRef.current = null;
    setStatus("committed");
    setCountdown(null);
  }

  // Overtake / expiry detection: runs whenever Plane A changes (the ~5s poll, an
  // after-commit refresh, or a manual Connect). Compares the live frame to the
  // captured baseline. A "conflict" is sticky (an op already refused the write)
  // until the frame is reselected.
  useEffect(() => {
    const baseline = baselineRef.current;
    if (!baseline) {
      return;
    }
    const live = (snapshot?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
    if (!live) {
      return;
    }
    const current = statusRef.current;
    if (current === "conflict") {
      return;
    }
    const revision = live.calibration?.revision ?? 1;
    const generation = live.generation ?? 0;
    // Either concurrency token advancing under us is an overtake (committed
    // elsewhere / binding changed) — surfaced without an op.
    if (revision !== baseline.revision || generation !== baseline.generation) {
      setStatus("overtaken");
      setCountdown(null);
      expiresAtRef.current = null;
      return;
    }
    if (current === "previewing") {
      if (live.preview == null) {
        // Our lease lapsed server-side: the panel is back on committed. Absorb
        // the expiry's configuration_revision bump so it is not later misread as
        // a third-party change.
        baseline.configuration_revision = live.configuration_revision ?? baseline.configuration_revision;
        expiresAtRef.current = null;
        previewConfigRevisionRef.current = null;
        setCountdown(null);
        setStatus("expired");
      } else {
        // The preview slot is still occupied, but the slot is single and
        // last-writer-wins: a SECOND tab's preview overtakes it while advancing
        // ONLY configuration_revision (registry.py:339), leaving revision and
        // generation — the token check above — untouched. Our own preview caused
        // exactly ONE bump, recorded in previewConfigRevisionRef; any advance
        // BEYOND that value is a foreign write that took the slot from us (§4b:
        // configuration_revision advanced during calibration → superseded). We
        // must stop claiming exclusive control (§4c).
        const liveConfig = live.configuration_revision ?? baseline.configuration_revision;
        const ownConfig = previewConfigRevisionRef.current ?? baseline.configuration_revision;
        if (liveConfig > ownConfig) {
          setStatus("overtaken");
          setCountdown(null);
          expiresAtRef.current = null;
          previewConfigRevisionRef.current = null;
        } else {
          // Only our own single self-bump so far; absorb it so a later foreign
          // bump still reads as a further advance.
          baseline.configuration_revision = liveConfig;
        }
      }
    } else if (current === "committed") {
      // A foreign preview on this frame (config advanced with a live preview set,
      // tokens unchanged) is an out-of-band change; otherwise absorb the value.
      if (
        (live.configuration_revision ?? baseline.configuration_revision) > baseline.configuration_revision &&
        live.preview != null
      ) {
        setStatus("overtaken");
      } else {
        baseline.configuration_revision = live.configuration_revision ?? baseline.configuration_revision;
      }
    }
  }, [snapshot, frameId]);

  // The ~5s overtake poll. Refreshing Plane A drives the detection effect above;
  // it is the ONLY timer here — there is no lease-renew timer, by design.
  useEffect(() => {
    const id = setInterval(() => {
      refresh().catch(() => {});
    }, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  // Countdown ticker (display only). Recomputes remaining seconds from the
  // server's expires_at each tick; clamped at 0. Reaching 0 does NOT expire the
  // lease — only the server-state poll does.
  useEffect(() => {
    if (status !== "previewing") {
      return undefined;
    }
    const tick = () => {
      const expiresAt = expiresAtRef.current;
      if (expiresAt == null) {
        setCountdown(null);
        return;
      }
      setCountdown(Math.max(0, Math.round(expiresAt - Date.now() / 1000)));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [status]);

  const calibrate = useCallback(
    async (op) => {
      const baseline = baselineRef.current;
      if (!baseline) {
        return { ok: false, conflict: "error" };
      }
      const body = {
        operation: op,
        expected_revision: baseline.revision,
        expected_generation: baseline.generation,
      };
      if (op === "preview" || op === "commit") {
        const draft = tryingRef.current;
        body.calibration = {
          corners: draft.corners,
          crop: draft.crop,
          rotation: draft.rotation,
          gain: draft.gain,
        };
      }
      const run = async () => {
        const result = await postCalibration(frameId, body);
        if (op === "commit") {
          // Advance the baseline to the committed revision BEFORE useMutate's
          // refresh lands, so our own commit is never read back as an overtake.
          baseline.revision = result.revision ?? baseline.revision + 1;
        }
        return result;
      };
      try {
        const result = op === "commit" ? await mutate(run) : await run();
        if (op === "preview") {
          expiresAtRef.current = result.expires_at ?? null;
          // Our own preview bumps configuration_revision by exactly one; record
          // the value it reaches so the poll can tell our self-bump from a
          // foreign preview that later overtakes the single slot.
          previewConfigRevisionRef.current = baseline.configuration_revision + 1;
          setStatus("previewing");
        } else {
          // commit and revert both return the panel to committed.
          expiresAtRef.current = null;
          previewConfigRevisionRef.current = null;
          setCountdown(null);
          setStatus("committed");
        }
        return { ok: true };
      } catch (error) {
        const code = error?.code;
        if (code === "calibration_revision_conflict") {
          setStatus("conflict");
          return { ok: false, conflict: "revision" };
        }
        if (code === "binding_generation_conflict") {
          setStatus("conflict");
          return { ok: false, conflict: "generation" };
        }
        if (code === "frame_unbound") {
          setStatus("conflict");
          return { ok: false, conflict: "unbound" };
        }
        // Any other failure (validation, network) is surfaced generically — it is
        // NOT mapped to a specific token conflict, so a dropped token (422) can
        // never masquerade as a clean revision conflict.
        return { ok: false, conflict: "error" };
      }
    },
    [frameId, mutate],
  );

  return { calibrate, countdown, status };
}
