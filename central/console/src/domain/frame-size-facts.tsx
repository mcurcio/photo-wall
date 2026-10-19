import { boxSizeMm } from "../placement.js";
import { FactRow, Note } from "../patterns/fact-row";
import { boxPixels, shapeWords, sizeWords, type Box } from "./position-box";
import type { Turn } from "./turn-words";

/** What the Frame's size on its screen is not known for, in words. */
const NO_SCREEN = "Not known yet: the Pi has not reported this screen's resolution";
const NOT_CONNECTED = "Not known until this Frame is connected to a Pi";
/** A Display that states no size (frame box run decision 6). */
const NO_DISPLAY_SIZE = "Not known (this display does not state its size)";
const WHILE_UNSIZED = "Until then, only even room light is drawn and this Frame counts as large for videos.";

export interface FrameSizeFactsProps {
  /** The Frame's box: the saved Position on the Hardware tab, the draft on the Position tab. */
  box: Box;
  /** The screen as it hangs, in pixels (position-box.ts `screenOf`); null when not known. */
  screen: readonly [number, number] | null;
  /** The Display's own picture area in millimetres as built; null when none is seen or it states none. */
  displayMm: readonly [number, number] | null;
  turn: Turn;
  /** Whether the Frame is connected to a Pi's port (no wall size is said before). */
  bound: boolean;
}

/**
 * A Frame's two sizes, worked out from its box and never typed (decision 0030), with one wording on
 * every tab that says them (the Position tab's "This Frame", the Hardware tab's Frame section): "Size
 * on screen" (its pixels and shape) and "Size on the wall" (the box's share of the Display's own
 * size, in centimetres). Each says when it is not known and why.
 */
export function FrameSizeFacts({ box, screen, displayMm, turn, bound }: FrameSizeFactsProps) {
  const onWall = sizeWords(boxSizeMm(box, displayMm === null ? null : [displayMm[0], displayMm[1]], turn) as
    [number, number] | null);
  return (
    <>
      <FactRow label="Size on screen" tone={screen === null ? "unknown" : "derived"}>
        {screen !== null ? shapeWords(boxPixels(box, screen)) : bound ? NO_SCREEN : NOT_CONNECTED}
      </FactRow>
      {bound && (
        <FactRow label="Size on the wall" tone={onWall === null ? "unknown" : "derived"}>
          {onWall ?? NO_DISPLAY_SIZE}
        </FactRow>
      )}
      {bound && onWall === null && <Note>{WHILE_UNSIZED}</Note>}
    </>
  );
}
