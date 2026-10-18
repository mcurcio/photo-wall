import type * as React from "react";

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

/**
 * RectangleEditor (render redesign S5, replacing QuadEditor): a screen drawn to its shape as it
 * hangs and the picture on it as one upright rectangle. Drag the picture to move it, or its
 * bottom-right handle to make it larger or smaller (its shape and centre kept); with the drawing
 * focused the arrow keys report a step. It holds no geometry rules: every move and size is the
 * caller's to accept (domain/frame-position.tsx, by src/placement.js). The handle is drawn in the
 * accent because it is what you act on; the picture in the text colour.
 */
export function RectangleEditor(props: RectangleEditorProps): React.ReactElement {
  void props;
  throw new Error("render-planes S5 bead B2");
}
