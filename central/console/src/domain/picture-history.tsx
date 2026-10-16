import type * as React from "react";
import { useCallback, useEffect, useState } from "react";

import { readPictureHistory, useHistoryPages } from "../historyApi.js";
import type { PictureEntryKind, PictureEntryView, PictureHistoryView } from "../historyApi.js";
import type {
  HardwareSettingView, PictureRestoreResult, SignalRange, SoftwarePictureView,
} from "../pictureApi.js";
import { Note } from "../patterns/fact-row";
import { changedSummary, type HistoryDay, HistoryList, type HistoryRow } from "../patterns/history-list";
import { ProblemCard } from "../patterns/problem-card";
import { dayLabel, timeOfDay } from "../timeWords.js";
import { hardwareWords, matchedWords, panelBlackWords, PATTERN_NAMES, type PictureSession } from "./frame-picture";

/** The read's shapes (historyApi.js, mirroring central/displays/views.py), for this module's users. */
export type { PictureEntryView, PictureHistoryView, SoftwarePictureView };

export interface PictureHistoryProps {
  /** The Display whose picture history this is (the Picture tab's `view.display_id`). */
  displayId: string;
  /** The tab's live session: Restore is one of its edits. */
  session: PictureSession;
  /** Called after an accepted Restore. */
  onRestored?: () => void;
}

const TITLE = "History";
const INTRO = "Every change to this display's picture, newest first. Restore puts one back in the editor; "
  + "press Done to keep it.";
const UNRECORDED = "This display's values changed outside Photo Wall since its last kept change. "
  + "The next Done records them.";
const EMPTY = "No changes yet. They appear here when you adjust this display, press Done or Copy.";
const OPEN_FIRST = "Open while the editor runs";
const AS_IT_HAS_IT = "As the display has it";
const SAME = "Same values as the change below";

/** Each kind of entry in the glossary's words (Done, Revert, Copy), never "snapshot" or "draft". */
export const WHAT: Record<PictureEntryKind, string> = {
  "kept-done": "Kept (Done)",
  "kept-copied": "Kept (Copy from another display)",
  "kept-unrecorded": "Changed outside Photo Wall",
  "sent-draft": "Tried, not kept",
  "sent-back": "Put back",
  "sent-kept": "Kept values sent again",
};
const NOT_KEPT = new Set<PictureEntryKind>(["sent-draft", "sent-back"]);

// The Picture tab's names for the signal range (frame-picture.tsx holds the control's own copy; these
// name stored values); the test patterns' are frame-picture.tsx's PATTERN_NAMES.
const RANGES: Record<SignalRange, string> = { auto: "Auto", full: "Full", limited: "Limited" };

/** A Restore's refusals in words; `stale_sequence` is tried once more by the session first. */
const REFUSALS: Record<string, string> = {
  session_ended: "The editor closed. Reopen the Picture tab to restore.",
  unknown_session: "The editor closed. Reopen the Picture tab to restore.",
  other_display: "That change was another display's. Use Copy.",
  unknown_entry: "That change is no longer in the history.",
  stale_sequence: "Another window changed this display's picture at the same moment.",
  unreachable: "Photo Wall's server did not answer.",
};

/** The display's settings by code, as the Picture tab's read names and ranges them. */
type Rows = Readonly<Record<string, HardwareSettingView>>;

/** A display setting's stored value in the tab's words; a code the tab does not list by its code. */
function hardwareValue(rows: Rows, code: string, value: number): string {
  const row = rows[code];
  return row?.offered ? hardwareWords(row, value) : String(value);
}

function hardwareLabel(rows: Rows, code: string): string {
  return `${rows[code]?.name ?? `Setting ${code}`} (display)`;
}

/**
 * Every value of one entry, in the Picture tab's order and units: each display setting set in
 * Photo Wall (by its picture-list name), the three Photo Wall values, the Display's light (decision
 * 0028), the signal range, and the test pattern when one was up.
 */
export function entryDetails(entry: PictureEntryView, rows: Rows): { label: string; value: string }[] {
  const { picture } = entry;
  const software = picture.software;
  return [
    ...Object.entries(picture.hardware).map(([code, value]) => ({
      label: hardwareLabel(rows, code), value: hardwareValue(rows, code, value),
    })),
    { label: "Contrast", value: `${software.contrast_percent} %` },
    { label: "Colour temperature", value: `${software.colour_temperature_kelvin} K` },
    { label: "Gamma", value: (software.gamma_tenths / 10).toFixed(1) },
    { label: "Matched light", value: matchedWords(picture.light.matched_lux) },
    { label: "Display black", value: panelBlackWords(picture.light.black) },
    { label: "Signal range", value: RANGES[picture.signal_range] },
    ...(entry.test_pattern === null ? [] : [{ label: "Test pattern", value: PATTERN_NAMES[entry.test_pattern] }]),
  ];
}

/** The pattern's one summary rule (`changedSummary`) with the Picture tab's words: a display setting
 * absent is "as the display has it", a test pattern absent Off. */
export function summary(details: readonly { label: string; value: string }[],
  below: readonly { label: string; value: string }[] | null): string {
  return changedSummary(details, below, {
    absent: (label) => (label === "Test pattern" ? "Off" : AS_IT_HAS_IT), same: SAME,
  });
}

/**
 * Any history's entries as the list's days, newest first: grouped by the house's day (`dayLabel`),
 * each row named by its entry id with its time with seconds (`timeOfDay`), the rest from `row`. The
 * Picture and Position tabs' one way to lay out a history.
 */
export function daysOf<E extends { entry_id: string; at: string }>(entries: readonly E[],
  row: (entry: E, index: number) => Omit<HistoryRow, "id" | "time">): HistoryDay[] {
  const days: { label: string; rows: HistoryRow[] }[] = [];
  entries.forEach((entry, index) => {
    const at = Date.parse(entry.at) / 1000;
    const label = dayLabel(at);
    const shown: HistoryRow = { id: entry.entry_id, time: timeOfDay(at), ...row(entry, index) };
    if (days.at(-1)?.label === label) days.at(-1)!.rows.push(shown);
    else days.push({ label, rows: [shown] });
  });
  return days;
}

/** The newest page's entries, then the older ones loaded, each once. */
export function shownEntries<E extends { entry_id: string }>(newest: readonly E[], older: readonly E[]): E[] {
  const seen = new Set(newest.map((entry) => entry.entry_id));
  return [...newest, ...older.filter((entry) => !seen.has(entry.entry_id))];
}

/** A History's problem: its read failed (with Retry), else the last Restore was refused (`words`). */
export function historyProblem({ readError, refused, what, words, onRetry }: {
  readError: string | null; refused: string | null; what: string; words: (code: string) => string; onRetry: () => void;
}): React.ReactNode {
  if (readError !== null) {
    return (
      <ProblemCard scope="central" variant="inline" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what={`Photo Wall's server did not answer with ${what}.`} doing={null}
        action={{ label: "Retry", onAction: onRetry }}
        details={<Note>{`Photo Wall's answer: ${readError}`}</Note>} />
    );
  }
  if (refused === null) return undefined;
  return (
    <ProblemCard scope="central" variant="inline" live verdict={{ severity: "notice", label: "Not restored", receipt: null }}
      what={words(refused)} doing={null} details={<Note>{`Photo Wall's answer: ${refused}`}</Note>} />
  );
}

/**
 * The entries as the list's days (`daysOf`), each row its kind in words, a summary against the
 * entry below it, every value, and Restore as `restore` says.
 */
export function historyDays(entries: readonly PictureEntryView[], rows: Rows,
  restore: HistoryRow["restore"]): HistoryDay[] {
  const details = entries.map((entry) => entryDetails(entry, rows));
  return daysOf(entries, (entry, index) => ({
    what: WHAT[entry.kind],
    notKept: NOT_KEPT.has(entry.kind),
    summary: summary(details[index], details[index + 1] ?? null),
    details: details[index],
    restore,
    session: entry.session_id,
  }));
}

/** What an accepted Restore did not bring back, in words (none: all of it came back). */
export function restoreNotes(result: PictureRestoreResult, rows: Rows): string[] {
  return [
    ...(result.hardware_skipped.length === 0 ? [] : [
      `Not restored: ${result.hardware_skipped.map((code) => rows[code]?.name ?? `setting ${code}`).join(", ")} `
        + "(this display does not offer them now).",
    ]),
  ];
}

/** Restore's state for every row: offered only while the session is live, else why not. */
export function restoreState(session: PictureSession): HistoryRow["restore"] {
  if (session.phase !== "idle") return { disabled: session.busy };
  return { disabled: true, reason: session.waitingFor ?? session.unavailable?.words ?? OPEN_FIRST };
}

export interface PictureHistoryListProps {
  /** The latest read (null: none yet). */
  history: PictureHistoryView | null;
  /** Older entries loaded by "Show older", after the read's. */
  older?: readonly PictureEntryView[];
  session: PictureSession;
  busy: boolean;
  /** The read failed: its code. */
  readError?: string | null;
  /** The last Restore: what did not come back, or its refusal's code. */
  outcome?: { notes: readonly string[] } | { error: string } | null;
  restoring: string | null;
  onRestore: (id: string) => void;
  onRetry: () => void;
  onOlder?: () => void;
}

/** The list as read: its words, the unrecorded change's Note, a Restore's notes or refusal, and the
 * read's problem. Plain props, so its stories need no server. */
export function PictureHistoryList({
  history, older = [], session, busy, readError = null, outcome = null, restoring, onRestore, onRetry, onOlder,
}: PictureHistoryListProps) {
  const rows: Rows = Object.fromEntries((session.view?.hardware ?? []).map((row) => [row.code, row]));
  const entries = shownEntries(history?.entries ?? [], older);
  const notes = [
    ...(history?.unrecorded_change ? [UNRECORDED] : []),
    ...(outcome !== null && "notes" in outcome ? outcome.notes : []),
  ];
  const problem = historyProblem({
    readError, refused: outcome !== null && "error" in outcome ? outcome.error : null, what: "this display's history",
    words: (code) => REFUSALS[code] ?? "Photo Wall refused the restore.", onRetry,
  });
  return (
    <HistoryList title={TITLE} intro={INTRO} notice={notes.length === 0 ? undefined : notes.join(" ")}
      days={historyDays(entries, rows, restoreState(session))} empty={EMPTY} busy={busy} problem={problem}
      restoring={restoring} onRestore={onRestore} onOlder={onOlder} />
  );
}

/**
 * The Picture tab's History (configuration snapshots; owner, 2026-10-13: "configuration snapshots"
 * that keep "a history of what has been done in the past", used to "View and restore"): every kept
 * and not-kept change of the Display's picture, newest first by day, read while the tab is shown
 * (historyApi.js `useHistoryPages`) and again after a Restore, Done or Revert. Restore loads an entry into the live
 * editor (pictureSession.js `restore`); the operator still presses Done. "Show older" reads the page
 * before the last entry shown; a later poll refreshes only the newest page.
 */
export function PictureHistory({ displayId, session, onRestored }: PictureHistoryProps) {
  const read = useCallback((page: { before?: string; limit: number }) => readPictureHistory(displayId, page) as
    Promise<{ ok: true; history: PictureHistoryView } | { ok: false; error: string }>, [displayId]);
  const pages = useHistoryPages(read);
  const { refresh } = pages;
  const [restoring, setRestoring] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<PictureHistoryListProps["outcome"]>(null);

  // Done (savedAt) and a session's end (Revert, a lapse) write entries: read again at once.
  const idle = session.phase === "idle";
  useEffect(() => { void refresh(); }, [refresh, session.savedAt, idle]);
  useEffect(() => { setOutcome(null); }, [session.savedAt]);

  const restore = async (id: string) => {
    setRestoring(id);
    setOutcome(null);
    const result = await session.restore(id);
    setRestoring(null);
    if (result.ok) {
      const rows = Object.fromEntries((session.view?.hardware ?? []).map((row) => [row.code, row]));
      setOutcome({ notes: restoreNotes(result.result, rows) });
      onRestored?.();
    } else if (result.error !== "busy") {
      setOutcome({ error: result.error });
    }
    void refresh();
  };

  return (
    <PictureHistoryList history={pages.history} older={pages.olderEntries} session={session}
      busy={pages.loading} readError={pages.error} outcome={outcome}
      restoring={restoring} onRestore={(id) => { void restore(id); }} onRetry={() => { void refresh(); }}
      onOlder={pages.more ? () => { void pages.older(); } : undefined} />
  );
}
