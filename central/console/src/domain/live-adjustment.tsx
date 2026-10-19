import type * as React from "react";

import type { useFrameAdjustment } from "../liveAdjustment.js";
import { Note } from "../patterns/fact-row";
import { LivePreviewEditor } from "../patterns/live-preview-editor";
import { ProblemCard } from "../patterns/problem-card";
import { timeOfDay } from "../timeWords.js";
import { Link } from "../ui/link";
import { Stack } from "../ui/stack";

/** The Frame page's live adjustment, as liveAdjustment.js serves it. */
export type Adjustment = ReturnType<typeof useFrameAdjustment>;

/** Whether the Frame's Pi can show changes live, so the controls are worth showing. */
export function adjustable(adjustment: Adjustment): boolean {
  return !["checking", "not_admitted", "unavailable", "unbound"].includes(adjustment.phase);
}

export interface LiveAdjustmentProps {
  adjustment: Adjustment;
  /** What is adjusted, for the words: "position", "picture". */
  noun: string;
  /** The Frame's Pi's page, for a Pi that did not start from Photo Wall. */
  piHref: string | null;
  /** The saved values must be confirmed on the display, in words (LivePreviewEditor `unconfirmed`). */
  unconfirmed?: string;
  /** The tab's controls. */
  children: React.ReactNode;
  /** Central's refusals the tab says itself (in `refusal`), by code: they get no card here. */
  ownRefusals?: ReadonlySet<string>;
  /** A change the tab refused, or Central refused for it: its card, its sentence for the bar on a
   * phone, and why Done is not offered while it stands (none: Done keeps what the Pi presents). */
  refusal?: { card: React.ReactNode; line: string; blocksDone?: string };
}

function unavailableWords(adjustment: Adjustment, noun: string, piHref: string | null): React.ReactNode | null {
  switch (adjustment.phase) {
    case "checking":
      return "Checking whether this Frame's Pi can show changes live…";
    case "not_admitted":
      return (
        <>
          {`This Frame's Pi did not start from Photo Wall, so its ${noun} cannot be changed here. Restart the Pi so it starts from Photo Wall, then come back.`}
          {piHref !== null && <>{" "}<Link href={piHref}>Open the Pi&apos;s page</Link></>}
        </>
      );
    case "unavailable":
      return "Photo Wall could not check this Frame's Pi.";
    case "unbound":
      return "Choose which Pi and HDMI output feed this Frame first (Hardware tab).";
    default:
      return null;
  }
}

function statusWords(adjustment: Adjustment): string {
  switch (adjustment.phase) {
    case "waiting":
      return "Someone else is adjusting this Frame. Photo Wall connects as soon as they finish.";
    case "stopped":
      return "Your changes are not being sent to the Pi.";
    default:
      return "Connecting to the Pi…";
  }
}

/**
 * A live adjustment's tab (Position, Picture) as a LivePreviewEditor: the controls, Central's
 * refusals as an inline ProblemCard (its code under Details) unless the tab says them itself (a
 * refused change: `ownRefusals`, `refusal`), and the bottom bar with the Pi's acknowledgement
 * (Central's time of it), **Done** and **Revert**.
 */
export function LiveAdjustment({
  adjustment, noun, piHref, unconfirmed, children, ownRefusals, refusal,
}: LiveAdjustmentProps) {
  const reason = unavailableWords(adjustment, noun, piHref);
  const error = adjustment.error !== null && ownRefusals?.has(adjustment.error.code) ? null : adjustment.error;
  const notes = [
    ...(adjustment.leftAt !== null
      ? [`Your unsaved changes were reverted at ${timeOfDay(adjustment.leftAt)} because you left this Frame's page.`]
      : []),
    ...(adjustment.savedAt !== null && !adjustment.dirty ? [`Saved at ${timeOfDay(adjustment.savedAt)}.`] : []),
  ];
  return (
    <LivePreviewEditor
      label="Show on the Display"
      latestRevision={adjustment.latest}
      ack={adjustment.ack === null ? null : {
        revision: adjustment.ack.revision,
        at: adjustment.ack.at === null ? null : timeOfDay(adjustment.ack.at),
      }}
      dirty={adjustment.dirty}
      unconfirmed={unconfirmed}
      busy={adjustment.busy}
      onDone={() => { void adjustment.done(); }}
      onRevert={() => { void adjustment.revert(); }}
      controls={<Stack>{children}</Stack>}
      unavailable={reason === null ? undefined : {
        reason,
        action: adjustment.phase === "unavailable" ? { label: "Try again", onAction: adjustment.retry } : undefined,
      }}
      status={statusWords(adjustment)}
      notes={notes}
      problemLine={refusal?.line}
      lastRefused={refusal !== undefined}
      doneBlocked={refusal?.blocksDone}
      problem={refusal !== undefined ? refusal.card : error === null ? undefined : (
        <ProblemCard
          scope="central"
          variant="inline"
          live
          verdict={{ severity: error.stops ? "alarm" : "notice", label: error.stops ? "Not sent" : "Retrying", receipt: null }}
          what={error.words}
          doing={error.stops ? null : "Photo Wall keeps trying."}
          action={{ label: "Revert", onAction: () => { void adjustment.revert(); } }}
          details={<Note>{`Photo Wall's answer: ${error.code}`}</Note>}
        />
      )}
    />
  );
}
