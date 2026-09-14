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

/**
 * Physical wall span (mm) that the plan viewport's WIDTH represents.
 *
 * `project()` (above) draws the plan scale-to-fit against the bounding box of the
 * Surface's placed frames, so there is NO single global mm↔px scale to invert —
 * the forward scale is data-dependent and changes as frames are added. For the
 * INVERSE placement of a fresh pointer drag we therefore adopt ONE documented
 * convention: the viewport width maps to a 4 m wall. This is a deliberate,
 * flagged choice (see the report) — a drag-created/moved frame is NOT guaranteed
 * to render at the exact same pixels after the write, because `project()` re-fits
 * on the next snapshot; the design explicitly accepts this ("the plan corrects on
 * the next snapshot", design §9a/J3).
 */
const WALL_SPAN_MM = 4000;

/**
 * Invert a pointer-drag rectangle (px, in the plan's viewBox space) back to a mm
 * placement — the px→mm inverse of `project()`'s forward `mm→px` mapping (Bead 10,
 * design J3/§9a).
 *
 * A single uniform `mmPerPx` (derived from the viewport WIDTH so a square drag
 * maps to a square placement, matching `project()`'s single `min()` scale) is
 * applied to every coordinate. Width/height are clamped to a minimum 1 mm so a
 * degenerate (zero-area) drag still yields a server-valid Frame (`FrameCreate`
 * requires `width_mm`/`height_mm` > 0).
 *
 * ORIGIN NUDGE (errata 2026-09-13, "isUnplaced origin heuristic"): `isUnplaced`
 * routes a frame at exactly `x_mm === 0 && y_mm === 0` to the Unplaced tray. A
 * drag that lands at the plan origin would therefore be mis-routed into the tray
 * instead of being drawn. This inverse nudges the exact-origin case to a minimal
 * non-origin offset (`x_mm = 1`) so a drag-created frame ALWAYS has distinct
 * geometry and renders on the plan. `isUnplaced`'s read-only heuristic is left
 * untouched (per the errata's instruction).
 *
 * @param {Rect} pxRect the drag rectangle `{x, y, w, h}` in viewBox px
 * @param {{width: number, height: number}} viewport the plan viewport in px
 * @param {string} surfaceId the Surface the frame is being placed on
 * @returns {{surface_id: string, x_mm: number, y_mm: number, width_mm: number, height_mm: number}}
 */
export function dragToPlacement(pxRect, viewport, surfaceId) {
  const mmPerPx = WALL_SPAN_MM / viewport.width;
  let x_mm = Math.round(pxRect.x * mmPerPx);
  let y_mm = Math.round(pxRect.y * mmPerPx);
  const width_mm = Math.max(1, Math.round(pxRect.w * mmPerPx));
  const height_mm = Math.max(1, Math.round(pxRect.h * mmPerPx));
  if (x_mm === 0 && y_mm === 0) {
    // Never emit the exact origin — see ORIGIN NUDGE above.
    x_mm = 1;
  }
  return { surface_id: surfaceId, x_mm, y_mm, width_mm, height_mm };
}

/**
 * True when physical (mm) and pixel dimensions agree on orientation — the SAME
 * guard the server enforces on `FrameCreate`/`place_frame`
 * (central/registry.py `_orientation_coherent`). A square frame
 * (`height_mm === width_mm`) is always coherent; otherwise portrait-vs-landscape
 * must match between the mm rectangle and the display profile's pixels.
 *
 * Used by the new-frame form to reject an incoherent profile client-side (design
 * §J2's "invalid → inline reason, no request" discipline) before the POST, rather
 * than relying only on the server's 422.
 *
 * @param {number} widthMm
 * @param {number} heightMm
 * @param {number} widthPx
 * @param {number} heightPx
 * @returns {boolean}
 */
export function orientationCoherent(widthMm, heightMm, widthPx, heightPx) {
  return heightMm === widthMm || heightMm > widthMm === heightPx > widthPx;
}

/**
 * Corner-handle pixel positions for the calibration editor (design §J2, Bead 7).
 *
 * The editor draws the four calibration corners (TL, TR, BR, BL) on a square
 * SVG whose normalized `[0, 1]` output space maps linearly to `size` px. This
 * pure helper turns the draft corners into on-screen handle centers, keeping the
 * mm/px-style projection decisions out of the React component.
 *
 * @param {number[][]} corners four normalized `[x, y]` points
 * @param {number} size the SVG's edge length in px
 * @returns {Array<{index: number, x: number, y: number}>}
 */
export function cornerHandles(corners, size) {
  return (corners ?? []).map((point, index) => ({
    index,
    x: point[0] * size,
    y: point[1] * size,
  }));
}

/**
 * Crop-rectangle handle pixel positions: the top-left `[left, top]` and
 * bottom-right `[right, bottom]` corners of the normalized crop rect, in px.
 *
 * @param {number[]} crop `[left, top, right, bottom]` in normalized space
 * @param {number} size the SVG's edge length in px
 * @returns {{topLeft: {x: number, y: number}, bottomRight: {x: number, y: number}}}
 */
export function cropHandles(crop, size) {
  const [left, top, right, bottom] = crop ?? [0, 0, 1, 1];
  return {
    topLeft: { x: left * size, y: top * size },
    bottomRight: { x: right * size, y: bottom * size },
  };
}

/**
 * Invert a pixel position within the editor back to a normalized `[x, y]`,
 * clamped to `[0, 1]` so a drag can never leave normalized output space (the
 * corner range half of the server guard is upheld by construction).
 *
 * @param {number} px pixel offset from the SVG's left edge
 * @param {number} py pixel offset from the SVG's top edge
 * @param {number} size the SVG's edge length in px
 * @returns {number[]} normalized `[x, y]`
 */
export function toNormalized(px, py, size) {
  const clamp = (value) => Math.min(1, Math.max(0, value));
  return [clamp(px / size), clamp(py / size)];
}
