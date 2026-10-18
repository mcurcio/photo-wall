import { useCallback, useRef, useState } from "react";

/**
 * A Frame fit (contracts/models.py `RectangleFit`, render redesign S5): where the Frame's picture
 * sits on its screen as it hangs, as one upright rectangle of the Frame profile's shape: its
 * centre as fractions of the hung screen's width and height, its width as a fraction of that
 * width. The screen's turn is the Frame's own (the Hardware tab), never the fit's.
 *
 * A Frame's lighting (src/generated/contract.ts `FrameLighting`): its mat depth, lights and paper,
 * saved with the Position through the same trial. Central serves every Frame's; the draft holds a
 * copy of it, never a lighting of its own making.
 *
 * @typedef {{mode: "rectangle", centre_x: number, centre_y: number, width: number}} Fit
 * @typedef {import("./generated/contract").FrameLighting} Lighting
 * @typedef {{fit: Fit, lighting: Lighting|null}} Trying   the operator's draft (lighting null: Central served none yet); its base revision is `base`
 * @typedef {{calibration: object|null, lighting: Lighting|null}} Saved   a saved Position and lighting
 */

/** Inline reason a patch outside FrameRectangle's ranges (contracts/frame_fit.py) is refused with. */
export const RECTANGLE_REASON = "A picture's centre is on the screen and its width is more than none and at most the screen's.";

/** Whether `fit` holds FrameRectangle's ranges: 0 <= centre <= 1 on each axis, 0 < width <= 1. */
const inRange = ({ centre_x: x, centre_y: y, width }) =>
  [x, y, width].every((value) => typeof value === "number" && Number.isFinite(value))
  && x >= 0 && x <= 1 && y >= 0 && y <= 1 && width > 0 && width <= 1;

/**
 * The fit a saved position holds, as a fresh copy (so a later edit never mutates the snapshot):
 * its centre and width, each absent value read as RectangleFit's default (centred, full width).
 *
 * @param {object|null|undefined} committed
 * @returns {Fit}
 */
export function fitOf(committed) {
  const fit = committed?.fit ?? {};
  return { mode: "rectangle", centre_x: fit.centre_x ?? 0.5, centre_y: fit.centre_y ?? 0.5, width: fit.width ?? 1 };
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
  return canonical([fit.centre_x, fit.centre_y, fit.width, lightingOf(lighting === undefined ? value?.lighting : lighting)]);
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
 * `updateHandles(patch)` merges a partial fit `{centre_x?, centre_y?, width?}` into the draft,
 * first checking FrameRectangle's ranges (contracts/frame_fit.py: each centre from 0 to 1, the
 * width above 0 and at most 1). An INVALID patch does not mutate `trying` and returns
 * RECTANGLE_REASON; a valid patch commits and returns `{valid: true}`. Whether the rectangle lies
 * on its screen needs the screen and the Frame's shape: the editor judges that by src/placement.js
 * `place` before it calls here.
 *
 * @param {string} frameId identity of the Frame being edited
 * @param {object|null|undefined} committed the Frame's saved Position (Plane A)
 * @param {Lighting|null|undefined} committedLighting the Frame's saved lighting (Plane A)
 * @returns {{trying: Trying, origin: Saved, base: number|null, dirty: boolean,
 *            updateHandles: (patch: Partial<Fit>) => {valid: boolean, reason?: string},
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

  const updateHandles = useCallback((patch) => {
    const { centre_x, centre_y, width } = { ...tryingRef.current.fit, ...patch };
    const fit = { mode: "rectangle", centre_x, centre_y, width };
    if (!inRange(fit)) return { valid: false, reason: RECTANGLE_REASON };
    const candidate = { ...tryingRef.current, fit };
    tryingRef.current = candidate;
    setDraft((current) => ({ ...current, trying: candidate }));
    return { valid: true };
  }, []);
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
    changeLighting,
    clearDraft,
    adopt,
  };
}
