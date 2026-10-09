import type * as React from "react";

import type { Severity } from "../design/tokens";
import { cn } from "./cn";
import { SeverityMark, severityTint } from "./severity";

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
        severityTint({ severity }),
      )}
    >
      <SeverityMark severity={severity} />
      {children}
    </span>
  );
}
