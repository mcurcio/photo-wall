import type * as React from "react";

import { cn } from "./cn";

export interface LinkProps {
  href: string;
  children: React.ReactNode;
}

/** Link (design language §4): text that goes somewhere, in the accent (you can act here). */
export function Link({ href, children }: LinkProps) {
  return (
    <a
      href={href}
      className={cn(
        "text-accent",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
      )}
    >
      {children}
    </a>
  );
}
