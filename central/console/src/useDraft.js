import { useCallback, useRef, useState } from "react";

import { cropValid, isConvex } from "./convex.js";

/**
 * @typedef {{corners: number[][], crop: number[], rotation: number, gain: number}} Trying
 */

/** Inline reasons an invalid handle patch snaps back with (design §J2 table). */
export const CONVEX_REASON = "corners must form a convex aperture";
export const CROP_REASON = "crop must describe a nonempty rectangle";

const DEFAULT_CORNERS = [
  [0, 0],
  [1, 0],
  [1, 1],
  [0, 1],
];
const DEFAULT_CROP = [0, 0, 1, 1];

/**
 * Seed a fresh `Trying` from a committed `Calibration`, defensively copying the
 * nested arrays so a later handle edit can never mutate the snapshot in place.
 *
 * @param {object|null|undefined} committed
 * @returns {Trying}
 */
function seed(committed) {
  const calibration = committed ?? {};
  const corners = Array.isArray(calibration.corners) ? calibration.corners : DEFAULT_CORNERS;
  const crop = Array.isArray(calibration.crop) ? calibration.crop : DEFAULT_CROP;
  return {
    corners: corners.map((point) => [point[0], point[1]]),
    crop: [...crop],
    rotation: calibration.rotation ?? 0,
    gain: calibration.gain ?? 1,
  };
}

/**
 * Plane B edit-draft hook (shared primitive #2).
 *
 * Holds the operator's "trying" calibration for one Frame as COMPONENT-LOCAL
 * React state seeded from that Frame's committed calibration. This is the second
 * of the two planes (design §4a/§6b): a snapshot refresh replaces Plane A
 * (`useSnapshot`) wholesale and CANNOT reach this state cell, so an in-progress
 * edit survives every refresh. The draft is re-seeded ONLY when the Frame
 * identity (`frameId`) changes — never when `committed` changes underneath an
 * open draft (that is a "committed changed underneath you" case a later bead
 * surfaces, not a silent clobber).
 *
 * `updateHandles(patch)` merges a partial `{corners?, crop?, rotation?, gain?}`
 * into the draft, first validating any geometry it touches with the SAME convex
 * test + `1e-6` epsilon + winding as the server (see convex.js). An INVALID
 * patch does not mutate `trying` (the handle snaps back) and returns a reason;
 * a valid patch commits and returns `{valid: true}`.
 *
 * @param {string} frameId identity of the Frame being edited
 * @param {object|null|undefined} committed the Frame's committed calibration (Plane A)
 * @returns {{trying: Trying, updateHandles: (patch: Partial<Trying>) => {valid: boolean, reason?: string}, clearDraft: () => void}}
 */
export function useDraft(frameId, committed) {
  const [trying, setTrying] = useState(() => seed(committed));
  // Track the frame the current draft was seeded for. Re-seeding is keyed on
  // this identity ALONE — a refresh mutates `committed` but not `frameId`, so
  // the two-plane rule ("refresh merges nothing into Plane B") is structural,
  // not a convention someone must remember to honor.
  const [seededFor, setSeededFor] = useState(frameId);

  if (seededFor !== frameId) {
    setSeededFor(frameId);
    setTrying(seed(committed));
  }

  // Latest values read synchronously by the mutators without re-binding them on
  // every draft edit: `tryingRef` for merge-against-current, `committedRef` so
  // clearDraft resets to the freshest committed baseline.
  const tryingRef = useRef(trying);
  tryingRef.current = trying;
  const committedRef = useRef(committed);
  committedRef.current = committed;

  const updateHandles = useCallback((patch) => {
    const candidate = { ...tryingRef.current, ...patch };
    if ("corners" in patch && !isConvex(candidate.corners)) {
      return { valid: false, reason: CONVEX_REASON };
    }
    if ("crop" in patch && !cropValid(candidate.crop)) {
      return { valid: false, reason: CROP_REASON };
    }
    setTrying(candidate);
    return { valid: true };
  }, []);

  const clearDraft = useCallback(() => {
    setTrying(seed(committedRef.current));
  }, []);

  return { trying, updateHandles, clearDraft };
}
