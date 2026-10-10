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
  return !["checking", "unsupported", "unavailable", "unbound"].includes(adjustment.phase);
}

export interface LiveAdjustmentProps {
  adjustment: Adjustment;
  /** What is adjusted, for the words: "position", "picture". */
  noun: string;
  /** The Frame's Pi's Software page, for a Pi that cannot show changes live. */
  softwareHref: string | null;
  /** The tab's controls. */
  children: React.ReactNode;
}

function unavailableWords(adjustment: Adjustment, noun: string, softwareHref: string | null): React.ReactNode | null {
  switch (adjustment.phase) {
    case "checking":
      return "Checking whether this Frame's Pi can show changes live…";
    case "unsupported":
      return (
        <>
          {`This Frame's Pi runs software that cannot show changes live, so its ${noun} cannot be changed here. Update the Pi's software, then come back.`}
          {softwareHref !== null && <>{" "}<Link href={softwareHref}>Open the Pi&apos;s software page</Link></>}
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
 * refusals as an inline ProblemCard (its code under Details), and the bottom bar with the Pi's
 * acknowledgement (Central's time of it), **Done** and **Revert**.
 */
export function LiveAdjustment({ adjustment, noun, softwareHref, children }: LiveAdjustmentProps) {
  const reason = unavailableWords(adjustment, noun, softwareHref);
  const { error } = adjustment;
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
      problem={error === null ? undefined : (
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
