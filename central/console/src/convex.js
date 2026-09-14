/**
 * Client-side calibration geometry guard (pure) — shared primitive #2 support.
 *
 * This module replicates the server's `Calibration.geometry` validator EXACTLY
 * (`contracts/models.py:50-63`) so a draft the console accepts is a draft the
 * server will also accept — no quad that passes here can 400 there. Replicated
 * verbatim, and load-bearing beyond the epsilon:
 *
 *  1. corners lie in normalized output space: every coordinate in `[0, 1]`.
 *  2. crop `(left, top, right, bottom)` is a nonempty normalized rectangle:
 *     `0 <= left < right <= 1` and `0 <= top < bottom <= 1`.
 *  3. the quad is a nondegenerate, consistently-clockwise convex aperture in
 *     SCREEN coordinates: for each corner index `i`, with `a = corners[i]`,
 *     `b = corners[(i+1)%4]`, `c = corners[(i+2)%4]`,
 *         cross[i] = (b[0]-a[0])*(c[1]-b[1]) - (b[1]-a[1])*(c[0]-b[0])
 *     and the quad is rejected when `min(cross) <= EPSILON`. The index rotation,
 *     the sign convention (screen-space / y-down clockwise winding), AND the
 *     `1e-6` epsilon all match the server; using `0` here would let a `1e-6`-thin
 *     quad the server rejects slip past the client (see the bead's epsilon probe).
 */

/**
 * The server's convex-guard epsilon. A quad is convex only when the minimum
 * cross product is STRICTLY greater than this — matching `min(cross) <= 1e-6`
 * rejection on the server (`contracts/models.py:62`).
 */
export const EPSILON = 1e-6;

/** True when every corner coordinate lies in normalized output space `[0, 1]`. */
export function cornersInRange(corners) {
  return (
    Array.isArray(corners) &&
    corners.length === 4 &&
    corners.every(
      (point) =>
        Array.isArray(point) &&
        point.length === 2 &&
        point.every((n) => typeof n === "number" && n >= 0 && n <= 1),
    )
  );
}

/**
 * True when `corners` (TL, TR, BR, BL) form a nondegenerate, consistently
 * clockwise convex aperture in screen space AND lie in `[0, 1]` — the exact
 * test the server applies. Rejects folded, self-intersecting and thin
 * (`min(cross) <= 1e-6`) quads.
 *
 * @param {number[][]} corners four `[x, y]` points, TL/TR/BR/BL
 * @returns {boolean}
 */
export function isConvex(corners) {
  if (!cornersInRange(corners)) {
    return false;
  }
  const cross = [];
  for (let i = 0; i < 4; i += 1) {
    const a = corners[i];
    const b = corners[(i + 1) % 4];
    const c = corners[(i + 2) % 4];
    cross.push((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]));
  }
  return Math.min(...cross) > EPSILON;
}

/**
 * True when `crop = [left, top, right, bottom]` describes a nonempty normalized
 * rectangle: `0 <= left < right <= 1` and `0 <= top < bottom <= 1`.
 *
 * @param {number[]} crop `[left, top, right, bottom]`
 * @returns {boolean}
 */
export function cropValid(crop) {
  if (!Array.isArray(crop) || crop.length !== 4) {
    return false;
  }
  const [left, top, right, bottom] = crop;
  if (![left, top, right, bottom].every((n) => typeof n === "number")) {
    return false;
  }
  return 0 <= left && left < right && right <= 1 && 0 <= top && top < bottom && bottom <= 1;
}
