import type { BoxHandle, PixelRectangle } from "../patterns/box-editor";
import type { Turn } from "./turn-words";

/*
 * A Frame's Position as a box, in the operator's terms (frame box run, .claude/runs/frame-box/
 * page.md, bead fb-4): the screen as it hangs, the box's edges in whole pixels of it, its shape and
 * its physical size. The one home of these numbers and their words, so the Position editor
 * (frame-position.tsx), its History (position-history.tsx) and the Hardware tab's Frame facts say a
 * box alike. The pixel rule is src/placement.js `placeBox` (Central's contracts/frame_fit.py
 * `place_box`); this module adds none. Pure: no React, no request. Replaces position-on-screen.ts
 * in bead fb-7.
 */

/** A Position's four values: fractions of the screen as it hangs (contracts/models.py `PositionBox`). */
export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** The whole screen: a new Frame's Position and "Fill the screen". */
export const WHOLE_SCREEN: Box = { left: 0, top: 0, width: 1, height: 1 };

/** The screen as it hangs, [width, height] in pixels: the mode as built (`size`, Central's
 * `FrameInventory.screen_px`) turned by `turn`; null when the size is not known. */
export function screenOf(size: readonly [number, number] | null, turn: Turn): [number, number] | null {
  void size;
  void turn;
  throw new Error("frame box bead fb-4");
}

/** The box in whole pixels of `screen` (placement.js `placeBox`). */
export function boxPixels(box: Box, screen: readonly [number, number]): PixelRectangle {
  void box;
  void screen;
  throw new Error("frame box bead fb-4");
}

/** The box whose pixels on `screen` are `pixels`, after moving and clipping them onto the screen
 * and to at least 1 x 1: left = x / W, top = y / H, width = w / W, height = h / H, so
 * `boxPixels(boxFromPixels(p, s), s)` is p for every p on s. */
export function boxFromPixels(pixels: PixelRectangle, screen: readonly [number, number]): Box {
  void pixels;
  void screen;
  throw new Error("frame box bead fb-4");
}

/** `box` moved by (dx, dy) whole pixels of `screen`, its size kept, stopped at the screen's edges. */
export function movedBy(box: Box, dx: number, dy: number, screen: readonly [number, number]): Box {
  void box;
  void dx;
  void dy;
  void screen;
  throw new Error("frame box bead fb-4");
}

/** `box` with the edge or corner `handle` moved by (dx, dy) whole pixels of `screen` (an edge only
 * on its own axis): the opposite edges stay, width and height change independently, never below
 * 1 pixel nor past the screen's edges. */
export function resizedBy(box: Box, handle: BoxHandle, dx: number, dy: number,
  screen: readonly [number, number]): Box {
  void box;
  void handle;
  void dx;
  void dy;
  void screen;
  throw new Error("frame box bead fb-4");
}

/** The Frame's pixels and shape in words: "1,920 × 1,080 pixels, 16:9" (a ratio of small whole
 * numbers when one is within 0.5 %, else "1.78:1"). */
export function shapeWords(pixels: PixelRectangle): string {
  void pixels;
  throw new Error("frame box bead fb-4");
}

/** The Frame's physical size in words: "95 × 54 cm, 43 in across" (one decimal below 10 cm);
 * null when it is not known (placement.js `boxSizeMm` answered null). */
export function sizeWords(sizeMm: readonly [number, number] | null): string | null {
  void sizeMm;
  throw new Error("frame box bead fb-4");
}
