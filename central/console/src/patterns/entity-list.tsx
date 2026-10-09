import type * as React from "react";

import { type Severity, WORST_FIRST } from "../design/tokens";
import { cn } from "../ui/cn";
import { severityBar } from "../ui/severity";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableRowHeader } from "../ui/table";

/** One column: its header and how a row's cell reads. `rowHeader` marks the naming column. */
export interface EntityColumn<Row> {
  id: string;
  header: string;
  cell: (row: Row) => React.ReactNode;
  rowHeader?: boolean;
}

/** A row: its key and its judge's severity (null: not judged, e.g. a spare or a retired box). */
export interface EntityRowBase {
  key: string;
  severity: Severity | null;
}

/**
 * One group of rows under its title. `worstFirst` orders it by severity, worst first, keeping
 * the given order among equals (rows not judged sort with ok). `columns` replaces the list's
 * columns for this group (a group whose values are not read shows fewer).
 */
export interface EntityGroup<Row extends EntityRowBase> {
  title: string;
  rows: readonly Row[];
  worstFirst?: boolean;
  columns?: readonly EntityColumn<Row>[];
}

export interface EntityListProps<Row extends EntityRowBase> {
  /** The list's heading and its region's accessible name. */
  label: string;
  columns: readonly EntityColumn<Row>[];
  groups: readonly EntityGroup<Row>[];
  /** Shown instead of the groups when no group has a row. */
  empty: React.ReactNode;
  /** Lines above the groups (a focus chip, a read that failed). */
  notes?: React.ReactNode;
}

const RANK = new Map(WORST_FIRST.map((severity, index) => [severity, index]));
const rank = (row: EntityRowBase) => RANK.get(row.severity ?? "ok") ?? WORST_FIRST.length;

/** A group's rows in display order: worst first when the group asks, stable among equals. */
export function orderRows<Row extends EntityRowBase>(group: EntityGroup<Row>): Row[] {
  const rows = [...group.rows];
  return group.worstFirst ? rows.sort((a, b) => rank(a) - rank(b)) : rows;
}

function GroupTable<Row extends EntityRowBase>({ group, columns }: {
  group: EntityGroup<Row>;
  columns: readonly EntityColumn<Row>[];
}) {
  return (
    <section aria-label={group.title} className="mt-4 min-w-0">
      <h3 className="m-0 mb-2 text-sm font-medium text-label">{group.title}</h3>
      <Table aria-label={group.title}>
        <TableHeader>
          <TableRow>
            {columns.map((column) => (
              <TableHead key={column.id} scope="col">{column.header}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {orderRows(group).map((row) => (
            <TableRow
              key={row.key}
              data-severity={row.severity ?? "none"}
              className={cn(row.severity !== null && severityBar({ severity: row.severity }))}
            >
              {columns.map((column) =>
                column.rowHeader ? (
                  <TableRowHeader key={column.id}>{column.cell(row)}</TableRowHeader>
                ) : (
                  <TableCell key={column.id}>{column.cell(row)}</TableCell>
                ),
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

/**
 * EntityList: a titled list of entities in named groups, each a table; empty groups are not
 * shown, and with no row at all the list shows its empty state. It orders a `worstFirst`
 * group by the severities its rows carry and never judges a row itself.
 */
export function EntityList<Row extends EntityRowBase>({ label, columns, groups, empty, notes }: EntityListProps<Row>) {
  const shown = groups.filter((group) => group.rows.length > 0);
  return (
    <section aria-label={label} className="min-w-0">
      <h2 className="m-0 mb-2 text-lg font-medium text-text">{label}</h2>
      {notes}
      {shown.length === 0 ? (
        <p className="m-0 mt-2 text-sm text-muted">{empty}</p>
      ) : (
        shown.map((group) => (
          <GroupTable key={group.title} group={group} columns={group.columns ?? columns} />
        ))
      )}
    </section>
  );
}
