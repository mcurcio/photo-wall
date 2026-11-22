import { useEffect, useRef, useState } from "react";
import type * as React from "react";

import { Skeleton } from "../ui/skeleton";

/** One Frame on its wall: where it hangs and how large, in millimetres; its tile. */
export interface WallMapFrame {
  key: string;
  /** Its top-left on the wall. */
  x: number;
  y: number;
  /** Its size as it hangs; null while its size is not known. */
  size: readonly [number, number] | null;
  tile: React.ReactNode;
}

export interface WallMapProps {
  /** The wall's name: the region's name and heading. */
  name: string;
  /** Where the wall is ("Living room"), beside its name; none: only the name. */
  room?: string;
  frames: readonly WallMapFrame[];
  /** The first read is on its way: skeleton tiles, in the Frames' places when they are known. */
  loading?: boolean;
  /** Under the map: what plays on this wall now and next. */
  below?: React.ReactNode;
}

// The narrowest and shortest a tile is drawn: its name, its chip, one line and "More" fit.
const MIN_WIDTH = 160;
const MIN_HEIGHT = 112;
// The widest the largest Frame is drawn, so one Frame alone is a tile and not a poster.
const MAX_WIDTH = 320;
// From this width of its own box the map is drawn to scale; narrower, the tiles are two across.
const SCALE_FROM = 640;
const GAP = 8;

interface Box { x: number; y: number; width: number; height: number }

/** Whether two boxes share any area. */
function overlap(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

/** Why a wall is not drawn to scale: nothing to draw, a Frame whose size is not known or two Frames
 * on the same piece of wall (`placement`), or a box too narrow for its smallest Frame's tile (`narrow`). */
export type NotScaled = "empty" | "placement" | "narrow";

/**
 * Each Frame's box in pixels for a map `width` wide, or why the wall cannot be drawn to scale
 * there. The scale fits the wall to the width and never draws the largest Frame wider than a tile
 * reads well; a wall whose smallest Frame would then be smaller than its tile's words is not drawn
 * to scale at this width (its tiles go side by side instead: nothing is cut and nothing scrolls).
 */
export function scaledBoxes(frames: readonly WallMapFrame[], width: number):
{ boxes: Box[]; width: number; height: number } | NotScaled {
  const sized = frames.map((frame) => (frame.size === null ? null
    : { x: frame.x, y: frame.y, width: frame.size[0], height: frame.size[1] }));
  if (sized.length === 0) return "empty";
  if (sized.some((box) => box === null || box.width <= 0 || box.height <= 0)) return "placement";
  const boxes = sized as Box[];
  if (boxes.some((a, i) => boxes.some((b, j) => i < j && overlap(a, b)))) return "placement";
  const left = Math.min(...boxes.map((box) => box.x));
  const top = Math.min(...boxes.map((box) => box.y));
  const wide = Math.max(...boxes.map((box) => box.x + box.width)) - left;
  const tall = Math.max(...boxes.map((box) => box.y + box.height)) - top;
  const scale = Math.min(width / wide, MAX_WIDTH / Math.max(...boxes.map((box) => box.width)));
  if (scale < MIN_WIDTH / Math.min(...boxes.map((box) => box.width))
    || scale < MIN_HEIGHT / Math.min(...boxes.map((box) => box.height))) return "narrow";
  return {
    boxes: boxes.map((box) => ({
      x: Math.round((box.x - left) * scale), y: Math.round((box.y - top) * scale),
      width: Math.round(box.width * scale) - GAP, height: Math.round(box.height * scale) - GAP,
    })),
    width: Math.round(wide * scale),
    height: Math.round(tall * scale),
  };
}

/** The map's own width in CSS pixels, measured; 0 until measured (the tiles are then two across). */
function useOwnWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const box = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = box.current;
    if (element === null || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [box, width];
}

/**
 * WallMap (design language §4, T1 Home): one wall, read-only, its Frames as tiles. In a box wide
 * enough the Frames are drawn to scale, each tile where its Frame hangs and at its shape (the tile
 * is HTML at its own text size inside the drawing: nothing is scaled); in a narrow box (a phone)
 * the same tiles are two across, in the order given, and nothing scrolls sideways. A wall that
 * cannot be drawn to scale (a Frame whose size is not known, two Frames on the same piece of wall)
 * is drawn two, three or four across instead and says so; so is one whose smallest Frame would be
 * smaller than its tile's words at this width. Arranging the wall is the layout page's.
 */
export function WallMap({ name, room, frames, loading = false, below }: WallMapProps) {
  const [box, width] = useOwnWidth();
  const drawn = width >= SCALE_FROM ? scaledBoxes(frames, width) : "narrow";
  const scaled = typeof drawn === "string" ? null : drawn;
  const tiles = loading && frames.length === 0
    ? ["a", "b", "c", "d"].map((key) => ({ key, tile: <Skeleton /> }))
    : frames.map((frame) => ({ key: frame.key, tile: loading ? <Skeleton /> : frame.tile }));
  return (
    <section aria-label={name} aria-busy={loading ? true : undefined} className="flex min-w-0 flex-col gap-2">
      <h2 className="m-0 text-base font-semibold text-text">
        {name}
        {room !== undefined && room !== "" && <span className="font-normal text-muted">{` · ${room}`}</span>}
      </h2>
      <div ref={box} className="min-w-0">
        {scaled !== null ? (
          <div className="min-w-0">
            <svg role="group" aria-label={`${name}, to scale`} width={scaled.width} height={scaled.height}
              viewBox={`0 0 ${scaled.width} ${scaled.height}`} className="block max-w-full">
              {tiles.map((entry, index) => (
                <foreignObject key={entry.key} x={scaled.boxes[index].x} y={scaled.boxes[index].y}
                  width={scaled.boxes[index].width} height={scaled.boxes[index].height}>
                  {/* The tile fills its Frame's box; what does not fit is cut, never spilt on a neighbour. */}
                  <div className="size-full *:size-full *:overflow-hidden">{entry.tile}</div>
                </foreignObject>
              ))}
            </svg>
          </div>
        ) : (
          // Two across in a box under SCALE_FROM (a phone, or a narrow column on a wide screen); the
          // screen's width adds columns only from there.
          <div className={`grid min-w-0 grid-cols-2 gap-2 ${width >= SCALE_FROM ? "md:grid-cols-3 xl:grid-cols-4" : ""}`}>
            {tiles.map((entry) => <div key={entry.key} className="flex min-w-0 flex-col *:grow">{entry.tile}</div>)}
          </div>
        )}
      </div>
      {!loading && drawn === "placement" && (
        <p className="m-0 text-xs text-muted">
          Not drawn to scale: a Frame’s size is not known yet, or two Frames are placed on the same piece of wall.
        </p>
      )}
      {below}
    </section>
  );
}

export interface NowNextStripProps {
  /** What plays now, already worded ("Evenings · until 23:00"); null: nothing is scheduled. */
  now: string | null;
  /** What follows ("Mornings · 07:00"); null: nothing follows. */
  next: string | null;
}

/** The now/next strip under a wall (T1 Home): what plays there now and what follows, two labelled lines. */
export function NowNextStrip({ now, next }: NowNextStripProps) {
  return (
    <dl className="m-0 flex min-w-0 flex-wrap gap-x-6 gap-y-1 text-sm text-text">
      <div className="flex min-w-0 gap-1.5">
        <dt className="text-muted">Now</dt>
        <dd className="m-0 wrap-anywhere">{now ?? "Nothing is scheduled"}</dd>
      </div>
      {next !== null && (
        <div className="flex min-w-0 gap-1.5">
          <dt className="text-muted">Next</dt>
          <dd className="m-0 wrap-anywhere">{next}</dd>
        </div>
      )}
    </dl>
  );
}
