import type * as React from "react";

import type { Turn } from "./turn-words";

export interface FrameTurnProps {
  frameId: string;
  /** The Frame's turn as Central serves it (the inventory's `turn`). */
  turn: Turn;
  /** The Frame's generation as served: the turn is set against it (Central refuses a stale one). */
  generation: number;
}

/**
 * The Hardware tab's "How the screen hangs" (render redesign S5; the Frame's turn, owner gate Q3,
 * 2026-10-17: the turn belongs to the spot on the wall; one Frame per screen, Q4): the four turns
 * in words (turn-words.ts) as one choice, set the moment it is chosen (framesApi.js `turnFrame`,
 * against the generation shown), no Done. A change turns the screen's picture on the Pi and makes
 * the Frame's Position one to check again (Central: readiness position-needed), which the section
 * says with a way to the Position tab. Refusals in words: the Frame changed meanwhile (409
 * binding_generation_conflict), the Frame is gone (404 unknown_frame), no answer.
 */
export function FrameTurn(props: FrameTurnProps): React.ReactElement {
  void props;
  throw new Error("render-planes S5 bead B5");
}
