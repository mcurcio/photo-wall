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
 * Placed frames (distinct position) are scaled uniformly to fit the bounding box
 * of that Surface's placed frames into the viewport; origin-stacked frames are
 * returned by id in `unplaced` and never projected.
 *
 * @param {Array<{id: string, surface_id: string, x_mm: number, y_mm: number, width_mm: number, height_mm: number}>} frames
 * @param {string} surfaceId
 * @param {{width: number, height: number}} viewport
 * @returns {{placed: Array<{id: string, rect: Rect}>, unplaced: string[]}}
 */
export function project(frames, surfaceId, viewport) {
  const onSurface = (frames ?? []).filter((frame) => frame.surface_id === surfaceId);
  const placedFrames = onSurface.filter((frame) => !isUnplaced(frame));
  const unplaced = onSurface.filter(isUnplaced).map((frame) => frame.id);

  if (placedFrames.length === 0) {
    return { placed: [], unplaced };
  }

  const minX = Math.min(...placedFrames.map((frame) => frame.x_mm));
  const minY = Math.min(...placedFrames.map((frame) => frame.y_mm));
  const maxX = Math.max(...placedFrames.map((frame) => frame.x_mm + frame.width_mm));
  const maxY = Math.max(...placedFrames.map((frame) => frame.y_mm + frame.height_mm));
  const spanX = maxX - minX;
  const spanY = maxY - minY;

  // Uniform scale-to-fit. A zero span (degenerate placement) falls back to 1 so
  // the scale stays finite rather than dividing by zero.
  const scaleX = spanX > 0 ? viewport.width / spanX : 1;
  const scaleY = spanY > 0 ? viewport.height / spanY : 1;
  const scale = Math.min(scaleX, scaleY);

  const placed = placedFrames.map((frame) => ({
    id: frame.id,
    rect: {
      x: (frame.x_mm - minX) * scale,
      y: (frame.y_mm - minY) * scale,
      w: frame.width_mm * scale,
      h: frame.height_mm * scale,
    },
  }));

  return { placed, unplaced };
}
