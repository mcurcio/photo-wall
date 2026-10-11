import * as React from "react";

import { cn } from "../ui/cn";

/** A point in normalized output space: x and y from 0 (left, top) to 1 (right, bottom). */
export type Point = readonly [number, number];

export interface QuadEditorProps {
  /** The editor's accessible name. */
  label: string;
  /** Four corners, clockwise from the top left. */
  corners: readonly Point[];
  /** The output's width over its height, so the drawing has the screen's shape. */
  aspect: number;
  /** The corner the arrow keys move, or null for the whole quad. */
  selected: number | null;
  onSelect: (corner: number | null) => void;
  /** A corner dragged to a point; the caller may refuse it (the drawing then stays). */
  onCorner: (corner: number, point: Point) => void;
  /** An arrow key, as a step of -1, 0 or 1 on each axis. */
  onArrow: (dx: number, dy: number) => void;
  disabled?: boolean;
}

const WIDTH = 480;
const PAD = 16;
const ARROWS: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};

/**
 * QuadEditor: a screen drawn to its shape and the picture's four corners as handles.
 * Drag a handle to move it; with the editor focused, the arrow keys
 * report a step for the selected corner (or the whole quad), which the caller turns into a move.
 * It holds no geometry rules: every move is the caller's to accept. The corner handles are drawn
 * in the accent because they are what you act on (an allowed use of the accent); the picture
 * itself is drawn in the text colour.
 */
export function QuadEditor({
  label, corners, aspect, selected, onSelect, onCorner, onArrow, disabled = false,
}: QuadEditorProps) {
  const height = Math.round(WIDTH / (aspect > 0 ? aspect : 16 / 9));
  const view = { width: WIDTH + PAD * 2, height: height + PAD * 2 };
  const svgRef = React.useRef<SVGSVGElement | null>(null);
  const drag = React.useRef<number | null>(null);
  const at = (point: Point) => [point[0] * WIDTH, point[1] * height] as const;

  const normalized = (event: React.PointerEvent): Point => {
    const box = svgRef.current!.getBoundingClientRect();
    const scale = box.width / view.width;
    const clamp = (value: number) => Math.min(1, Math.max(0, value));
    return [
      clamp((event.clientX - box.left) / scale / WIDTH - PAD / WIDTH),
      clamp((event.clientY - box.top) / scale / height - PAD / height),
    ];
  };
  const start = (event: React.PointerEvent, corner: number) => {
    if (disabled) return;
    event.preventDefault();
    svgRef.current?.setPointerCapture?.(event.pointerId);
    drag.current = corner;
    onSelect(corner);
  };
  const move = (event: React.PointerEvent) => {
    const corner = drag.current;
    if (corner === null) return;
    onCorner(corner, normalized(event));
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

  const points = corners.map((corner) => at(corner).join(",")).join(" ");
  return (
    <svg
      ref={svgRef}
      role="img"
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
          const [x, y] = at(corner);
          return (
            <circle key={index} cx={x} cy={y} r={selected === index ? 11 : 9}
              className={cn("cursor-grab stroke-surface", selected === index ? "fill-text" : "fill-accent")}
              strokeWidth={2}
              onPointerDown={(event) => start(event, index)} />
          );
        })}
      </g>
    </svg>
  );
}
