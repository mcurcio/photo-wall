import type * as React from "react";

export interface EntityPageProps {
  /** The page's EntityHeader. */
  header: React.ReactNode;
  /** Its Sections, in order. */
  children: React.ReactNode;
}

/** EntityPage: one entity's page, its header above its sections. */
export function EntityPage({ header, children }: EntityPageProps) {
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {header}
      {children}
    </div>
  );
}

/** EmptyState: a page or section with nothing to show, and why. */
export function EmptyState({ children }: { children: React.ReactNode }) {
  return <p className="m-0 text-sm text-muted">{children}</p>;
}
