import * as React from "react";

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
  /**
   * The saved values still have to be confirmed on the display (a Frame whose Position must be
   * checked again): Done is then offered with no change, and these words say what it keeps.
   */
  unconfirmed?: string;
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
  /** A problem (a ProblemCard), beside the bar; on a phone it is scrolled into view above the bar
   * when it appears. */
  problem?: React.ReactNode;
  /** The problem in one sentence, also said in the bar on a phone (where the bar may cover the card). */
  problemLine?: string;
  /** The last change was refused, so the Pi still presents the one before: the acknowledgement says so. */
  lastRefused?: boolean;
  /** Done cannot keep what is drafted (Central refused it): Done is not offered and these words say why. */
  doneBlocked?: string;
  /** Plain notes above the bar ("Saved at 21:04:07."). */
  notes?: readonly string[];
}

/**
 * LivePreviewEditor (design language §4): a tab that changes what a physical display shows.
 * Every change goes to the display as it is made; the bar at the bottom says whether the Pi has
 * acknowledged presenting the latest change, and holds **Done** and **Revert**. Done is enabled
 * only when the acknowledged change is the latest one (`ack.revision === latestRevision`): a
 * page cannot pass an enabled flag. With no change, Done is offered only while the saved values
 * are `unconfirmed`, and keeps them as they are. When Done cannot be pressed, the bar says why. On a phone
 * the bar stays at the bottom of the screen while the controls scroll, says the problem's sentence,
 * and a problem that appears is scrolled into view above it.
 */
export function LivePreviewEditor({
  label, latestRevision, ack, dirty, unconfirmed, busy = false, onDone, onRevert, controls, unavailable, status,
  problem, problemLine, lastRefused = false, doneBlocked, notes = [],
}: LivePreviewEditorProps) {
  const problemRef = React.useRef<HTMLDivElement | null>(null);
  const barRef = React.useRef<HTMLDivElement | null>(null);
  const hasProblem = problem !== undefined && problem !== null;
  // A problem that appears while the editor is open is brought into view, and above the bar where the
  // bar sticks over it (a phone); one there when the editor opens leaves the page where it is.
  const hadProblem = React.useRef(hasProblem);
  React.useEffect(() => {
    const card = problemRef.current;
    const appeared = hasProblem && !hadProblem.current;
    hadProblem.current = hasProblem;
    if (!appeared || card === null) return;
    card.scrollIntoView?.({ block: "nearest" });
    const bar = barRef.current?.getBoundingClientRect();
    const below = card.getBoundingClientRect().bottom - (bar?.top ?? Infinity);
    if (bar !== undefined && below > 0 && bar.bottom >= window.innerHeight - 1) window.scrollBy(0, below + 8);
  }, [hasProblem]);
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
  const keeping = dirty || unconfirmed !== undefined;
  const canDone = keeping && acknowledged && !busy && doneBlocked === undefined;
  const why = doneBlocked !== undefined ? doneBlocked
    : !keeping ? "No changes to keep."
      : !acknowledged ? "Done is offered once the Pi presents your latest change."
        : dirty ? null : unconfirmed ?? null;
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {controls}
      {notes.map((note) => <p key={note} role="status" className="m-0 text-sm text-muted">{note}</p>)}
      {hasProblem && <div ref={problemRef} className="min-w-0">{problem}</div>}
      <div
        ref={barRef}
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
          <AckBadge state="acknowledged" at={ack.at}
            note={lastRefused ? "your last change was refused" : undefined} />
        ) : (
          <AckBadge state="requested" />
        )}
        {problemLine !== undefined && (
          <span className="w-full text-sm text-text md:hidden">{problemLine}</span>
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
