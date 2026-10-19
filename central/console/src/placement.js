/**
 * A Frame's rectangle on its screen, in pixels (render redesign S5, run ledger
 * .claude/runs/render-planes/s5-page.md), pure. The console's mirror of Central's rules, so a
 * Position the editor accepts is one Central's Save and the Player's planes accept, pixel for pixel:
 *
 *  - `hanging` is contracts/display_turn.py `hanging`: a screen hung turned 90 or 270 swaps its
 *    mode's width and height.
 *  - `place` is contracts/frame_fit.py `place` (and `FrameRectangle`'s check): each value rounds
 *    half up, floor(v + 0.5), never JavaScript's Math.round of a negative half; a rectangle that
 *    does not lie wholly on its screen has no placement (null, where Python raises
 *    "placement_outside"), never a clipped one.
 *  - `fillScreen` is the largest rectangle of the Frame's shape that `place` puts on the screen,
 *    centred (the editor's "Fill the screen"; migration 102 shrinks a Position by the same rule).
 *
 * tests/test_console_placement.py runs this module under Node against the Python functions on
 * shared vectors. It keeps no other rule: moves and steps are the editor's (domain/frame-position.tsx).
 *
 * @typedef {{centre_x: number, centre_y: number, width: number}} Rectangle   fractions of the screen as it hangs
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

/** Error("placement_screen") or Error("placement_aspect") as `place` refuses them. */
function checkScreen(screen, aspect) {
  if (!(side(screen[0]) && side(screen[1]))) throw new Error("placement_screen");
  if (!(finite(aspect) && aspect > 0)) throw new Error("placement_aspect");
}

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
 * `rectangle` in whole pixels on a screen of `screen` = [W, H] pixels as it hangs, for a Frame
 * profile of width-to-height `aspect`: width = max(1, halfUp(rectangle.width * W)), height =
 * max(1, halfUp(width / aspect)), x = halfUp(centre_x * W - width / 2), y = halfUp(centre_y * H -
 * height / 2); null unless 0 <= x, x + width <= W, 0 <= y and y + height <= H. Throws
 * Error("frame_rectangle") unless every field is finite, 0 <= centre_x <= 1, 0 <= centre_y <= 1
 * and 0 < width <= 1; Error("placement_screen") unless both sides are integers of at least 1;
 * Error("placement_aspect") unless `aspect` is finite and above 0.
 *
 * @param {Rectangle} rectangle
 * @param {[number, number]} screen as it hangs
 * @param {number} aspect the Frame profile's width_px / height_px
 * @returns {Placement|null}
 */
export function place(rectangle, screen, aspect) {
  const { centre_x: centreX, centre_y: centreY, width: fraction } = rectangle;
  if (!([centreX, centreY, fraction].every(finite)
        && centreX >= 0 && centreX <= 1 && centreY >= 0 && centreY <= 1 && fraction > 0 && fraction <= 1)) {
    throw new Error("frame_rectangle");
  }
  const placement = pixels(rectangle, screen, aspect);
  const { x, y, width, height } = placement;
  if (!(x >= 0 && x + width <= screen[0] && y >= 0 && y + height <= screen[1])) return null;
  return placement;
}

/**
 * `place`'s pixels with neither of its checks on the rectangle: where `rectangle` would lie, on the
 * screen or not (the editor draws a refused one there and names the edge it crosses). Its fields
 * need only be finite numbers; throws as `place` for the screen and the aspect.
 *
 * @param {Rectangle} rectangle
 * @param {[number, number]} screen as it hangs
 * @param {number} aspect
 * @returns {Placement}
 */
export function pixels(rectangle, screen, aspect) {
  checkScreen(screen, aspect);
  const { centre_x: centreX, centre_y: centreY, width: fraction } = rectangle;
  const [screenWidth, screenHeight] = screen;
  const width = Math.max(1, halfUp(fraction * screenWidth));
  const height = Math.max(1, halfUp(width / aspect));
  const x = halfUp(centreX * screenWidth - width / 2);
  const y = halfUp(centreY * screenHeight - height / 2);
  return { x, y, width, height };
}

/**
 * The largest rectangle of width-to-height `aspect` that `place` puts on `screen`, centred:
 * width = min(W, floor(H * aspect + 1e-6)) / W, centre (0.5, 0.5). Throws as `place`.
 *
 * @param {[number, number]} screen as it hangs
 * @param {number} aspect the Frame profile's width_px / height_px
 * @returns {Rectangle}
 */
export function fillScreen(screen, aspect) {
  checkScreen(screen, aspect);
  const [screenWidth, screenHeight] = screen;
  return { centre_x: 0.5, centre_y: 0.5, width: Math.min(screenWidth, Math.floor(screenHeight * aspect + 1e-6)) / screenWidth };
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
