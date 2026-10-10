import type * as React from "react";

interface GroupProps {
  /** With a label the container is a named group. */
  label?: string;
  children: React.ReactNode;
}

/** Row: controls side by side (fields, or a set of buttons), wrapping on a narrow screen. */
export function Row({ label, children }: GroupProps) {
  return (
    <div role={label ? "group" : undefined} aria-label={label} className="flex min-w-0 flex-wrap items-end gap-3">
      {children}
    </div>
  );
}

/** Stack: blocks one under the other, evenly spaced. */
export function Stack({ label, children }: GroupProps) {
  return (
    <div role={label ? "group" : undefined} aria-label={label} className="flex min-w-0 flex-col gap-3">
      {children}
    </div>
  );
}
