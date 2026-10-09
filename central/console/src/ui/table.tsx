import {
  FlexRender,
  type ReactTable,
  type RowData,
  type TableFeatures,
} from "@tanstack/react-table";
import type * as React from "react";

import { cn } from "./cn";

/*
 * Table: native table markup in the console's dense style (shadcn's Table parts), and
 * DataTable, which renders a TanStack Table 9 instance (`useTable`) through them. TanStack
 * owns rows and state (sort, group, order); these parts own markup and look.
 */

export function Table({ className, ...props }: React.ComponentProps<"table">) {
  return (
    <div className="w-full min-w-0 overflow-x-auto">
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

export interface DataTableProps<TFeatures extends TableFeatures, TData extends RowData> {
  table: ReactTable<TFeatures, TData>;
  caption?: React.ReactNode;
  /** Shown in one full-width row when the table has no rows. */
  empty?: React.ReactNode;
}

/** Renders a TanStack Table 9 instance (`useTable`) as a Table. */
export function DataTable<TFeatures extends TableFeatures, TData extends RowData>({
  table,
  caption,
  empty = "Nothing to show.",
}: DataTableProps<TFeatures, TData>) {
  const rows = table.getRowModel().rows;
  const columns = table.getAllLeafColumns().length;
  return (
    <Table>
      {caption !== undefined && <TableCaption>{caption}</TableCaption>}
      <TableHeader>
        {table.getHeaderGroups().map((group) => (
          <TableRow key={group.id}>
            {group.headers.map((header) => (
              <TableHead key={header.id} colSpan={header.colSpan} scope="col">
                {header.isPlaceholder ? null : <FlexRender header={header} />}
              </TableHead>
            ))}
          </TableRow>
        ))}
      </TableHeader>
      <TableBody>
        {rows.length === 0 ? (
          <TableRow>
            <TableCell colSpan={columns} className="text-muted">
              {empty}
            </TableCell>
          </TableRow>
        ) : (
          rows.map((row) => (
            <TableRow key={row.id}>
              {row.getAllCells().map((cell) => (
                <TableCell key={cell.id}>
                  <FlexRender cell={cell} />
                </TableCell>
              ))}
            </TableRow>
          ))
        )}
      </TableBody>
    </Table>
  );
}
