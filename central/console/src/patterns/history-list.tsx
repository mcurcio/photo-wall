import * as React from "react";

import { Button } from "../ui/button";
import { cn } from "../ui/cn";
import { Disclosure } from "../ui/disclosure";
import { Link } from "../ui/link";
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
  /** `reason`: why Restore is not offered; `link`: where that is put right (the tab that owns it). */
  restore: { disabled: boolean; reason?: string; link?: { text: string; href: string } };
  /** The tuning session it came from (none: not from one). Consecutive rows not kept from one
   * session are grouped: the newest shows, the earlier ones open on request. */
  session?: string | null;
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

/** A value `details` and `below` both name by its label, in one line, "Label value · Label value". */
const line = (pairs: readonly { label: string; value: string }[]) =>
  pairs.map(({ label, value }) => `${label} ${value}`).join(" · ");

/**
 * Every history's one summary rule: the values of `details` that differ from `below`'s (the row under
 * it), in one line, then each value `below` had and `details` does not, as `absent` names it (null:
 * not said); all of `details` for the oldest row shown (`below` null); `same` when nothing differs.
 */
export function changedSummary(details: readonly { label: string; value: string }[],
  below: readonly { label: string; value: string }[] | null,
  { absent, same }: { absent: (label: string) => string | null; same: string }): string {
  if (below === null) return line(details);
  const was = new Map(below.map(({ label, value }) => [label, value]));
  const now = new Set(details.map(({ label }) => label));
  const gone = below.filter(({ label }) => !now.has(label)).flatMap(({ label }) => {
    const value = absent(label);
    return value === null ? [] : [{ label, value }];
  });
  const changed = [...details.filter(({ label, value }) => was.get(label) !== value), ...gone];
  return changed.length === 0 ? same : line(changed);
}

/** A day's rows as shown: a row alone, or a run of rows not kept from one session (two or more). */
type Shown = { row: HistoryRow } | { session: string; rows: readonly HistoryRow[] };

function runsOf(rows: readonly HistoryRow[]): Shown[] {
  const shown: Shown[] = [];
  for (const row of rows) {
    const last = shown.at(-1);
    const session = row.notKept ? row.session ?? null : null;
    if (session !== null && last !== undefined && "session" in last && last.session === session) {
      shown[shown.length - 1] = { session, rows: [...last.rows, row] };
    } else if (session !== null && last !== undefined && "row" in last && last.row.notKept
      && (last.row.session ?? null) === session) {
      shown[shown.length - 1] = { session, rows: [last.row, row] };
    } else {
      shown.push({ row });
    }
  }
  return shown;
}

const tries = (count: number) => (count === 1 ? "1 earlier try" : `${count} earlier tries`);

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
        "flex min-w-0 flex-col gap-1 border-0 border-l-2 py-2 pl-3",
        row.notKept ? "border-dashed border-line-input" : "border-accent",
      )}
    >
      {/* The first line: the time, what kind of change, and Restore at its end (on a phone too). */}
      <div className="flex min-w-0 items-center gap-x-3">
        <span className="min-w-16 shrink-0 text-sm text-label tabular-nums">{row.time}</span>
        <span className={cn("min-w-0 flex-1 text-sm", row.notKept ? "text-muted italic" : "font-medium text-text")}>
          {row.what}
        </span>
        <Button variant="ghost" className="shrink-0 max-md:min-h-11" aria-describedby={described}
          disabled={row.restore.disabled || restoring !== null} onClick={() => onRestore(row.id)}>
          {restoring === row.id ? "Restoring…" : "Restore"}
        </Button>
      </div>
      <div className="flex min-w-0 flex-col gap-1 md:pl-19">
        <span className="text-sm wrap-anywhere text-text">{row.summary}</span>
        {reason !== null && reasonId === null && (
          <span id={ownReason} className="text-sm text-muted">
            {reason}
            {row.restore.link !== undefined && <>{" "}<Link href={row.restore.link.href}>{row.restore.link.text}</Link></>}
          </span>
        )}
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
    </li>
  );
}

/**
 * HistoryList: a titled history of changes, newest first, grouped by day; each row its time, what
 * changed and whether it was kept (a row not kept is marked: dashed and muted), a summary, every
 * value under Details, and Restore. Rows not kept from one session follow one another as one run:
 * its newest row shows, and "Show N earlier tries from this session" opens the rest. A Restore reason every disabled row shares is said once above the
 * days. Loading shows a skeleton (`aria-busy`); "Show older" asks for the next rows.
 */
export function HistoryList({
  title, intro, notice, days, empty, busy, problem, restoring, onRestore, onOlder,
}: HistoryListProps) {
  const sharedId = React.useId();
  // The runs opened, by day and session, so a poll's new rows keep a run open.
  const [opened, setOpened] = React.useState<ReadonlySet<string>>(new Set());
  const toggle = (key: string) => setOpened((now) => {
    const next = new Set(now);
    if (!next.delete(key)) next.add(key);
    return next;
  });
  const shown = days.filter((day) => day.rows.length > 0);
  const rows = shown.flatMap((day) => day.rows);
  const reasons = new Set(rows.map((row) => (row.restore.disabled ? row.restore.reason ?? null : null)));
  const [onlyReason] = [...reasons];
  const shared = reasons.size === 1 && onlyReason !== null && onlyReason !== undefined && rows.length > 1
    ? onlyReason : null;
  const sharedLink = shared === null ? undefined : rows.find((row) => row.restore.link !== undefined)?.restore.link;
  return (
    <Section title={title}>
      <div className="flex min-w-0 flex-col gap-3">
        <p className="m-0 text-sm text-muted">{intro}</p>
        {notice !== undefined && <Note live>{notice}</Note>}
        {problem}
        {shared !== null && (
          <p id={sharedId} className="m-0 text-sm text-muted">
            {shared}
            {sharedLink !== undefined && <>{" "}<Link href={sharedLink.href}>{sharedLink.text}</Link></>}
          </p>
        )}
        {shown.length === 0 ? (busy ? <Skeleton /> : <p className="m-0 text-sm text-muted">{empty}</p>) : (
          shown.map((day) => (
            <section key={day.label} aria-label={day.label} className="min-w-0">
              <h4 className="m-0 mb-1 text-sm font-medium text-label">{day.label}</h4>
              <ol className="m-0 flex list-none flex-col gap-1 p-0">
                {runsOf(day.rows).map((shownRow) => {
                  const reasonId = shared !== null ? sharedId : null;
                  if ("row" in shownRow) {
                    return <Row key={shownRow.row.id} row={shownRow.row} restoring={restoring} onRestore={onRestore}
                      reasonId={reasonId} />;
                  }
                  const [newest, ...earlier] = shownRow.rows;
                  const key = `${day.label}|${shownRow.session}`;
                  const open = opened.has(key);
                  return (
                    <React.Fragment key={newest.id}>
                      <Row row={newest} restoring={restoring} onRestore={onRestore} reasonId={reasonId} />
                      {open && earlier.map((row) => (
                        <Row key={row.id} row={row} restoring={restoring} onRestore={onRestore} reasonId={reasonId} />
                      ))}
                      <li className="border-0 border-l-2 border-dashed border-line-input pl-3">
                        <Button variant="ghost" aria-expanded={open} onClick={() => toggle(key)}>
                          {open ? `Hide ${tries(earlier.length)} from this session`
                            : `Show ${tries(earlier.length)} from this session`}
                        </Button>
                      </li>
                    </React.Fragment>
                  );
                })}
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
