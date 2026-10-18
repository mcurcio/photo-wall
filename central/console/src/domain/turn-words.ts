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

/** Each turn in words: where the screen's own top edge is, as you face it. */
export const TURN_WORDS: Record<Turn, string> = {
  0: "Upright",
  90: "Top to the right",
  180: "Upside down",
  270: "Top to the left",
};
