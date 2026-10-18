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

const NOT_BUILT = "render-planes S5 bead B2";

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
  void size;
  void turn;
  throw new Error(NOT_BUILT);
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
  void rectangle;
  void screen;
  void aspect;
  throw new Error(NOT_BUILT);
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
  void screen;
  void aspect;
  throw new Error(NOT_BUILT);
}
