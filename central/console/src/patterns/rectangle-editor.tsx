import * as React from "react";

import { cn } from "../ui/cn";

/** A rectangle on the screen in whole pixels: its top-left corner and its size (src/placement.js `Placement`). */
export interface PixelRectangle {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface RectangleEditorProps {
  /** The editor's accessible name. */
  label: string;
  /** The screen as it hangs, in pixels: the drawing has its shape, and drags are measured in its pixels. */
  screen: { width: number; height: number };
  /** The picture on the screen, in its pixels; null draws the screen alone (the picture does not lie on it). */
  picture: PixelRectangle | null;
  /** The picture dragged: its proposed move since the last report, in screen pixels (whole numbers);
   * the caller accepts or refuses it (the drawing then stays). */
  onMove: (dx: number, dy: number) => void;
  /** The corner handle dragged: its proposed change of the picture's width, in screen pixels (a
   * whole number, either sign); the picture keeps its shape and its centre. The caller accepts or refuses. */
  onResize: (dWidth: number) => void;
  /** An arrow key on the focused drawing: a step of -1, 0 or 1 on each axis. */
  onArrow: (dx: number, dy: number) => void;
  disabled?: boolean;
}

// The drawing's largest size on the page, in its own units: a landscape screen is drawn 480 wide,
// a portrait one 360 high, so a turned screen does not tower over the controls.
const MAX_WIDTH = 480;
const MAX_HEIGHT = 360;
const PAD = 16;
const HANDLE = 10;
const ARROWS: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};

interface Drag {
  kind: "move" | "resize";
  /** The pointer where the last report was made, in screen pixels. */
  from: [number, number];
}

/**
 * RectangleEditor (render redesign S5, replacing QuadEditor): a screen drawn to its shape as it
 * hangs and the picture on it as one upright rectangle. Drag the picture to move it, or its
 * bottom-right handle to make it larger or smaller (its shape and centre kept); with the drawing
 * focused the arrow keys report a step. It holds no geometry rules: every move and size is the
 * caller's to accept (domain/frame-position.tsx, by src/placement.js). The handle is drawn in the
 * accent because it is what you act on; the picture in the text colour.
 */
export function RectangleEditor({ label, screen, picture, onMove, onResize, onArrow, disabled = false }: RectangleEditorProps) {
  const scale = Math.min(MAX_WIDTH / screen.width, MAX_HEIGHT / screen.height);
  const drawn = { width: screen.width * scale, height: screen.height * scale };
  const view = { width: drawn.width + PAD * 2, height: drawn.height + PAD * 2 };
  const svgRef = React.useRef<SVGSVGElement | null>(null);
  const drag = React.useRef<Drag | null>(null);

  // The pointer in screen pixels (the drawing may be shown smaller or larger than its view box).
  const pixels = (event: React.PointerEvent): [number, number] => {
    const box = svgRef.current!.getBoundingClientRect();
    const shown = box.width / view.width;
    return [((event.clientX - box.left) / shown - PAD) / scale, ((event.clientY - box.top) / shown - PAD) / scale];
  };
  const start = (event: React.PointerEvent, kind: Drag["kind"]) => {
    if (disabled || picture === null) return;
    event.preventDefault();
    event.stopPropagation();
    svgRef.current?.setPointerCapture?.(event.pointerId);
    drag.current = { kind, from: pixels(event) };
  };
  const move = (event: React.PointerEvent) => {
    const current = drag.current;
    if (current === null) return;
    const [x, y] = pixels(event);
    const dx = Math.round(x - current.from[0]);
    const dy = Math.round(y - current.from[1]);
    if (current.kind === "move") {
      if (dx === 0 && dy === 0) return;
      current.from = [current.from[0] + dx, current.from[1] + dy];
      onMove(dx, dy);
    } else {
      // The handle is the bottom-right corner and the centre stays: the width changes by twice
      // the handle's move across.
      if (dx === 0) return;
      current.from = [current.from[0] + dx, current.from[1]];
      onResize(dx * 2);
    }
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

  const box = picture === null ? null : {
    x: picture.x * scale, y: picture.y * scale, width: picture.width * scale, height: picture.height * scale,
  };
  return (
    <svg
      ref={svgRef}
      role="group"
      aria-label={label}
      aria-disabled={disabled || undefined}
      tabIndex={disabled ? -1 : 0}
      viewBox={`0 0 ${view.width} ${view.height}`}
      className={cn(
        "block h-auto w-full touch-none rounded-card bg-surface-sunken",
        // A portrait screen is drawn narrower, so it is no taller than a landscape one.
        screen.width >= screen.height ? "max-w-xl" : "max-w-72",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
      )}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={end}
      onKeyDown={key}
    >
      <g transform={`translate(${PAD}, ${PAD})`}>
        <rect x={0} y={0} width={drawn.width} height={drawn.height} className="fill-surface stroke-line-input" strokeWidth={1} />
        {box !== null && (
          <>
            <rect
              role="img"
              aria-label="Picture"
              x={box.x}
              y={box.y}
              width={box.width}
              height={box.height}
              className={cn("fill-text/10 stroke-text", disabled ? "cursor-default" : "cursor-grab")}
              strokeWidth={2}
              onPointerDown={(event) => start(event, "move")}
            />
            {!disabled && (
              <rect
                role="img"
                aria-label="Picture size handle"
                x={box.x + box.width - HANDLE}
                y={box.y + box.height - HANDLE}
                width={HANDLE * 2}
                height={HANDLE * 2}
                rx={4}
                className="cursor-nwse-resize fill-accent stroke-surface"
                strokeWidth={2}
                onPointerDown={(event) => start(event, "resize")}
              />
            )}
          </>
        )}
      </g>
    </svg>
  );
}
