import { useCallback, useRef, useState } from "react";

/**
 * A Frame fit (contracts/models.py `PositionBox`, frame box run, .claude/runs/frame-box/page.md):
 * where the Frame's picture sits on its screen as it hangs, as one box: its left and top edges, its
 * width and its height, each a fraction of the hung screen's width or height. The box is the one
 * source of the Frame's shape. The screen's turn is the Frame's own (the Hardware tab), never the fit's.
 *
 * A Frame's lighting (src/generated/contract.ts `FrameLighting`): its mat depth, lights and paper,
 * saved with the Position through the same trial. Central serves every Frame's; the draft holds a
 * copy of it, never a lighting of its own making.
 *
 * THE RECTANGLE ADAPTER (transitional, bead fb-6; bead fb-7 deletes it with the rectangle editor):
 * the Position editor still edits a centred rectangle (its centre and its width). `rectangleOf`
 * gives it the box's rectangle view, and `updateHandles` turns each rectangle patch back into a box
 * of the box's own shape (its height follows its width), so the draft and everything sent stay
 * boxes.
 *
 * @typedef {{mode: "box", left: number, top: number, width: number, height: number}} Fit
 * @typedef {{centre_x: number, centre_y: number, width: number}} RectangleView   the adapter's view of a Fit
 * @typedef {import("./generated/contract").FrameLighting} Lighting
 * @typedef {{fit: Fit, lighting: Lighting|null}} Trying   the operator's draft (lighting null: Central served none yet); its base revision is `base`
 * @typedef {{calibration: object|null, lighting: Lighting|null}} Saved   a saved Position and lighting
 */

/** Inline reason a fit outside FrameBox's ranges (contracts/frame_fit.py) is refused with. */
export const BOX_REASON = "A picture lies on the screen: it starts at or after the screen's left and top edges, "
  + "has some width and height, and ends at or before its right and bottom edges.";

// How far a box's right or bottom edge may pass the screen's in floating point (contracts/frame_fit.py BOX_SLACK).
const BOX_SLACK = 1e-9;

/** Whether `fit` holds FrameBox's ranges: 0 <= left, 0 <= top, 0 < width, 0 < height, and its right
 * and bottom edges at most the screen's (within BOX_SLACK). */
const inRange = ({ left, top, width, height }) =>
  [left, top, width, height].every((value) => typeof value === "number" && Number.isFinite(value))
  && left >= 0 && top >= 0 && width > 0 && height > 0 && width <= 1 && height <= 1
  && left + width <= 1 + BOX_SLACK && top + height <= 1 + BOX_SLACK;

/**
 * The fit a saved position holds, as a fresh copy (so a later edit never mutates the snapshot):
 * its four values, each absent value read as PositionBox's default (the whole screen).
 *
 * @param {object|null|undefined} committed
 * @returns {Fit}
 */
export function fitOf(committed) {
  const fit = committed?.fit ?? {};
  return { mode: "box", left: fit.left ?? 0, top: fit.top ?? 0, width: fit.width ?? 1, height: fit.height ?? 1 };
}

/**
 * The adapter's rectangle view of a box: its centre and its width (bead fb-7 deletes it).
 *
 * @param {Fit} fit
 * @returns {RectangleView}
 */
export function rectangleOf(fit) {
  return { centre_x: fit.left + fit.width / 2, centre_y: fit.top + fit.height / 2, width: fit.width };
}

/** `value`, a floating-point remainder within BOX_SLACK of 0 read as 0. */
const snapped = (value) => (Math.abs(value) < BOX_SLACK ? 0 : value);

/**
 * The box of `fit`'s own shape that `rectangle` (the adapter's view) describes: its width the
 * rectangle's, its height `fit`'s scaled with the width (the same share of the screen's height per
 * share of its width, so the same pixel shape on any screen), centred on the rectangle's centre.
 *
 * @param {Fit} fit
 * @param {RectangleView} rectangle
 * @returns {Fit}
 */
function boxOfRectangle(fit, rectangle) {
  const { width } = rectangle;
  const height = fit.height * (width / fit.width);
  return {
    mode: "box", left: snapped(rectangle.centre_x - width / 2), top: snapped(rectangle.centre_y - height / 2),
    width, height,
  };
}

/**
 * The lighting Central serves for a Frame, as a fresh copy (so a later edit never mutates the
 * snapshot); null while none is served.
 *
 * @param {Lighting|null|undefined} lighting
 * @returns {Lighting|null}
 */
const lightingOf = (lighting) => (lighting == null ? null : structuredClone(lighting));

/** @returns {Trying} */
const seed = (saved) => ({ fit: fitOf(saved?.calibration), lighting: lightingOf(saved?.lighting) });

/** JSON with every object's keys sorted: one text for one value, whatever order its keys came in. */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/**
 * A draft's identity for comparison: its fit and its lighting. Takes a draft (`{fit, lighting}`)
 * or what Central serves (`calibration`, `lighting`) as `draftKey(calibration, lighting)`.
 *
 * @param {object|null|undefined} value a draft, or a calibration
 * @param {Lighting|null} [lighting] the lighting beside a calibration (a draft carries its own)
 */
export const draftKey = (value, lighting) => {
  const fit = fitOf(value);
  return canonical([fit.left, fit.top, fit.width, fit.height, lightingOf(lighting === undefined ? value?.lighting : lighting)]);
};

/**
 * Plane B edit-draft hook (shared primitive #2).
 *
 * Holds the operator's "trying" Position and lighting for one Frame as COMPONENT-LOCAL React state
 * seeded from that Frame's saved ones. This is the second of the two planes (design §4a/§6b): a snapshot
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
 * is whether the draft's fit or lighting differs from its origin's. `changeLighting` replaces the
 * draft's lighting (FrameLighting checks its own bounds).
 *
 * `changeFit(fit)` replaces the draft's box (a Restore) and `updateHandles(patch)` merges a partial
 * rectangle view `{centre_x?, centre_y?, width?}` into it (the adapter, above), each first checking
 * FrameBox's ranges (contracts/frame_fit.py). An INVALID change does not mutate `trying` and
 * returns BOX_REASON; a valid one commits and returns `{valid: true}`.
 *
 * @param {string} frameId identity of the Frame being edited
 * @param {object|null|undefined} committed the Frame's saved Position (Plane A)
 * @param {Lighting|null|undefined} committedLighting the Frame's saved lighting (Plane A)
 * @returns {{trying: Trying, origin: Saved, base: number|null, dirty: boolean,
 *            updateHandles: (patch: Partial<RectangleView>) => {valid: boolean, reason?: string},
 *            changeFit: (fit: Fit) => {valid: boolean, reason?: string},
 *            changeLighting: (lighting: Lighting) => void,
 *            clearDraft: () => void, adopt: (calibration: object, lighting: Lighting|null) => void}}
 */
export function useDraft(frameId, committed, committedLighting) {
  const saved = () => ({ calibration: committed ?? null, lighting: committedLighting ?? null });
  const [draft, setDraft] = useState(() => ({ trying: seed(saved()), origin: saved() }));
  // Track the frame the current draft was seeded for. Re-seeding on a refresh is keyed on
  // this identity ALONE — a refresh mutates `committed` but not `frameId`, so the two-plane
  // rule ("refresh merges nothing into Plane B") is structural, not a convention someone
  // must remember to honor.
  const [seededFor, setSeededFor] = useState(frameId);

  if (seededFor !== frameId) {
    setSeededFor(frameId);
    setDraft({ trying: seed(saved()), origin: saved() });
  }

  // Latest values read synchronously by the mutators without re-binding them on
  // every draft edit: `tryingRef` for merge-against-current, `committedRef` so
  // clearDraft resets to the freshest committed baseline.
  const tryingRef = useRef(draft.trying);
  tryingRef.current = draft.trying;
  const committedRef = useRef(saved());
  committedRef.current = saved();

  const changeFit = useCallback((next) => {
    const fit = fitOf({ fit: next });
    if (!inRange(fit)) return { valid: false, reason: BOX_REASON };
    const candidate = { ...tryingRef.current, fit };
    tryingRef.current = candidate;
    setDraft((current) => ({ ...current, trying: candidate }));
    return { valid: true };
  }, []);
  const updateHandles = useCallback((patch) => {
    const { fit } = tryingRef.current;
    return changeFit(boxOfRectangle(fit, { ...rectangleOf(fit), ...patch }));
  }, [changeFit]);
  const changeLighting = useCallback((lighting) => {
    const candidate = { ...tryingRef.current, lighting: lightingOf(lighting) };
    tryingRef.current = candidate;
    setDraft((current) => ({ ...current, trying: candidate }));
  }, []);

  const adopt = useCallback((calibration, lighting) => {
    const origin = { calibration: calibration ?? null, lighting: lighting ?? null };
    tryingRef.current = seed(origin);
    setDraft({ trying: tryingRef.current, origin });
  }, []);
  const clearDraft = useCallback(() => adopt(committedRef.current.calibration, committedRef.current.lighting), [adopt]);

  const { trying, origin } = draft;
  return {
    trying,
    origin,
    base: origin.calibration?.revision ?? null,
    dirty: draftKey(trying) !== draftKey(seed(origin)),
    updateHandles,
    changeFit,
    changeLighting,
    clearDraft,
    adopt,
  };
}
