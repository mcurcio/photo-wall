import type * as React from "react";

import type { Severity } from "../design/tokens";
import { cn } from "./cn";
import { SeverityMark, severityText, severityTint } from "./severity";

export interface AlertProps {
  /** How bad it is; `alarm` is the destructive alert (shadcn's `destructive` variant). */
  severity: Severity;
  /** What happened, in the model's words: the alert's heading. */
  title: string;
  /** Why, and the one fix, in the model's words. */
  children?: React.ReactNode;
}

/**
 * Alert: a block that says something needs attention (shadcn's Alert idiom: a tinted,
 * bordered box led by its severity's mark, a title and a description). An `alarm` alert is
 * announced at once (`role="alert"`); any other severity is a polite `status`. The title
 * takes its severity's colour over the severity's tint; the description stays in body text
 * so it reads in both schemes.
 */
export function Alert({ severity, title, children }: AlertProps) {
  return (
    <div
      role={severity === "alarm" ? "alert" : "status"}
      data-severity={severity}
      className={cn(
        "my-2 flex min-w-0 gap-2 rounded-input border px-3 py-2 text-sm text-text",
        severityTint({ severity }),
      )}
    >
      <SeverityMark severity={severity} />
      <div className="grid min-w-0 gap-1 wrap-anywhere">
        <p className={cn("m-0 font-semibold", severityText({ severity }))}>{title}</p>
        {children}
      </div>
    </div>
  );
}

/** One sentence of an alert's description. */
export function AlertLine({ children }: { children: React.ReactNode }) {
  return <p className="m-0">{children}</p>;
}
