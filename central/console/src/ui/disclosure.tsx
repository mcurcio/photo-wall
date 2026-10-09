import { Collapsible } from "@base-ui/react/collapsible";
import type * as React from "react";

import { cn } from "./cn";

export interface DisclosureProps {
  /** The always-visible line that opens and closes the details. */
  summary: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
}

/** Disclosure: details shown on request (replaces the console's `<details>`). */
export function Disclosure({ summary, defaultOpen = false, children }: DisclosureProps) {
  return (
    <Collapsible.Root defaultOpen={defaultOpen} className="min-w-0">
      <Collapsible.Trigger
        className={cn(
          "group inline-flex cursor-pointer items-center gap-1.5 border-0 bg-transparent p-0",
          "font-sans text-sm text-accent",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        )}
      >
        <span aria-hidden="true" className="transition-transform group-data-panel-open:rotate-90">
          ▸
        </span>
        {summary}
      </Collapsible.Trigger>
      <Collapsible.Panel className="mt-2 min-w-0 text-sm text-text">{children}</Collapsible.Panel>
    </Collapsible.Root>
  );
}
