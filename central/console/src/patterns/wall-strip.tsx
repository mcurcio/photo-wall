import { cn } from "../ui/cn";
import { FrameTile } from "./crop-thumb";
import type { FrameFitView } from "./frame-fit-row";

export interface WallStripProps {
  /** The Frames, each with the photo it would show (the answer's leads are distinct across
   * Frames, as the Planner never shows one photo on two Frames at once). */
  frames: readonly FrameFitView[];
  /** `full` tiles (88 px high) or `small` ones (56 px, under a line); each is as wide as its
   * Frame output's shape. */
  size?: "full" | "small";
  /** Its pictures dimmed: an older answer, while a newer one is read. */
  faded?: boolean;
  /** Pressing a Frame (its row, its sheet). */
  onFrame?: (frameKey: string) => void;
  /** The strip's accessible name ("On the wall together"). */
  label?: string;
}

/**
 * WallStrip (design language §4, follow-ups review): the Frames of a selection side by side,
 * each drawn at its Frame output's shape and filled with a different photo it would show,
 * cropped as it would crop it, with its name, how many photos fit it and, under a Frame that
 * would go without or with only a few, a short warning in words. It shows how a Scene fills its
 * Frames together, as a picture; the counts and reasons are the Frame rows'.
 */
/** The tiles' heights, and the classes that keep a row of them on one baseline. */
const HEIGHTS = { full: [88, "h-22"], small: [56, "h-14"] } as const;

export function WallStrip({ frames, size = "full", faded = false, onFrame, label = "On the wall together" }: WallStripProps) {
  const [height, row] = HEIGHTS[size];
  return (
    <ul aria-label={label} className="m-0 flex min-w-0 list-none flex-wrap items-start gap-x-4 gap-y-3 p-0">
      {frames.map((frame) => {
        // Every tile sits on one baseline, so the names under them line up.
        const tile = (
          <span className={cn("flex items-end", row, faded ? "opacity-60" : "")}>
            <FrameTile aspect={frame.aspect} lead={frame.lead} height={height}
              label={`${frame.name}, ${frame.ratio}`} emptyText={frame.emptyText} />
          </span>
        );
        return (
          <li key={frame.key} className="flex min-w-0 flex-col gap-1">
            {onFrame ? (
              <button type="button" onClick={() => onFrame(frame.key)} aria-label={`${frame.name}: ${frame.count}`}
                className="cursor-pointer self-start border-0 bg-transparent p-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
                {tile}
              </button>
            ) : tile}
            <span className="max-w-48 text-sm font-medium wrap-anywhere text-text">{frame.name}</span>
            <span className="text-xs text-muted tabular-nums">{frame.count}</span>
            {frame.alert !== null && (
              <span className="text-xs font-medium text-text">{frame.alert}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}
