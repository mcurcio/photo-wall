import { cn } from "../ui/cn";
import { FrameTile } from "./crop-thumb";
import { FitAlertNote, type FrameFitView } from "./frame-fit-row";

/** The tiles' heights, and the classes that keep a row of them on one baseline. */
const HEIGHTS = { full: [88, "h-22"], small: [56, "h-14"] } as const;

export interface FrameFitTileProps {
  frame: FrameFitView;
  /** `full` (88 px high, its alert in words with its action) or `small` (56 px, its alert's
   * chip only); as wide as its Frame output's shape. */
  size?: "full" | "small";
  /** Its picture dimmed: an older answer, while a newer one is read. */
  faded?: boolean;
  /** False when its host already names the Frame (a checkbox labelled with it). */
  named?: boolean;
  /** Pressing its picture (a board's row, its sheet); without it the picture is a picture only. */
  onPress?: () => void;
  /** Its alert's chip only, without its sentence and action (default: at `small`). */
  brief?: boolean;
}

/**
 * FrameFitTile (follow-ups review): one Frame at its output's shape, filled with the photo it
 * would show, cropped as it would crop it, with its name, how many photos fit and, under a Frame
 * that would go without or with only a few, its alert, said once. The one drawing of a Frame
 * on a wall strip and in a Frame picker.
 */
export function FrameFitTile({ frame, size = "full", faded = false, named = true, onPress, brief = size === "small" }: FrameFitTileProps) {
  const [height, row] = HEIGHTS[size];
  // Every tile sits on one baseline, so the names under a row of them line up.
  const picture = (
    <span className={cn("flex items-end", row, faded ? "opacity-60" : "")}>
      <FrameTile aspect={frame.aspect} lead={frame.lead} height={height}
        label={`${frame.name}, ${frame.ratio}`} emptyText={frame.emptyText} />
    </span>
  );
  return (
    <span className="flex min-w-0 flex-col gap-1">
      {onPress ? (
        <button type="button" onClick={onPress} aria-label={`${frame.name}: ${frame.count}`}
          className="cursor-pointer self-start border-0 bg-transparent p-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
          {picture}
        </button>
      ) : picture}
      {named && <span className="max-w-48 text-sm font-medium wrap-anywhere text-text">{frame.name}</span>}
      <span className="text-xs text-muted tabular-nums">{frame.count}</span>
      {frame.alert !== null && (
        <span className="block max-w-64"><FitAlertNote alert={frame.alert} brief={brief} /></span>
      )}
    </span>
  );
}

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
 * each a `FrameFitTile`: drawn at its Frame output's shape and filled with a different photo it
 * would show, with its name, how many photos fit it and, under a Frame that would go without or
 * with only a few, its alert. It shows how a Scene fills its Frames together, as a picture, and
 * is the one place a board says which Frames go without.
 */
export function WallStrip({ frames, size = "full", faded = false, onFrame, label = "On the wall together" }: WallStripProps) {
  return (
    <ul aria-label={label} className="m-0 flex min-w-0 list-none flex-wrap items-start gap-x-6 gap-y-4 p-0">
      {frames.map((frame) => (
        <li key={frame.key} className="min-w-0">
          <FrameFitTile frame={frame} size={size} faded={faded} onPress={onFrame ? () => onFrame(frame.key) : undefined} />
        </li>
      ))}
    </ul>
  );
}
