import type { FrameTurnChange } from "../generated/contract";

/*
 * How a Frame's screen hangs (render redesign S5; contracts/display_turn.py, the Frame's turn,
 * owner gate Q3, 2026-10-17: the turn belongs to the spot on the wall), in the operator's words.
 * One home for the four words: the Hardware tab's turn control (frame-turn.tsx) and the Position
 * tab's line naming it (frame-position.tsx) both read them. Pure: no React, no request.
 */

/** A turn, degrees clockwise as the screen hangs: Central's own union. */
export type Turn = FrameTurnChange["turn"];

/** The four turns, in the control's order. */
export const TURNS: readonly Turn[] = [0, 90, 180, 270];

/** Each turn in words, as the choice names it: where the display's own top edge points, as you face it. */
export const TURN_WORDS: Record<Turn, string> = {
  0: "Upright",
  90: "Top to the right",
  180: "Upside down",
  270: "Top to the left",
};

/** How the screen hangs, inside a sentence: "This screen hangs upright.", "Made when the screen
 * hung with its top to the right." (the Position tab's line and a History row's). */
export const HANGS: Record<Turn, string> = {
  0: "upright",
  90: "with its top to the right",
  180: "upside down",
  270: "with its top to the left",
};

/** Which edge of the screen drawn as it hangs is the display's own top edge. */
export const TOP_EDGE: Record<Turn, "top" | "right" | "bottom" | "left"> = {
  0: "top",
  90: "right",
  180: "bottom",
  270: "left",
};
