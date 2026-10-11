import { useCallback, useRef, useState } from "react";

import { isConvex } from "./convex.js";

/**
 * A Frame fit (contracts/models.py `CornersFit` | `CoverFit`, roadmap 1c): how the Frame's picture
 * sits in its four corners. **Cover** (the default, owner 2026-10-10 q1): the picture keeps its
 * shape, turned `rotation` degrees clockwise (any angle, 0 <= r < 360), and is the smallest such
 * rectangle that holds every corner; nothing is drawn outside the corners. **Corners** (an
 * explicit opt-in): the picture is stretched exactly onto the corners, a quarter turn picking its
 * top.
 *
 * @typedef {[number, number]} Point
 * @typedef {{mode: "cover"|"corners", corners: Point[], rotation: number}} Fit
 * @typedef {{fit: Fit}} Trying   the operator's draft; its base revision is `base`
 */

/** Inline reason an invalid corner patch snaps back with (design §J2 table). */
export const CONVEX_REASON = "corners must form a convex aperture";

/** The four quarter turns: Corners' only rotations, Cover's presets. */
export const QUARTER_TURNS = [0, 90, 180, 270];

const FULL_SCREEN = [
  [0, 0],
  [1, 0],
  [1, 1],
  [0, 1],
];

/** The nearest quarter turn to `rotation` degrees (Corners takes only those). */
export const quarterTurn = (rotation) => (Math.round(rotation / 90) * 90) % 360;

/**
 * The fit a saved position holds, as a fresh copy (so a later edit never mutates the snapshot).
 * With no saved position, Cover on the whole output at angle 0 (owner 2026-10-10 q1).
 *
 * @param {object|null|undefined} committed
 * @returns {Fit}
 */
export function fitOf(committed) {
  const fit = committed?.fit ?? { mode: "cover", corners: FULL_SCREEN, rotation: 0 };
  return {
    mode: fit.mode,
    corners: (fit.corners ?? FULL_SCREEN).map((point) => [point[0], point[1]]),
    rotation: fit.rotation ?? 0,
  };
}

/** @returns {Trying} */
const seed = (committed) => ({ fit: fitOf(committed) });

/** A draft's identity for comparison (a draft, or a calibration Central served): its fit only. */
export const draftKey = (value) => {
  const fit = fitOf(value);
  return JSON.stringify([fit.mode, fit.corners, fit.rotation]);
};

/**
 * Plane B edit-draft hook (shared primitive #2).
 *
 * Holds the operator's "trying" Position for one Frame as COMPONENT-LOCAL React state seeded from
 * that Frame's saved one. This is the second of the two planes (design §4a/§6b): a snapshot
 * refresh replaces Plane A (`useSnapshot`) wholesale and CANNOT reach this state cell, so an
 * in-progress edit survives every refresh. The draft is re-seeded when the Frame identity
 * (`frameId`) changes, by `clearDraft` (Revert) and by `adopt` — never when `committed` changes
 * underneath an open draft.
 *
 * THE DRAFT'S BASE. A draft is made from one saved Position, its `origin`; `base` is that
 * Position's `revision`. It is set whenever the draft is seeded, cleared or adopted (a Done hands
 * the saved Position to `adopt`), and only then, so it is the draft's own, whatever session shows
 * the draft: the live adjustment sends it with every draft, and Central refuses a draft whose
 * base is no longer the saved revision (someone saved meanwhile) instead of re-basing it. `dirty`
 * is whether the draft's fit differs from its origin's.
 *
 * `updateHandles(patch)` merges a partial fit `{mode?, corners?, rotation?}` into the draft,
 * first validating any corners it touches with the SAME convex test + `1e-6` epsilon + winding as
 * the server (see convex.js). An INVALID patch does not mutate `trying` (the handle snaps back)
 * and returns a reason; a valid patch commits and returns `{valid: true}`. Choosing Corners turns
 * the picture to the nearest quarter turn, the only rotations Corners takes.
 *
 * @param {string} frameId identity of the Frame being edited
 * @param {object|null|undefined} committed the Frame's saved Position (Plane A)
 * @returns {{trying: Trying, origin: object|null, base: number|null, dirty: boolean,
 *            updateHandles: (patch: Partial<Fit>) => {valid: boolean, reason?: string},
 *            clearDraft: () => void, adopt: (calibration: object) => void}}
 */
export function useDraft(frameId, committed) {
  const [draft, setDraft] = useState(() => ({ trying: seed(committed), origin: committed ?? null }));
  // Track the frame the current draft was seeded for. Re-seeding on a refresh is keyed on
  // this identity ALONE — a refresh mutates `committed` but not `frameId`, so the two-plane
  // rule ("refresh merges nothing into Plane B") is structural, not a convention someone
  // must remember to honor.
  const [seededFor, setSeededFor] = useState(frameId);

  if (seededFor !== frameId) {
    setSeededFor(frameId);
    setDraft({ trying: seed(committed), origin: committed ?? null });
  }

  // Latest values read synchronously by the mutators without re-binding them on
  // every draft edit: `tryingRef` for merge-against-current, `committedRef` so
  // clearDraft resets to the freshest committed baseline.
  const tryingRef = useRef(draft.trying);
  tryingRef.current = draft.trying;
  const committedRef = useRef(committed);
  committedRef.current = committed;

  const updateHandles = useCallback((patch) => {
    const fit = { ...tryingRef.current.fit, ...patch };
    if ("corners" in patch && !isConvex(fit.corners)) {
      return { valid: false, reason: CONVEX_REASON };
    }
    if (fit.mode === "corners") fit.rotation = quarterTurn(fit.rotation);
    const candidate = { fit };
    tryingRef.current = candidate;
    setDraft((current) => ({ ...current, trying: candidate }));
    return { valid: true };
  }, []);

  const adopt = useCallback((calibration) => {
    tryingRef.current = seed(calibration);
    setDraft({ trying: tryingRef.current, origin: calibration ?? null });
  }, []);
  const clearDraft = useCallback(() => adopt(committedRef.current), [adopt]);

  const { trying, origin } = draft;
  return {
    trying,
    origin,
    base: origin?.revision ?? null,
    dirty: draftKey(trying) !== draftKey(seed(origin)),
    updateHandles,
    clearDraft,
    adopt,
  };
}
