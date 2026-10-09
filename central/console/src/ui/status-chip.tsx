import type * as React from "react";

import type { Severity } from "../design/tokens";
import { cn } from "./cn";

/** A severity's shape: a chip never relies on colour alone. */
const MARK: Record<Severity, string> = {
  ok: "●",
  todo: "■",
  notice: "◆",
  alarm: "▲",
  unknown: "○",
};

/** Whole class names, so Tailwind's scan finds each one. */
const TONE: Record<Severity, string> = {
  ok: "border-ok bg-ok/12",
  todo: "border-todo bg-todo/12",
  notice: "border-notice bg-notice/12",
  alarm: "border-alarm bg-alarm/12",
  unknown: "border-unknown bg-unknown/12",
};

const MARK_TONE: Record<Severity, string> = {
  ok: "text-ok",
  todo: "text-todo",
  notice: "text-notice",
  alarm: "text-alarm",
  unknown: "text-unknown",
};

export interface StatusChipProps {
  severity: Severity;
  /** The model's words for the state; the chip never re-judges it. */
  children: React.ReactNode;
}

/**
 * StatusChip: a state in words, in text colour over its severity's tint inside a 1 px
 * severity border, led by the severity's shape (hidden from the accessible name).
 */
export function StatusChip({ severity, children }: StatusChipProps) {
  return (
    <span
      data-severity={severity}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-0.5 text-xs text-text",
        TONE[severity],
      )}
    >
      <span aria-hidden="true" className={MARK_TONE[severity]}>
        {MARK[severity]}
      </span>
      {children}
    </span>
  );
}
