import type * as React from "react";

export interface SaveBarProps {
  /** Its accessible name ("Source actions"). */
  label: string;
  /** Save first, then the rest (Discard). */
  children: React.ReactNode;
  /** A line beside the buttons: why Save was refused, or a receipt. */
  message?: React.ReactNode;
}

/**
 * SaveBar (design language §4, the Save model's bar): an editor's Save and Discard, kept at the
 * bottom of the view while the page scrolls, so Save is never below a long preview.
 */
export function SaveBar({ label, children, message }: SaveBarProps) {
  return (
    <div
      role="group"
      aria-label={label}
      className="sticky bottom-0 z-10 flex min-w-0 flex-wrap items-center gap-3 border-t border-line bg-surface px-1 py-3"
    >
      {children}
      {message ? <div className="min-w-0 text-sm text-muted">{message}</div> : null}
    </div>
  );
}
