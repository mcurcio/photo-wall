import * as React from "react";

import { cn } from "../ui/cn";

/** A rectangle on the screen in whole pixels: its top-left corner and its size (src/placement.js `Placement`). */
export interface PixelRectangle {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** An edge of the screen as it hangs. */
export type ScreenEdge = "top" | "right" | "bottom" | "left";

export interface RectangleEditorProps {
  /** The editor's accessible name. */
  label: string;
  /** The screen as it hangs, in pixels: the drawing has its shape, and drags are measured in its pixels. */
  screen: { width: number; height: number };
  /** The picture on the screen, in its pixels; null draws the screen alone (the picture does not lie on it). */
  picture: PixelRectangle | null;
  /** The picture's name, in the drawing and to a screen reader (default "Picture"). */
  pictureLabel?: string;
  /** A rectangle just refused, where it would have lain (on the screen or past its edges): drawn as a
   * dashed outline in the notice colour, crossing the edge it breaks; null or absent for none. */
  refused?: PixelRectangle | null;
  /** The display's own top edge on the drawing, drawn thicker, so a turned screen shows its turn. */
  topEdge?: ScreenEdge;
  /** The picture dragged: its proposed move since the last report, in screen pixels (whole numbers);
   * the caller accepts or refuses it (the drawing then stays). */
  onMove: (dx: number, dy: number) => void;
  /** The corner handle dragged: its proposed change of the picture's width, in screen pixels (a
   * whole number, either sign); the picture keeps its shape and its centre. The caller accepts or refuses. */
  onResize: (dWidth: number) => void;
  /** An arrow key on the focused drawing: a step of -1, 0 or 1 on each axis. */
  onArrow: (dx: number, dy: number) => void;
  disabled?: boolean;
  /** The editor's controls: beside a portrait screen on a wide page (it is drawn narrow), below it otherwise. */
  aside?: React.ReactNode;
}

// The drawing's largest size in its own units: a landscape screen is drawn 480 wide, a portrait one
// 360 high, so a turned screen does not tower over the controls.
const MAX_WIDTH = 480;
const MAX_HEIGHT = 360;
const PAD = 16;
// The size handle as drawn (24 units across) and the area that takes the pointer (44 units).
const HANDLE = 12;
const HANDLE_HIT = 22;
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

interface Drag {
  kind: "move" | "resize";
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

/**
 * RectangleEditor (render redesign S5, replacing QuadEditor): a screen drawn to its shape as it
 * hangs and the picture on it as one upright rectangle. Drag the picture to move it, or its
 * bottom-right handle to make it larger or smaller (its shape and centre kept); with the drawing
 * focused the arrow keys report a step. It holds no geometry rules: every move and size is the
 * caller's to accept (domain/frame-position.tsx, by src/placement.js). The handle is drawn in the
 * accent because it is what you act on; the picture in the text colour, named inside; a refused
 * rectangle dashed in the notice colour, out to the padding round the screen so it is seen to cross
 * the edge it breaks; the display's own top edge thicker.
 */
export function RectangleEditor({
  label, screen, picture, pictureLabel = "Picture", refused = null, topEdge, onMove, onResize, onArrow,
  disabled = false, aside,
}: RectangleEditorProps) {
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
  const out = refused === null ? null : (() => {
    const left = Math.max(-PAD, refused.x * scale);
    const top = Math.max(-PAD, refused.y * scale);
    const right = Math.min(drawn.width + PAD, (refused.x + refused.width) * scale);
    const bottom = Math.min(drawn.height + PAD, (refused.y + refused.height) * scale);
    return { x: left, y: top, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
  })();
  const portrait = screen.width < screen.height;
  const shownWidth = portrait ? SHOWN_PORTRAIT : SHOWN_LANDSCAPE;
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
      onKeyDown={key}
    >
      <g transform={`translate(${PAD}, ${PAD})`}>
        <rect x={0} y={0} width={drawn.width} height={drawn.height} className="fill-surface stroke-line-input" strokeWidth={1} />
        {topEdge !== undefined && (
          <line {...edgeLine(topEdge, drawn.width, drawn.height)} aria-hidden="true" className="stroke-label"
            strokeWidth={5} strokeLinecap="round" />
        )}
        {box !== null && (
          <>
            <rect
              role="img"
              aria-label={pictureLabel}
              x={box.x}
              y={box.y}
              width={box.width}
              height={box.height}
              className={cn("fill-text/10 stroke-text", disabled ? "cursor-default" : "cursor-grab")}
              strokeWidth={2}
              onPointerDown={(event) => start(event, "move")}
            />
            {box.width > 150 && box.height > 40 && (
              <text x={box.x + box.width / 2} y={box.y + box.height / 2} aria-hidden="true" textAnchor="middle"
                dominantBaseline="middle" fontSize={13} className="pointer-events-none fill-muted font-sans">
                {pictureLabel}
              </text>
            )}
            {!disabled && (
              <>
                <rect
                  aria-hidden="true"
                  x={box.x + box.width - HANDLE}
                  y={box.y + box.height - HANDLE}
                  width={HANDLE * 2}
                  height={HANDLE * 2}
                  rx={5}
                  className="pointer-events-none fill-accent stroke-surface"
                  strokeWidth={2}
                />
                <rect
                  role="img"
                  aria-label="Picture size handle"
                  x={box.x + box.width - HANDLE_HIT}
                  y={box.y + box.height - HANDLE_HIT}
                  width={HANDLE_HIT * 2}
                  height={HANDLE_HIT * 2}
                  className="cursor-nwse-resize fill-transparent"
                  onPointerDown={(event) => start(event, "resize")}
                />
              </>
            )}
          </>
        )}
        {out !== null && (
          <rect
            role="img"
            aria-label="Refused position"
            x={out.x}
            y={out.y}
            width={out.width}
            height={out.height}
            className="pointer-events-none fill-none stroke-notice"
            strokeWidth={2.5}
            strokeDasharray="8 5"
          />
        )}
      </g>
    </svg>
  );
  if (aside === undefined) return drawing;
  return (
    <div className={cn("flex min-w-0 flex-col gap-4", portrait && "md:flex-row md:items-start")}>
      {drawing}
      <div className="flex min-w-0 flex-1 flex-col gap-4">{aside}</div>
    </div>
  );
}
