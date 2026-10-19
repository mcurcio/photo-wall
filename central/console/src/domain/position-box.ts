import { hanging, placeBox } from "../placement.js";
import type { BoxHandle, PixelRectangle } from "../patterns/box-editor";
import { countText, pixelSize } from "../timeWords.js";
import type { Turn } from "./turn-words";

/*
 * A Frame's Position as a box, in the operator's terms (frame box run, .claude/runs/frame-box/
 * page.md, bead fb-4): the screen as it hangs, the box's edges in whole pixels of it, its shape and
 * its physical size. The one home of these numbers and their words, so the Position editor
 * (frame-position.tsx), its History (position-history.tsx) and the Hardware tab's Frame facts say a
 * box alike. The pixel rule is src/placement.js `placeBox` (Central's contracts/frame_fit.py
 * `place_box`); this module adds none. Pure: no React, no request. Replaces position-on-screen.ts
 * in bead fb-7.
 *
 * Moves and resizes work on the box's pixels: the editor's steps are whole pixels of the screen,
 * and a box built back from whole pixels (`boxFromPixels`) places on exactly those pixels, so what
 * the editor draws is what Central and the Player place.
 */

/** A Position's four values: fractions of the screen as it hangs (contracts/models.py `PositionBox`). */
export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** The whole screen: a new Frame's Position and "Fill the screen". */
export const WHOLE_SCREEN: Box = Object.freeze({ left: 0, top: 0, width: 1, height: 1 });

/** The screen as it hangs, [width, height] in pixels: the mode as built (`size`, Central's
 * `FrameInventory.screen_px`) turned by `turn`; null when the size is not known. */
export function screenOf(size: readonly [number, number] | null, turn: Turn): [number, number] | null {
  if (size === null) return null;
  return hanging([size[0], size[1]], turn) as [number, number];
}

/** The box in whole pixels of `screen` (placement.js `placeBox`). */
export function boxPixels(box: Box, screen: readonly [number, number]): PixelRectangle {
  return placeBox(box, [screen[0], screen[1]]) as PixelRectangle;
}

/** `value` held between `low` and `high`. */
const clamp = (value: number, low: number, high: number) => Math.min(Math.max(value, low), high);

/** The box whose pixels on `screen` are `pixels`, after moving and clipping them onto the screen
 * and to at least 1 x 1: left = x / W, top = y / H, width = w / W, height = h / H, so
 * `boxPixels(boxFromPixels(p, s), s)` is p for every p on s. */
export function boxFromPixels(pixels: PixelRectangle, screen: readonly [number, number]): Box {
  const [screenWidth, screenHeight] = screen;
  const width = clamp(Math.round(pixels.width), 1, screenWidth);
  const height = clamp(Math.round(pixels.height), 1, screenHeight);
  const x = clamp(Math.round(pixels.x), 0, screenWidth - width);
  const y = clamp(Math.round(pixels.y), 0, screenHeight - height);
  return { left: x / screenWidth, top: y / screenHeight, width: width / screenWidth, height: height / screenHeight };
}

/** `box` moved by (dx, dy) whole pixels of `screen`, its size kept, stopped at the screen's edges. */
export function movedBy(box: Box, dx: number, dy: number, screen: readonly [number, number]): Box {
  const placed = boxPixels(box, screen);
  return boxFromPixels({ ...placed, x: placed.x + dx, y: placed.y + dy }, screen);
}

/** `box` with the edge or corner `handle` moved by (dx, dy) whole pixels of `screen` (an edge only
 * on its own axis): the opposite edges stay, width and height change independently, never below
 * 1 pixel nor past the screen's edges. */
export function resizedBy(box: Box, handle: BoxHandle, dx: number, dy: number,
  screen: readonly [number, number]): Box {
  const [screenWidth, screenHeight] = screen;
  const placed = boxPixels(box, screen);
  let left = placed.x;
  let right = placed.x + placed.width;
  let top = placed.y;
  let bottom = placed.y + placed.height;
  const edges = new Set(handle.split("-"));
  if (edges.has("left")) left = clamp(left + dx, 0, right - 1);
  if (edges.has("right")) right = clamp(right + dx, left + 1, screenWidth);
  if (edges.has("top")) top = clamp(top + dy, 0, bottom - 1);
  if (edges.has("bottom")) bottom = clamp(bottom + dy, top + 1, screenHeight);
  return boxFromPixels({ x: left, y: top, width: right - left, height: bottom - top }, screen);
}

// A shape is named as a ratio of whole numbers up to this (16:9, 9:16, 4:3, 1:1) when one is
// within RATIO_TOLERANCE of it, else as a decimal to one ("2.37:1").
const RATIO_TERMS = 16;
const RATIO_TOLERANCE = 0.005;

/** A width-to-height `ratio` as the simplest "a:b" within RATIO_TOLERANCE, else "r.rr:1". */
function ratioWords(ratio: number): string {
  for (let below = 1; below <= RATIO_TERMS; below += 1) {
    const above = Math.round(ratio * below);
    if (above >= 1 && above <= RATIO_TERMS && Math.abs(above / below - ratio) <= ratio * RATIO_TOLERANCE) {
      return `${above}:${below}`;
    }
  }
  return `${ratio.toFixed(2)}:1`;
}

/** The Frame's pixels and shape in words: "1,920 × 1,080 pixels, 16:9" (a ratio of small whole
 * numbers when one is within 0.5 %, else "1.78:1"). */
export function shapeWords(pixels: PixelRectangle): string {
  return `${pixelSize(pixels.width, pixels.height)} pixels, ${ratioWords(pixels.width / pixels.height)}`;
}

/** A length in words, whole, or to one decimal below 10. */
const lengthWords = (value: number) => countText(value < 10 ? Math.round(value * 10) / 10 : Math.round(value)) as string;

const MM_PER_INCH = 25.4;

/** The Frame's physical size in words: "95 × 54 cm, 43 in across" (one decimal below 10 cm);
 * null when it is not known (placement.js `boxSizeMm` answered null). */
export function sizeWords(sizeMm: readonly [number, number] | null): string | null {
  if (sizeMm === null) return null;
  const [width, height] = sizeMm;
  const across = Math.hypot(width, height) / MM_PER_INCH;
  return `${lengthWords(width / 10)} × ${lengthWords(height / 10)} cm, ${lengthWords(across)} in across`;
}
