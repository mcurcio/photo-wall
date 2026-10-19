/**
 * A Frame's box on its screen, in pixels (render redesign S5, run ledger
 * .claude/runs/render-planes/s5-page.md; frame box run, .claude/runs/frame-box/page.md fb-4, fb-7),
 * pure. The console's mirror of Central's rules, so a Position the editor draws is one Central's
 * Save and the Player's planes place, pixel for pixel:
 *
 *  - `hanging` is contracts/display_turn.py `hanging`: a screen hung turned 90 or 270 swaps its
 *    mode's width and height.
 *  - `placeBox` is contracts/frame_fit.py `place_box` (and `FrameBox`'s check): each edge rounds
 *    half up, floor(v + 0.5), never JavaScript's Math.round of a negative half.
 *  - `boxSizeMm` is contracts/models.py `frame_size_mm`: the box's share of the Display's own size.
 *
 * tests/test_console_placement.py runs this module under Node against the Python functions on
 * shared vectors. It keeps no other rule: moves, resizes and words are domain/position-box.ts's.
 *
 * @typedef {{x: number, y: number, width: number, height: number}} Placement  whole pixels, top-left corner and size
 * @typedef {0|90|180|270} Turn   degrees clockwise, as the screen hangs
 */

const TURNS = [0, 90, 180, 270];

/** `value` rounded half up, floor(value + 0.5): never Math.round's rounding of a negative half. */
const halfUp = (value) => Math.floor(value + 0.5);

/** Whether `value` is a finite number, never a boolean (Python's `_finite`). */
const finite = (value) => typeof value === "number" && Number.isFinite(value);

/** Whether `value` is a whole number of at least 1 (Python: an int, never a bool, >= 1). */
const side = (value) => Number.isInteger(value) && value >= 1;

/**
 * The screen's [width, height] in pixels as it hangs, from its mode's [width, height]: swapped for
 * 90 and 270. Throws Error("display_turn") for any other turn, Error("screen_size") unless both
 * sides are integers of at least 1.
 *
 * @param {[number, number]} size the mode as built
 * @param {Turn} turn
 * @returns {[number, number]}
 */
export function hanging(size, turn) {
  if (!TURNS.includes(turn)) throw new Error("display_turn");
  const [width, height] = size;
  if (!(side(width) && side(height))) throw new Error("screen_size");
  return turn === 90 || turn === 270 ? [height, width] : [width, height];
}

/**
 * A Frame's box in whole pixels (frame box run, .claude/runs/frame-box/page.md, bead fb-4):
 * contracts/frame_fit.py `place_box` and `FrameBox`'s check, mirrored pixel for pixel
 * (tests/test_console_placement.py). Edges round half up: x = min(W - 1, halfUp(left * W)),
 * right = max(x + 1, min(W, halfUp((left + width) * W))), width = right - x; y and height likewise
 * with H. Throws Error("frame_box") unless every field is finite, 0 <= left, 0 <= top, 0 < width,
 * 0 < height, left + width <= 1 + 1e-9 and top + height <= 1 + 1e-9; Error("placement_screen")
 * unless both sides are integers of at least 1.
 *
 * @typedef {{left: number, top: number, width: number, height: number}} Box   fractions of the screen as it hangs
 * @param {Box} box
 * @param {[number, number]} screen as it hangs
 * @returns {Placement}
 */
export function placeBox(box, screen) {
  checkBox(box);
  const [screenWidth, screenHeight] = screen;
  if (!(side(screenWidth) && side(screenHeight))) throw new Error("placement_screen");
  const [x, width] = edges(box.left, box.width, screenWidth);
  const [y, height] = edges(box.top, box.height, screenHeight);
  return { x, y, width, height };
}

/** How far a box's right or bottom edge may pass 1 in floating point (Python's BOX_SLACK). */
const BOX_SLACK = 1e-9;

/** Error("frame_box") as contracts/frame_fit.py `FrameBox` refuses a box. */
function checkBox(box) {
  const { left, top, width, height } = box;
  if (!([left, top, width, height].every(finite)
        && left >= 0 && top >= 0 && width > 0 && height > 0
        && left + width <= 1 + BOX_SLACK && top + height <= 1 + BOX_SLACK)) {
    throw new Error("frame_box");
  }
}

/** One axis of `placeBox` (Python's `_edges`): the first pixel and the pixel count of the span
 * `start` to `start + length` on `count` pixels, each edge rounding half up on its own. */
function edges(start, length, count) {
  const first = Math.min(count - 1, halfUp(start * count));
  const end = Math.max(first + 1, Math.min(count, halfUp((start + length) * count)));
  return [first, end - first];
}

/**
 * The box's physical [width, height] in millimetres: contracts/models.py `frame_size_mm`,
 * [box.width * W, box.height * H] of `displayMm` (the Display's picture area as built) turned by
 * `turn` (`hanging`); null when `displayMm` is null. Throws Error("frame_box") as `placeBox` for
 * any box (Central's box is a PositionBox before it has a size), then as `hanging` for a known size.
 *
 * @param {Box} box
 * @param {[number, number] | null} displayMm as built
 * @param {Turn} turn
 * @returns {[number, number] | null}
 */
export function boxSizeMm(box, displayMm, turn) {
  checkBox(box);
  if (displayMm === null) return null;
  const [width, height] = hanging(displayMm, turn);
  return [box.width * width, box.height * height];
}
