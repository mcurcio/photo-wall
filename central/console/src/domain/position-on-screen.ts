import { fillScreen, hanging, pixels } from "../placement.js";
import type { PixelRectangle, ScreenEdge } from "../patterns/rectangle-editor";
import { countText } from "../timeWords.js";
import type { Turn } from "./turn-words";

/*
 * A Frame's Position on its screen in the operator's terms (render redesign S5): the screen as it
 * hangs, the rectangle's centre and width in whole pixels of it, the edges a refused rectangle
 * runs past and the nearest one that fits. The one home of these numbers and their words, so the
 * Position editor (frame-position.tsx) and its History (position-history.tsx) say a Position
 * alike. The pixel rule itself is src/placement.js (Central's and the Player's); this module adds
 * none. Pure: no React, no request.
 */

/** A Position's three values: its centre as fractions of the screen as it hangs, its width as a fraction of its width. */
export interface Rectangle {
  centre_x: number;
  centre_y: number;
  width: number;
}

/** The output size a Pi that reports none is drawn at: steps are still pixels of a common output. */
export const FALLBACK_SIZE: [number, number] = [1920, 1080];

/** The screen as it hangs, [width, height] in pixels: the output's mode (`size`; null: FALLBACK_SIZE)
 * turned by `turn`. */
export function screenOf(size: { width: number; height: number } | null, turn: Turn): [number, number] {
  return hanging(size === null ? FALLBACK_SIZE : [size.width, size.height], turn) as [number, number];
}

/** Where `rectangle` would lie on `screen` in whole pixels, on it or not (placement.js `pixels`);
 * null when the screen or the aspect is no size. */
export function wouldLie(rectangle: Rectangle, screen: [number, number], aspect: number): PixelRectangle | null {
  try {
    return pixels(rectangle, screen, aspect) as PixelRectangle;
  } catch {
    return null;
  }
}

/** The edges of `screen` that `placed` runs past, in the order left, right, top, bottom. */
export function edgesCrossed(placed: PixelRectangle, screen: [number, number]): ScreenEdge[] {
  const [width, height] = screen;
  return [
    ...(placed.x < 0 ? ["left" as const] : []),
    ...(placed.x + placed.width > width ? ["right" as const] : []),
    ...(placed.y < 0 ? ["top" as const] : []),
    ...(placed.y + placed.height > height ? ["bottom" as const] : []),
  ];
}

/**
 * The nearest Position to `rectangle` that lies on `screen`: no wider than the screen takes (Fill the
 * screen's width), then moved in just far enough, in whole pixels. "Move it back inside".
 */
export function movedInside(rectangle: Rectangle, screen: [number, number], aspect: number): Rectangle {
  const [screenWidth, screenHeight] = screen;
  const width = Math.min(rectangle.width, fillScreen(screen, aspect).width);
  const placed = pixels({ ...rectangle, width }, screen, aspect) as PixelRectangle;
  const x = Math.min(Math.max(placed.x, 0), screenWidth - placed.width);
  const y = Math.min(Math.max(placed.y, 0), screenHeight - placed.height);
  return {
    centre_x: (x + placed.width / 2) / screenWidth,
    centre_y: (y + placed.height / 2) / screenHeight,
    width,
  };
}

const EDGE_WORDS: Record<ScreenEdge, string> = { left: "left", right: "right", top: "top", bottom: "bottom" };

/** What a rectangle that does not fit would do, naming the edges it runs past ("run past the right
 * edge of the screen"); a clause for "The picture would …". No edge: it fits as drawn here, yet
 * Central measured the screen otherwise (a size the Pi reported since). */
function offScreenClause(edges: readonly ScreenEdge[]): string {
  const crosses = new Set(edges);
  if (crosses.has("left") && crosses.has("right")) return "be wider than the screen";
  if (crosses.has("top") && crosses.has("bottom")) return "be taller than the screen";
  if (edges.length === 0) return "run off the screen as the Pi measures it";
  const named = edges.map((edge) => EDGE_WORDS[edge]);
  return named.length === 1
    ? `run past the ${named[0]} edge of the screen`
    : `run past the ${named.join(" and ")} edges of the screen`;
}

/** Why a rectangle does not fit, in a sentence: "The picture would run past the right edge of the screen." */
export function offScreenWords(edges: readonly ScreenEdge[]): string {
  return `The picture would ${offScreenClause(edges)}.`;
}

/** A refused change's card: what the Pi kept, why, what to do. */
export function keptWords(edges: readonly ScreenEdge[]): string {
  return `The Pi kept the last position: this one would ${offScreenClause(edges)}. Move it in or make it smaller.`;
}

/** The same, short, for a typed value's Field: "Would run past the right edge of the screen". */
export function offScreenShort(edges: readonly ScreenEdge[]): string {
  return `Would ${offScreenClause(edges)}`;
}

/** A Position's centre and width in pixels of `screen`, as the editor and History show them: the
 * centre to a tenth of a pixel, the width whole. */
export function pixelValues(rectangle: Rectangle, screen: [number, number]): { across: number; down: number; width: number } {
  const tenth = (value: number) => Math.round(value * 10) / 10;
  return {
    across: tenth(rectangle.centre_x * screen[0]),
    down: tenth(rectangle.centre_y * screen[1]),
    width: Math.round(rectangle.width * screen[0]),
  };
}

// The console's one grouping of a number (timeWords.js), as every pixel size reads.
const grouped = (value: number) => countText(value) as string;

/** "630 across, 540 down": a centre in pixels (`pixelValues`). */
export function centreWords(values: { across: number; down: number }): string {
  return `${grouped(values.across)} across, ${grouped(values.down)} down`;
}

/** "960 px": a width in pixels. */
export function widthWords(width: number): string {
  return `${grouped(width)} px`;
}

/** "half the screen's width", "the whole width", "38 % of the screen's width": a width in pixels as a
 * share of the screen's. */
export function shareWords(width: number, screenWidth: number): string {
  if (width >= screenWidth) return "the screen's whole width";
  if (width * 2 === screenWidth) return "half the screen's width";
  return `${Math.round((width / screenWidth) * 100)} % of the screen's width`;
}
