/**
 * mm -> px SVG projection + Unplaced routing (shared primitive #3).
 *
 * A pure module imported by Plan.jsx and UnplacedTray.jsx. It carries the ONE
 * decision about which frames are drawable on a Surface plan and which are
 * legacy origin-stacked frames that belong in the Unplaced tray (design §1/§6,
 * J3). Keeping both the placement projection and the tray routing here means the
 * tray and the plan can never disagree about a frame's fate.
 */

/** @typedef {{x:number,y:number,w:number,h:number}} Rect */

/**
 * The size, in millimetres on the wall, of the dashed outline the plan draws for a Frame whose
 * size is not known yet (frame box run decision 6: bound to nothing, or on a Display that states
 * no size). Not the Frame's size: a placeholder that only shows where it hangs.
 */
export const UNSIZED_FRAME_MM = [500, 300];

/**
 * The Frame's drawn size on the wall: Central's `size_mm` (its Position box's share of its
 * Display's own size, as its screen hangs; central/registry.py `_frame_inventory`), else the
 * UNSIZED_FRAME_MM outline.
 *
 * @param {{size_mm?: [number, number]|null}} frame
 * @returns {{size: [number, number], sized: boolean}}
 */
export function wallSize(frame) {
  return Array.isArray(frame.size_mm) ? { size: frame.size_mm, sized: true }
    : { size: UNSIZED_FRAME_MM, sized: false };
}

/**
 * True when a frame has no distinct position and therefore belongs in the
 * Unplaced tray rather than on the plan.
 *
 * Per design §1/§6 every legacy frame was created by the old flat UI at
 * `wall`/(0,0), so the honest, verifiable heuristic for "no distinct position"
 * is "sitting at the origin": `x_mm === 0 && y_mm === 0`. Such a frame is routed
 * to the tray by identity instead of being drawn stacked at the origin, where it
 * would overlap every other origin frame into an unreadable pile. A frame with
 * any non-zero placement coordinate is treated as deliberately placed.
 *
 * @param {{x_mm: number, y_mm: number}} frame
 * @returns {boolean}
 */
export function isUnplaced(frame) {
  return frame.x_mm === 0 && frame.y_mm === 0;
}

/**
 * Project the frames of one Surface from mm into px for the given viewport.
 *
 * Each placed frame (distinct position) is drawn from its top-left (`x_mm`, `y_mm`) at its
 * {@link wallSize}; all are scaled uniformly to fit the bounding box of that Surface's placed
 * frames into the viewport, and `sized` says whether the rect is the Frame's own size or the
 * not-yet-known outline. Origin-stacked frames are returned by id in `unplaced` and never
 * projected.
 *
 * @param {Array<{id: string, surface_id: string, x_mm: number, y_mm: number, size_mm?: [number, number]|null}>} frames
 * @param {string} surfaceId
 * @param {{width: number, height: number}} viewport
 * @returns {{placed: Array<{id: string, rect: Rect, sized: boolean}>, unplaced: string[]}}
 */
export function project(frames, surfaceId, viewport) {
  const onSurface = (frames ?? []).filter((frame) => frame.surface_id === surfaceId);
  const placedFrames = onSurface.filter((frame) => !isUnplaced(frame))
    .map((frame) => ({ frame, ...wallSize(frame) }));
  const unplaced = onSurface.filter(isUnplaced).map((frame) => frame.id);

  if (placedFrames.length === 0) {
    return { placed: [], unplaced };
  }

  const minX = Math.min(...placedFrames.map(({ frame }) => frame.x_mm));
  const minY = Math.min(...placedFrames.map(({ frame }) => frame.y_mm));
  const maxX = Math.max(...placedFrames.map(({ frame, size }) => frame.x_mm + size[0]));
  const maxY = Math.max(...placedFrames.map(({ frame, size }) => frame.y_mm + size[1]));
  const spanX = maxX - minX;
  const spanY = maxY - minY;

  // Uniform scale-to-fit. A zero span (degenerate placement) falls back to 1 so
  // the scale stays finite rather than dividing by zero.
  const scaleX = spanX > 0 ? viewport.width / spanX : 1;
  const scaleY = spanY > 0 ? viewport.height / spanY : 1;
  const scale = Math.min(scaleX, scaleY);

  const placed = placedFrames.map(({ frame, size, sized }) => ({
    id: frame.id,
    rect: {
      x: (frame.x_mm - minX) * scale,
      y: (frame.y_mm - minY) * scale,
      w: size[0] * scale,
      h: size[1] * scale,
    },
    sized,
  }));

  return { placed, unplaced };
}

/**
 * Physical wall span (mm) that the plan viewport's WIDTH represents.
 *
 * `project()` (above) draws the plan scale-to-fit against the bounding box of the
 * Surface's placed frames, so there is NO single global mm↔px scale to invert —
 * the forward scale is data-dependent and changes as frames are added. For the
 * INVERSE placement of a fresh pointer press we therefore adopt ONE documented
 * convention: the viewport width maps to a 4 m wall. This is a deliberate,
 * flagged choice (see the report) — a placed/moved frame is NOT guaranteed
 * to render at the exact same pixels after the write, because `project()` re-fits
 * on the next snapshot; the design explicitly accepts this ("the plan corrects on
 * the next snapshot", design §9a/J3).
 */
const WALL_SPAN_MM = 4000;

/**
 * The UNSIZED_FRAME_MM outline in viewBox px under the WALL_SPAN_MM convention: what the plan
 * draws under the pointer while a new Frame is being placed (its size is not known until it is
 * bound to a Display that states one).
 *
 * @param {{width: number, height: number}} viewport the plan viewport in px
 * @returns {{w: number, h: number}}
 */
export function unsizedOutlinePx(viewport) {
  const pxPerMm = viewport.width / WALL_SPAN_MM;
  return { w: UNSIZED_FRAME_MM[0] * pxPerMm, h: UNSIZED_FRAME_MM[1] * pxPerMm };
}

/**
 * Invert a pointer position (px, in the plan's viewBox space) back to where a Frame hangs on the
 * wall: its top-left in mm, the px→mm inverse of `project()`'s forward mapping (Bead 10, design
 * J3/§9a; frame box run fb-14: the wall placement is the Frame's top-left alone, its size comes
 * from its Display).
 *
 * A single uniform `mmPerPx` (derived from the viewport WIDTH, matching `project()`'s single
 * `min()` scale) is applied to both coordinates.
 *
 * ORIGIN NUDGE (errata 2026-09-13, "isUnplaced origin heuristic"): `isUnplaced`
 * routes a frame at exactly `x_mm === 0 && y_mm === 0` to the Unplaced tray. A
 * press that lands at the plan origin would therefore be mis-routed into the tray
 * instead of being drawn. This inverse nudges the exact-origin case to a minimal
 * non-origin offset (`x_mm = 1`) so a placed frame ALWAYS has a distinct
 * position and renders on the plan. `isUnplaced`'s read-only heuristic is left
 * untouched (per the errata's instruction).
 *
 * @param {{x: number, y: number}} point the Frame's top-left in viewBox px
 * @param {{width: number, height: number}} viewport the plan viewport in px
 * @param {string} surfaceId the Surface the frame is being placed on
 * @returns {{surface_id: string, x_mm: number, y_mm: number}}
 */
export function pointToPlacement(point, viewport, surfaceId) {
  const mmPerPx = WALL_SPAN_MM / viewport.width;
  let x_mm = Math.round(point.x * mmPerPx);
  const y_mm = Math.round(point.y * mmPerPx);
  if (x_mm === 0 && y_mm === 0) {
    // Never emit the exact origin — see ORIGIN NUDGE above.
    x_mm = 1;
  }
  return { surface_id: surfaceId, x_mm, y_mm };
}
