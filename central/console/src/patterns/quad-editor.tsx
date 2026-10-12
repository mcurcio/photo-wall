import * as React from "react";

import { cn } from "../ui/cn";

/** A point in normalized output space: x and y from 0 (left, top) to 1 (right, bottom). */
export type Point = readonly [number, number];

/**
 * What a drag or the arrow keys move: one corner (0 to 3, clockwise from the top left) or one
 * edge (edge `i` runs from corner `i` to the next: top, right, bottom, left).
 */
export interface QuadPart {
  kind: "corner" | "edge";
  index: number;
}

/** The corners a part moves; null (the whole quad) moves all four. */
export function partCorners(part: QuadPart | null): number[] {
  if (part === null) return [0, 1, 2, 3];
  return part.kind === "corner" ? [part.index] : [part.index, (part.index + 1) % 4];
}

/**
 * The corners with `part`'s moved by (dx, dy): the one move a drag and a nudge both make. It
 * keeps no rule; the caller accepts or refuses the result.
 */
export function moveCorners(corners: readonly Point[], part: QuadPart | null, dx: number, dy: number): Point[] {
  const moved = partCorners(part);
  return corners.map(([x, y], index) => (moved.includes(index) ? [x + dx, y + dy] : [x, y]) as Point);
}

export interface QuadEditorProps {
  /** The editor's accessible name. */
  label: string;
  /** Four corners, clockwise from the top left. */
  corners: readonly Point[];
  /** The output's width over its height, so the drawing has the screen's shape. */
  aspect: number;
  /** The corner or edge the arrow keys move, or null for the whole quad. */
  selected: QuadPart | null;
  onSelect: (part: QuadPart | null) => void;
  /** A corner or an edge dragged: the corners it proposes; the caller may refuse them (the drawing then stays). */
  onCorners: (corners: Point[]) => void;
  /** An arrow key, as a step of -1, 0 or 1 on each axis. */
  onArrow: (dx: number, dy: number) => void;
  disabled?: boolean;
}

const WIDTH = 480;
const PAD = 16;
// The edge line's hit target, wide enough for a finger.
const EDGE_HIT = 24;
const ARROWS: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};
const CORNER_NAMES = ["Top-left corner", "Top-right corner", "Bottom-right corner", "Bottom-left corner"];
const EDGE_NAMES = ["Top edge", "Right edge", "Bottom edge", "Left edge"];

interface Drag {
  part: QuadPart;
  from: Point;
  corners: readonly Point[];
}

const same = (a: QuadPart | null, b: QuadPart) => a !== null && a.kind === b.kind && a.index === b.index;
const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value));

/**
 * QuadEditor: a screen drawn to its shape and the picture's four corners and four edges as
 * handles. Drag a corner to move it, or an edge (its midpoint handle or the line itself) to move
 * both its corners together by the same step; a drag never takes a corner off the screen. Each
 * handle takes focus, which selects it; with the editor or a handle focused, the arrow keys
 * report a step for the selected corner or edge (or the whole quad), which the caller turns into a
 * move. It holds no geometry rules: every move is the caller's to accept. The handles are drawn in
 * the accent because they are what you act on (an allowed use of the accent); the picture itself
 * is drawn in the text colour.
 */
export function QuadEditor({
  label, corners, aspect, selected, onSelect, onCorners, onArrow, disabled = false,
}: QuadEditorProps) {
  const height = Math.round(WIDTH / (aspect > 0 ? aspect : 16 / 9));
  const view = { width: WIDTH + PAD * 2, height: height + PAD * 2 };
  const svgRef = React.useRef<SVGSVGElement | null>(null);
  const drag = React.useRef<Drag | null>(null);
  const at = (point: Point) => [point[0] * WIDTH, point[1] * height] as const;

  const normalized = (event: React.PointerEvent): Point => {
    const box = svgRef.current!.getBoundingClientRect();
    const scale = box.width / view.width;
    return [
      (event.clientX - box.left) / scale / WIDTH - PAD / WIDTH,
      (event.clientY - box.top) / scale / height - PAD / height,
    ];
  };
  const start = (event: React.PointerEvent, part: QuadPart) => {
    if (disabled) return;
    event.preventDefault();
    svgRef.current?.setPointerCapture?.(event.pointerId);
    drag.current = { part, from: normalized(event), corners };
    onSelect(part);
  };
  const move = (event: React.PointerEvent) => {
    const current = drag.current;
    if (current === null) return;
    const [x, y] = normalized(event);
    // The step since the drag began, held so every moved corner stays on the screen.
    const moved = partCorners(current.part).map((index) => current.corners[index]);
    const xs = moved.map((point) => point[0]);
    const ys = moved.map((point) => point[1]);
    const dx = clamp(x - current.from[0], -Math.min(...xs), 1 - Math.max(...xs));
    const dy = clamp(y - current.from[1], -Math.min(...ys), 1 - Math.max(...ys));
    onCorners(moveCorners(current.corners, current.part, dx, dy));
  };
  const end = () => {
    drag.current = null;
  };
  const key = (event: React.KeyboardEvent) => {
    const step = ARROWS[event.key];
    if (step === undefined || disabled) return;
    event.preventDefault();
    onArrow(step[0], step[1]);
  };
  // A handle: named, focusable, and selected when it takes focus or a pointer presses it.
  const handle = (part: QuadPart, name: string) => ({
    role: "button",
    "aria-label": name,
    "aria-pressed": same(selected, part),
    tabIndex: disabled ? -1 : 0,
    className: "cursor-grab outline-none",
    onFocus: () => {
      if (!disabled && !same(selected, part)) onSelect(part);
    },
    onPointerDown: (event: React.PointerEvent) => start(event, part),
  });

  const points = corners.map((corner) => at(corner).join(",")).join(" ");
  return (
    <svg
      ref={svgRef}
      role="group"
      aria-label={label}
      tabIndex={disabled ? -1 : 0}
      viewBox={`0 0 ${view.width} ${view.height}`}
      className={cn(
        "block h-auto w-full max-w-xl touch-none rounded-card bg-surface-sunken",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
      )}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={end}
      onKeyDown={key}
    >
      <g transform={`translate(${PAD}, ${PAD})`}>
        <rect x={0} y={0} width={WIDTH} height={height} className="fill-surface stroke-line-input" strokeWidth={1} />
        <polygon points={points} className={cn("fill-text/10 stroke-text", selected === null && "stroke-3")} strokeWidth={2} />
        {corners.map((corner, index) => {
          const part: QuadPart = { kind: "edge", index };
          const [x1, y1] = at(corner);
          const [x2, y2] = at(corners[(index + 1) % 4]);
          const angle = (Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI;
          const on = same(selected, part);
          return (
            <g key={`edge-${index}`} {...handle(part, EDGE_NAMES[index])}>
              {on && <line x1={x1} y1={y1} x2={x2} y2={y2} className="stroke-text" strokeWidth={4} pointerEvents="none" />}
              <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="transparent" strokeWidth={EDGE_HIT}
                pointerEvents="stroke" />
              <rect x={-12} y={-4} width={24} height={8} rx={4}
                transform={`translate(${(x1 + x2) / 2}, ${(y1 + y2) / 2}) rotate(${angle})`}
                className={cn("stroke-surface", on ? "fill-text" : "fill-accent")} strokeWidth={2} />
            </g>
          );
        })}
        {corners.map((corner, index) => {
          const part: QuadPart = { kind: "corner", index };
          const [x, y] = at(corner);
          const on = same(selected, part);
          return (
            <g key={`corner-${index}`} {...handle(part, CORNER_NAMES[index])}>
              <circle cx={x} cy={y} r={on ? 11 : 9}
                className={cn("stroke-surface", on ? "fill-text" : "fill-accent")} strokeWidth={2} />
            </g>
          );
        })}
      </g>
    </svg>
  );
}
