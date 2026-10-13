import * as React from "react";

import { Button } from "../ui/button";
import { cn } from "../ui/cn";
import { Disclosure } from "../ui/disclosure";
import { Section } from "../ui/section";
import { Note } from "./fact-row";

/** One change in a history: when, what kind of change in words, whether it was kept, a one-line
 * summary, every value under Details, and whether it may be restored now (with why not). */
export interface HistoryRow {
  id: string;
  time: string;
  what: string;
  notKept: boolean;
  summary: string;
  details: readonly { label: string; value: string }[];
  restore: { disabled: boolean; reason?: string };
}

/** The rows of one day, under its label. */
export interface HistoryDay {
  label: string;
  rows: readonly HistoryRow[];
}

export interface HistoryListProps {
  /** The section's heading and its region's accessible name. */
  title: string;
  /** What the list holds and what Restore does. */
  intro: string;
  /** A line above the days (something the reader should know before restoring). */
  notice?: string;
  /** Newest first; empty days are not shown. */
  days: readonly HistoryDay[];
  /** Shown when there is no row and nothing is being read. */
  empty: string;
  /** A read in flight: with no row yet, the list shows its skeleton. */
  busy: boolean;
  /** A problem (a read or a Restore that failed), shown above the days. */
  problem?: React.ReactNode;
  /** The row being restored now; every Restore waits meanwhile. */
  restoring: string | null;
  onRestore: (id: string) => void;
  /** Loads older rows; none: there are no more. */
  onOlder?: () => void;
}

function Skeleton() {
  return (
    <div role="group" aria-busy="true" aria-label="Reading the history" className="flex min-w-0 flex-col gap-3">
      {[0, 1, 2].map((row) => (
        <div key={row} className="flex items-center gap-3">
          <div className="h-3 w-16 rounded-pill bg-surface-sunken" />
          <div className="flex flex-1 flex-col gap-2">
            <div className="h-3 w-1/3 rounded-pill bg-surface-sunken" />
            <div className="h-2.5 w-full rounded-pill bg-surface-sunken" />
          </div>
        </div>
      ))}
    </div>
  );
}

function Row({ row, restoring, reasonId, onRestore }: {
  row: HistoryRow; restoring: string | null; reasonId: string | null; onRestore: (id: string) => void;
}) {
  const ownReason = React.useId();
  const reason = row.restore.disabled && row.restore.reason !== undefined ? row.restore.reason : null;
  // A reason every row shares is said once above the days (`reasonId`); a row's own one beside it.
  const described = reason === null ? undefined : reasonId ?? ownReason;
  return (
    <li
      aria-label={`${row.time} ${row.what}`}
      data-kept={row.notKept ? "false" : "true"}
      className={cn(
        "flex min-w-0 items-start gap-x-3 gap-y-1 border-0 border-l-2 py-2 pl-3",
        "max-md:flex-col",
        row.notKept ? "border-dashed border-line-input" : "border-accent",
      )}
    >
      <span className="min-w-16 shrink-0 text-sm text-label tabular-nums">{row.time}</span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className={cn("text-sm", row.notKept ? "text-muted italic" : "font-medium text-text")}>{row.what}</span>
        <span className="text-sm wrap-anywhere text-text">{row.summary}</span>
        <Disclosure summary="Details">
          <dl className="m-0 flex flex-col gap-0.5">
            {row.details.map((detail) => (
              <div key={detail.label} className="flex min-w-0 flex-wrap gap-x-3">
                <dt className="text-muted">{detail.label}</dt>
                <dd className="m-0 wrap-anywhere">{detail.value}</dd>
              </div>
            ))}
          </dl>
        </Disclosure>
      </div>
      <div className="flex min-w-0 shrink-0 flex-col items-start gap-1">
        <Button variant="ghost" aria-describedby={described}
          disabled={row.restore.disabled || restoring !== null} onClick={() => onRestore(row.id)}>
          {restoring === row.id ? "Restoring…" : "Restore"}
        </Button>
        {reason !== null && reasonId === null && (
          <span id={ownReason} className="text-sm text-muted">{reason}</span>
        )}
      </div>
    </li>
  );
}

/**
 * HistoryList: a titled history of changes, newest first, grouped by day; each row its time, what
 * changed and whether it was kept (a row not kept is marked: dashed and muted), a summary, every
 * value under Details, and Restore. A Restore reason every disabled row shares is said once above the
 * days. Loading shows a skeleton (`aria-busy`); "Show older" asks for the next rows.
 */
export function HistoryList({
  title, intro, notice, days, empty, busy, problem, restoring, onRestore, onOlder,
}: HistoryListProps) {
  const sharedId = React.useId();
  const shown = days.filter((day) => day.rows.length > 0);
  const rows = shown.flatMap((day) => day.rows);
  const reasons = new Set(rows.map((row) => (row.restore.disabled ? row.restore.reason ?? null : null)));
  const [onlyReason] = [...reasons];
  const shared = reasons.size === 1 && onlyReason !== null && onlyReason !== undefined && rows.length > 1
    ? onlyReason : null;
  return (
    <Section title={title}>
      <div className="flex min-w-0 flex-col gap-3">
        <p className="m-0 text-sm text-muted">{intro}</p>
        {notice !== undefined && <Note live>{notice}</Note>}
        {problem}
        {shared !== null && <p id={sharedId} className="m-0 text-sm text-muted">{shared}</p>}
        {shown.length === 0 ? (busy ? <Skeleton /> : <p className="m-0 text-sm text-muted">{empty}</p>) : (
          shown.map((day) => (
            <section key={day.label} aria-label={day.label} className="min-w-0">
              <h4 className="m-0 mb-1 text-sm font-medium text-label">{day.label}</h4>
              <ol className="m-0 flex list-none flex-col gap-1 p-0">
                {day.rows.map((row) => (
                  <Row key={row.id} row={row} restoring={restoring} onRestore={onRestore}
                    reasonId={shared !== null ? sharedId : null} />
                ))}
              </ol>
            </section>
          ))
        )}
        {onOlder !== undefined && shown.length > 0 && (
          <Button variant="ghost" className="w-fit" disabled={busy} onClick={onOlder}>Show older</Button>
        )}
      </div>
    </Section>
  );
}
