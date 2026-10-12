import type * as React from "react";

export interface OverrideBannerProps {
  /** The region's accessible name ("Holds"). */
  label: string;
  /** What is in force, in a few words ("Held off until 23:00"). */
  title: string;
  /** Its lines: what it covers, who set it, its way back (a button). */
  children: React.ReactNode;
}

/**
 * OverrideBanner (design language §4, F4 and F5): something the operator put in force over the
 * schedule (All off or All on, later Show now and Pause), at the top of the page that owns it,
 * with its way back. A plain bordered region, never a status colour: a hold is not a problem.
 */
export function OverrideBanner({ label, title, children }: OverrideBannerProps) {
  return (
    <section aria-label={label} className="flex min-w-0 flex-col gap-2 rounded-card border-2 border-line-input bg-surface-sunken px-4 py-3">
      <h3 className="m-0 text-base font-medium text-text">{title}</h3>
      {children}
    </section>
  );
}
