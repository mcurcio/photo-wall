import type * as React from "react";

import { cn } from "./cn";

/*
 * Table: native table markup in the console's dense style (shadcn's Table parts). The rows
 * and their order belong to the pattern that renders them (EntityList), not to a table
 * library: see .claude/errata.md E-CDS-FIX-6.
 */

export function Table({ className, ...props }: React.ComponentProps<"table">) {
  return (
    <div className="w-full min-w-0 overflow-x-auto" tabIndex={0}>
      <table
        className={cn("w-full border-collapse font-sans text-sm text-text", className)}
        {...props}
      />
    </div>
  );
}

export function TableCaption({ className, ...props }: React.ComponentProps<"caption">) {
  return <caption className={cn("mb-2 text-left text-sm text-muted", className)} {...props} />;
}

export function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return <thead className={cn("border-b border-line", className)} {...props} />;
}

export function TableBody(props: React.ComponentProps<"tbody">) {
  return <tbody {...props} />;
}

export function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return <tr className={cn("border-b border-line last:border-b-0", className)} {...props} />;
}

export function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      className={cn("px-3 py-row text-left align-bottom text-xs font-medium text-muted", className)}
      {...props}
    />
  );
}

export function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return <td className={cn("px-3 py-row align-top", className)} {...props} />;
}

/** The cell that names its row (`scope="row"`). */
export function TableRowHeader({ className, ...props }: React.ComponentProps<"th">) {
  return <th scope="row" className={cn("px-3 py-row text-left align-top font-normal", className)} {...props} />;
}
