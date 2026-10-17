import type { ReactNode } from "react";

import { Note } from "./fact-row";
import { ProblemCard } from "./problem-card";

/** A polled page's read: not read yet, never read, or read (`stale`: the last read failed, this is the one before). */
export type PageRead<T> = { status: "loading" } | { status: "failed" } | { status: "read"; value: T; stale: boolean };

/** A page's read before its first answer. */
export const NOT_READ = { status: "loading" } as const;

/** The read after `previous` given this read's value (null: Central did not answer): a failed read
 * keeps the last value, marked stale, and fails only a page that never read. */
export function nextRead<T>(previous: PageRead<T>, value: T | null): PageRead<T> {
  if (value !== null) return { status: "read", value, stale: false };
  return previous.status === "read" ? { ...previous, stale: true } : { status: "failed" };
}

export interface PolledPageProps<T> {
  read: PageRead<T>;
  /** What Central did not answer with, for a page that never read: "Central did not answer with …". */
  what: string;
  /** The line above a stale read: "… these are the lights as last read." */
  stale: string;
  /** Read again now. */
  onRetry: () => void;
  /** The page as read. */
  children: (value: T) => ReactNode;
}

/**
 * PolledPage: a page read every few seconds while shown (the console's one polled read). Loading
 * until the first answer; a page that never read is a problem card with Try again; a read that
 * failed after one succeeded keeps the last one under a line saying so.
 */
export function PolledPage<T>({ read, what, stale, onRetry, children }: PolledPageProps<T>) {
  if (read.status === "loading") return <Note>Loading…</Note>;
  if (read.status === "failed") {
    return (
      <ProblemCard scope="central" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what={what} doing={null} action={{ label: "Try again", onAction: onRetry }} />
    );
  }
  return (
    <>
      {read.stale && <Note>{stale}</Note>}
      {children(read.value)}
    </>
  );
}
