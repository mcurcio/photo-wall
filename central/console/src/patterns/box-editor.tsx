import * as React from "react";

/*
 * BoxEditor (frame box run, .claude/runs/frame-box/page.md, bead fb-4; owner, 2026-10-19: "I need
 * to be able to set width and height independently, along with the position of that box on the
 * display"): a screen drawn to its shape as it hangs and the Frame's box on it. Drag the box to
 * move it, any of its four edges or four corners to resize it (the opposite edges stay put; width
 * and height change independently), or, with the drawing focused, the arrow keys to report a step.
 * It holds no geometry rule: every move and size is the caller's to accept (domain/frame-position.tsx,
 * by domain/position-box.ts). Replaces patterns/rectangle-editor.tsx in bead fb-7.
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
}

/** The box editor (module comment). */
export function BoxEditor(props: BoxEditorProps): React.JSX.Element {
  void props;
  throw new Error("frame box bead fb-4");
}
