import type * as React from "react";

import { Button } from "../ui/button";

/** What is over the schedule: a display power hold, Show now on top, or a Frame holding its photo. */
export type OverrideBannerKind = "power-hold" | "takeover" | "pause";

export interface OverrideBannerProps {
  /** Which kind of override this is; said to a test and a style hook as `data-kind`. */
  kind: OverrideBannerKind;
  /** The region's accessible name ("Holds", "Show now", "Paused"). */
  label: string;
  /** What is in force, in a few words ("Held off until 23:00", "Paused until 22:04"). */
  title: string;
  /** Its one way back, when the banner as a whole has one ("Back to normal", "Carry on"). */
  action?: { label: string; onAction: () => void; busy?: boolean };
  /** Its lines: what it covers, who set it; a hold's rows each carry their own way back. */
  children?: React.ReactNode;
}

/**
 * OverrideBanner (design language §4, F4, F5 and F8): something a person put in force over the
 * schedule, at the top of the page that shows it, with its way back: a display power hold (All
 * off or All on · **Resume schedule**), a takeover (Show now · **Back to normal**) or a pause (a
 * Frame holding its photo · **Carry on**). A plain bordered region, never a status colour: an
 * override is not a problem.
 */
export function OverrideBanner({ kind, label, title, action, children }: OverrideBannerProps) {
  return (
    <section aria-label={label} data-kind={kind}
      className="flex min-w-0 flex-col gap-2 rounded-card border-2 border-line-input bg-surface-sunken px-4 py-3">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h3 className="m-0 min-w-0 text-base font-medium wrap-anywhere text-text">{title}</h3>
        {action !== undefined && (
          <Button className="shrink-0" disabled={action.busy} onClick={action.onAction}>{action.label}</Button>
        )}
      </div>
      {children}
    </section>
  );
}
