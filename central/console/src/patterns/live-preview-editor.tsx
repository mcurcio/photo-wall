import type * as React from "react";

import { Button } from "../ui/button";
import { cn } from "../ui/cn";
import { AckBadge } from "./ack-badge";

export interface LivePreviewEditorProps {
  /** The bar's accessible name ("Show on the Display"). */
  label: string;
  /** The number of the change last made; null while no live session shows the changes. */
  latestRevision: number | null;
  /** The newest change the Pi acknowledged presenting, and when (already worded). */
  ack: { revision: number; at: string | null } | null;
  /** The changes differ from the saved values. */
  dirty: boolean;
  /** Done or Revert is running. */
  busy?: boolean;
  onDone: () => void;
  onRevert: () => void;
  /** The domain's controls (a position canvas and its nudge pad, a picture's sliders). */
  controls: React.ReactNode;
  /** The editor cannot be used: no controls, the reason in words and what to do. */
  unavailable?: { reason: React.ReactNode; action?: { label: string; onAction: () => void } };
  /** Words in place of the acknowledgement while no live session shows the changes ("Connecting…"). */
  status?: string;
  /** A problem (a ProblemCard), beside the bar. */
  problem?: React.ReactNode;
  /** Plain notes above the bar ("Saved at 21:04:07."). */
  notes?: readonly string[];
}

/**
 * LivePreviewEditor (design language §4): a tab that changes what a physical display shows.
 * Every change goes to the display as it is made; the bar at the bottom says whether the Pi has
 * acknowledged presenting the latest change, and holds **Done** and **Revert**. Done is enabled
 * only when the acknowledged change is the latest one (`ack.revision === latestRevision`): a
 * page cannot pass an enabled flag. When Done cannot be pressed, the bar says why. On a phone
 * the bar stays at the bottom of the screen while the controls scroll.
 */
export function LivePreviewEditor({
  label, latestRevision, ack, dirty, busy = false, onDone, onRevert, controls, unavailable, status, problem,
  notes = [],
}: LivePreviewEditorProps) {
  if (unavailable !== undefined) {
    return (
      <div className="flex min-w-0 flex-col items-start gap-2">
        <p role="status" className="m-0 text-sm text-text">{unavailable.reason}</p>
        {unavailable.action !== undefined && (
          <Button variant="ghost" onClick={unavailable.action.onAction}>{unavailable.action.label}</Button>
        )}
      </div>
    );
  }
  const acknowledged = latestRevision !== null && ack !== null && ack.revision === latestRevision;
  const canDone = dirty && acknowledged && !busy;
  const why = !dirty ? "No changes to keep." : acknowledged ? null : "Done is offered once the Pi presents your latest change.";
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {controls}
      {notes.map((note) => <p key={note} role="status" className="m-0 text-sm text-muted">{note}</p>)}
      {problem}
      <div
        role="group"
        aria-label={label}
        className={cn(
          "z-10 flex min-w-0 flex-wrap items-center gap-3 border-t border-line max-md:sticky max-md:bottom-0",
          "bg-surface-raised py-3",
        )}
      >
        {latestRevision === null ? (
          <span role="status" className="text-sm text-text">{status ?? ""}</span>
        ) : acknowledged ? (
          <AckBadge state="acknowledged" at={ack.at} />
        ) : (
          <AckBadge state="requested" />
        )}
        <span className="ml-auto flex flex-wrap items-center gap-2">
          {why !== null && <span className="text-xs text-muted">{why}</span>}
          <Button variant="primary" disabled={!canDone} onClick={onDone}>Done</Button>
          <Button disabled={!dirty || busy} onClick={onRevert}>Revert</Button>
        </span>
      </div>
    </div>
  );
}
