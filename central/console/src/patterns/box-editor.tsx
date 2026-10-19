import * as React from "react";

import { cn } from "../ui/cn";
import { Disclosure } from "../ui/disclosure";

/*
 * BoxEditor (frame box run, .claude/runs/frame-box/page.md, bead fb-4; owner, 2026-10-19: "I need
 * to be able to set width and height independently, along with the position of that box on the
 * display"): a screen drawn to its shape as it hangs and the Frame's box on it. Drag the box to
 * move it, any of its four edges or four corners to resize it (the opposite edges stay put; width
 * and height change independently), or, with the drawing focused, the arrow keys to report a step.
 * It holds no geometry rule: every move and size is the caller's to accept (domain/frame-position.tsx,
 * by domain/position-box.ts).
 *
 * Drawn on a sunken well: the screen in the surface colour, the box in the
 * text colour and named inside when there is room, the display's own top edge thicker. Its eight
 * handles are drawn in the accent (what you act on): a square at each corner, a short bar at each
 * edge's middle. Each takes the pointer over 44 CSS pixels however small the drawing is shown
 * (design language §2: a phone's finger finds it), reaching into the box at most a quarter of its
 * width or height, so a press in the box's middle always moves it. A box too small for three
 * handles across (or down) keeps only its corners. The keyboard reaches the drawing (the arrow keys
 * report a move) and then each handle in turn, edges before corners (the arrow keys report that
 * handle's resize, an edge's on its own axis only).
 */

/** A box on the screen in whole pixels: its top-left corner and its size (src/placement.js `Placement`). */
export interface PixelRectangle {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** An edge of the screen as it hangs. */
export type ScreenEdge = "top" | "right" | "bottom" | "left";

/** What a drag takes hold of: one of the box's four edges or four corners. */
export type BoxHandle = "top" | "right" | "bottom" | "left" | "top-left" | "top-right" | "bottom-right" | "bottom-left";

export interface BoxEditorProps {
  /** The editor's accessible name. */
  label: string;
  /** The screen as it hangs, in pixels: the drawing has its shape, and drags are measured in its pixels. */
  screen: { width: number; height: number };
  /** The Frame's box on the screen, in its pixels (src/placement.js `placeBox`). */
  picture: PixelRectangle;
  /** The box's name, in the drawing and to a screen reader (default "Picture"). */
  pictureLabel?: string;
  /** The display's own top edge on the drawing, drawn thicker, so a turned screen shows its turn. */
  topEdge?: ScreenEdge;
  /** The box dragged: its proposed move since the last report, in whole screen pixels. */
  onMove: (dx: number, dy: number) => void;
  /** An edge or corner dragged: its proposed move since the last report, in whole screen pixels
   * (an edge reports only its own axis: 0 on the other). The caller accepts or refuses. */
  onResize: (handle: BoxHandle, dx: number, dy: number) => void;
  /** An arrow key on the focused drawing: a step of -1, 0 or 1 on each axis. */
  onArrow: (dx: number, dy: number) => void;
  disabled?: boolean;
  /** The editor's controls: beside a portrait screen on a wide page, below it otherwise. */
  aside?: React.ReactNode;
  /** How to use it, in words: above the drawing on a wide page; on a phone folded under it as
   * `helpSummary`, so the drawing and the editor's bar fit the first screen. Shown with `aside`. */
  help?: React.ReactNode;
  helpSummary?: string;
}

// The drawing's largest size in its own units: a landscape screen is drawn 480 wide, a portrait one
// 360 high, so a turned screen does not tower over the controls.
const MAX_WIDTH = 480;
const MAX_HEIGHT = 360;
// Room round the screen for the handles' pointer areas at its edges.
const PAD = 32;
// A corner handle as drawn (14 units across) and an edge handle's bar (24 by 8).
const CORNER = 7;
const BAR_LONG = 12;
const BAR_THICK = 4;
// Half the area that takes the pointer, in CSS pixels (44 across: design language §2's touch target).
const HIT_CSS = 22;
// The box's name is drawn inside it when it has this much room.
const NAME_WIDTH = 150;
const NAME_HEIGHT = 40;
// The widest the drawing is shown, in CSS pixels (a landscape screen, a portrait one): its own size,
// which it keeps its shape within, so on a phone, where it is also no taller than 45 % of the
// viewport, a portrait screen gets narrower, never taller, and the editor's sticky bar never covers it.
const SHOWN_LANDSCAPE = 576;
const SHOWN_PORTRAIT = 288;
const ARROWS: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};

interface HandleSpec {
  handle: BoxHandle;
  /** Which way it moves: across, down, or both. */
  across: boolean;
  down: boolean;
  /** Where it sits on the box: 0 the left or top edge, 0.5 the middle, 1 the right or bottom edge. */
  at: [number, number];
  /** Its name, before the box's: "Right edge: Picture". */
  words: string;
  cursor: string;
}

// Edges first, then corners, clockwise from the top: the keyboard's order after the drawing.
const HANDLES: readonly HandleSpec[] = [
  { handle: "top", across: false, down: true, at: [0.5, 0], words: "Top edge", cursor: "cursor-ns-resize" },
  { handle: "right", across: true, down: false, at: [1, 0.5], words: "Right edge", cursor: "cursor-ew-resize" },
  { handle: "bottom", across: false, down: true, at: [0.5, 1], words: "Bottom edge", cursor: "cursor-ns-resize" },
  { handle: "left", across: true, down: false, at: [0, 0.5], words: "Left edge", cursor: "cursor-ew-resize" },
  { handle: "top-left", across: true, down: true, at: [0, 0], words: "Top-left corner", cursor: "cursor-nwse-resize" },
  { handle: "top-right", across: true, down: true, at: [1, 0], words: "Top-right corner", cursor: "cursor-nesw-resize" },
  { handle: "bottom-right", across: true, down: true, at: [1, 1], words: "Bottom-right corner", cursor: "cursor-nwse-resize" },
  { handle: "bottom-left", across: true, down: true, at: [0, 1], words: "Bottom-left corner", cursor: "cursor-nesw-resize" },
];

/** The arrow keys a handle answers, for its accessible description. */
const keysOf = (spec: HandleSpec) => [
  ...(spec.across ? ["ArrowLeft", "ArrowRight"] : []),
  ...(spec.down ? ["ArrowUp", "ArrowDown"] : []),
].join(" ");

interface Drag {
  /** The box, or the handle taken hold of. */
  target: "box" | HandleSpec;
  /** The pointer where the last report was made, in screen pixels. */
  from: [number, number];
}

/** The line of `edge` on a drawn screen of `width` x `height`. */
function edgeLine(edge: ScreenEdge, width: number, height: number) {
  switch (edge) {
    case "top": return { x1: 0, y1: 0, x2: width, y2: 0 };
    case "right": return { x1: width, y1: 0, x2: width, y2: height };
    case "bottom": return { x1: 0, y1: height, x2: width, y2: height };
    default: return { x1: 0, y1: 0, x2: 0, y2: height };
  }
}

/** The span a handle's pointer area takes on one axis, from the edge it sits on at `at` (0 the near
 * edge, 1 the far, 0.5 the middle) of a box from `start` over `size`: `hit` * 2 in all, inward at
 * most a quarter of `size` (so the box's middle half is always the box's) and the rest outward. */
function hitSpan(at: number, start: number, size: number, hit: number): [number, number] {
  const inward = Math.min(hit, size / 4);
  const outward = hit * 2 - inward;
  if (at === 0) return [start - outward, hit * 2];
  if (at === 1) return [start + size - inward, hit * 2];
  return [start, size];
}

/** A handle's pointer area on a drawn box, `hit` units out from its edge (44 CSS pixels across in
 * all): a square at a corner; along an edge, a band over the edge between the corners' areas. */
function hitArea(spec: HandleSpec, box: PixelRectangle, hit: number) {
  const [atX, atY] = spec.at;
  const [x, width] = hitSpan(atX, box.x, box.width, hit);
  const [y, height] = hitSpan(atY, box.y, box.height, hit);
  if (spec.across && spec.down) return { x, y, width, height };
  if (spec.across) {
    const inside = Math.min(hit, box.height / 4);
    return { x, y: box.y + inside, width, height: Math.max(1, box.height - inside * 2) };
  }
  const inside = Math.min(hit, box.width / 4);
  return { x: box.x + inside, y, width: Math.max(1, box.width - inside * 2), height };
}

/** A handle as drawn: a square at a corner, a bar along the middle of an edge. */
function drawnHandle(spec: HandleSpec, box: PixelRectangle) {
  const [atX, atY] = spec.at;
  const x = box.x + box.width * atX;
  const y = box.y + box.height * atY;
  if (spec.across && spec.down) return { x: x - CORNER, y: y - CORNER, width: CORNER * 2, height: CORNER * 2 };
  if (spec.across) return { x: x - BAR_THICK, y: y - BAR_LONG, width: BAR_THICK * 2, height: BAR_LONG * 2 };
  return { x: x - BAR_LONG, y: y - BAR_THICK, width: BAR_LONG * 2, height: BAR_THICK * 2 };
}

/** The box editor (module comment). */
export function BoxEditor({
  label, screen, picture, pictureLabel = "Picture", topEdge, onMove, onResize, onArrow, disabled = false, aside,
  help, helpSummary = "How to use it",
}: BoxEditorProps): React.JSX.Element {
  const scale = Math.min(MAX_WIDTH / screen.width, MAX_HEIGHT / screen.height);
  const drawn = { width: screen.width * scale, height: screen.height * scale };
  const view = { width: drawn.width + PAD * 2, height: drawn.height + PAD * 2 };
  const svgRef = React.useRef<SVGSVGElement | null>(null);
  const drag = React.useRef<Drag | null>(null);
  const portrait = screen.width < screen.height;
  const shownWidth = portrait ? SHOWN_PORTRAIT : SHOWN_LANDSCAPE;
  // The drawing's units per CSS pixel as shown, the larger of its two axes (its own size until
  // measured): the handles' pointer areas are 44 CSS pixels across whatever size it is shown at.
  const [unitsPerCss, setUnitsPerCss] = React.useState(view.width / shownWidth);
  React.useEffect(() => {
    const svg = svgRef.current;
    if (svg === null || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => {
      const shown = svg.getBoundingClientRect();
      if (shown.width > 0 && shown.height > 0) {
        setUnitsPerCss(Math.max(view.width / shown.width, view.height / shown.height));
      }
    });
    observer.observe(svg);
    return () => observer.disconnect();
  }, [view.width, view.height]);
  const hit = HIT_CSS * unitsPerCss;

  // The pointer in screen pixels (the drawing may be shown smaller or larger than its view box).
  const pixels = (event: React.PointerEvent): [number, number] => {
    const bounds = svgRef.current!.getBoundingClientRect();
    const shown = bounds.width / view.width;
    return [((event.clientX - bounds.left) / shown - PAD) / scale, ((event.clientY - bounds.top) / shown - PAD) / scale];
  };
  const start = (event: React.PointerEvent, target: Drag["target"]) => {
    if (disabled) return;
    event.preventDefault();
    event.stopPropagation();
    svgRef.current?.setPointerCapture?.(event.pointerId);
    drag.current = { target, from: pixels(event) };
  };
  const move = (event: React.PointerEvent) => {
    const current = drag.current;
    if (current === null) return;
    const [x, y] = pixels(event);
    const { target } = current;
    // An edge reports its own axis only; the box and a corner report both.
    const dx = target === "box" || target.across ? Math.round(x - current.from[0]) : 0;
    const dy = target === "box" || target.down ? Math.round(y - current.from[1]) : 0;
    if (dx === 0 && dy === 0) return;
    current.from = [current.from[0] + dx, current.from[1] + dy];
    if (target === "box") onMove(dx, dy);
    else onResize(target.handle, dx, dy);
  };
  const end = () => {
    drag.current = null;
  };
  const drawingKey = (event: React.KeyboardEvent) => {
    const step = ARROWS[event.key];
    if (step === undefined || disabled || event.target !== event.currentTarget) return;
    event.preventDefault();
    onArrow(step[0], step[1]);
  };
  const handleKey = (event: React.KeyboardEvent, spec: HandleSpec) => {
    const step = ARROWS[event.key];
    if (step === undefined || disabled) return;
    const dx = spec.across ? step[0] : 0;
    const dy = spec.down ? step[1] : 0;
    event.preventDefault();
    event.stopPropagation();
    if (dx !== 0 || dy !== 0) onResize(spec.handle, dx, dy);
  };

  const box: PixelRectangle = {
    x: picture.x * scale, y: picture.y * scale, width: picture.width * scale, height: picture.height * scale,
  };
  // A box too small for three handles' pointer areas across (or down) keeps only its corners.
  const compact = box.width < hit * 6 || box.height < hit * 6;
  const handles = compact ? HANDLES.filter((spec) => spec.across && spec.down) : HANDLES;
  const drawing = (
    <svg
      ref={svgRef}
      role="group"
      aria-label={label}
      aria-disabled={disabled || undefined}
      tabIndex={disabled ? -1 : 0}
      viewBox={`0 0 ${view.width} ${view.height}`}
      width={shownWidth}
      height={Math.round((shownWidth * view.height) / view.width)}
      className={cn(
        // Both sides auto: the drawing keeps its shape within its own size, the column and, on a phone, 45vh.
        "block h-auto w-auto max-w-full shrink-0 self-start touch-none rounded-card bg-surface-sunken max-md:max-h-drawing-phone",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
      )}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={end}
      onKeyDown={drawingKey}
    >
      <g transform={`translate(${PAD}, ${PAD})`}>
        <rect x={0} y={0} width={drawn.width} height={drawn.height} className="fill-surface stroke-line-input" strokeWidth={1} />
        {topEdge !== undefined && (
          <line {...edgeLine(topEdge, drawn.width, drawn.height)} aria-hidden="true" className="stroke-label"
            strokeWidth={5} strokeLinecap="round" />
        )}
        <rect
          role="img"
          aria-label={pictureLabel}
          x={box.x}
          y={box.y}
          width={box.width}
          height={box.height}
          className={cn("fill-text/10 stroke-text", disabled ? "cursor-default" : "cursor-grab")}
          strokeWidth={2}
          onPointerDown={(event) => start(event, "box")}
        />
        {box.width > NAME_WIDTH && box.height > NAME_HEIGHT && (
          <text x={box.x + box.width / 2} y={box.y + box.height / 2} aria-hidden="true" textAnchor="middle"
            dominantBaseline="middle" fontSize={13} className="pointer-events-none fill-muted font-sans">
            {pictureLabel}
          </text>
        )}
        {!disabled && handles.map((spec) => (
          <rect key={`${spec.handle}-drawn`} aria-hidden="true" {...drawnHandle(spec, box)} rx={3}
            className="pointer-events-none fill-accent stroke-surface" strokeWidth={2} />
        ))}
        {!disabled && handles.map((spec) => (
          <rect
            key={spec.handle}
            role="button"
            tabIndex={0}
            aria-label={`${spec.words}: ${pictureLabel}`}
            aria-keyshortcuts={keysOf(spec)}
            {...hitArea(spec, box, hit)}
            className={cn("fill-transparent outline-none focus-visible:stroke-focus", spec.cursor)}
            strokeWidth={2}
            onPointerDown={(event) => start(event, spec)}
            onKeyDown={(event) => handleKey(event, spec)}
          />
        ))}
      </g>
    </svg>
  );
  if (aside === undefined) return drawing;
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {help !== undefined && <div className="max-md:hidden">{help}</div>}
      <div className={cn("flex min-w-0 flex-col gap-4", portrait && "md:flex-row md:items-start")}>
        {drawing}
        <div className="flex min-w-0 flex-1 flex-col gap-4">
          {help !== undefined && <div className="md:hidden"><Disclosure summary={helpSummary}>{help}</Disclosure></div>}
          {aside}
        </div>
      </div>
    </div>
  );
}
