import { useCallback, useEffect, useState } from "react";

import { readPictureHistory } from "../historyApi.js";
import type { PictureEntryKind, PictureEntryView, PictureHistoryView } from "../historyApi.js";
import type {
  HardwareSettingView, PictureRestoreResult, SignalRange, SoftwarePictureView, TestPattern,
} from "../pictureApi.js";
import { Note } from "../patterns/fact-row";
import { type HistoryDay, HistoryList, type HistoryRow } from "../patterns/history-list";
import { ProblemCard } from "../patterns/problem-card";
import { usePolledRead } from "../polledRead.js";
import { dayLabel, timeOfDay } from "../timeWords.js";
import { blackFloorWords, hardwareWords, type PictureSession } from "./frame-picture";

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

/** How often the list is read while the tab is shown. */
const CADENCE_MS = 2000;
/** Entries per read: the newest page, and each "Show older". */
const PAGE = 50;

const TITLE = "History";
const INTRO = "Every change to this display's picture, newest first. Restore puts one back in the editor; "
  + "press Done to keep it.";
const UNRECORDED = "This display's values changed outside Photo Wall since its last kept change. "
  + "The next Done records them.";
const EMPTY = "No changes yet. They appear here when you adjust this display, press Done or Copy.";
const OPEN_FIRST = "Open while the editor runs";
const AS_IT_HAS_IT = "As the display has it";
const SAME = "Same values as the change below";
const OLDER_RECIPE = "made with an older recipe";

/** Each kind of entry in the glossary's words (Done, Revert, Copy), never "snapshot" or "draft". */
export const WHAT: Record<PictureEntryKind, string> = {
  "kept-done": "Kept (Done)",
  "kept-copied": "Kept (Copy from another display)",
  "kept-unrecorded": "Changed outside Photo Wall",
  "sent-draft": "Tried, not kept",
  "sent-back": "Put back",
  "sent-kept": "Kept values shown again",
};
const NOT_KEPT = new Set<PictureEntryKind>(["sent-draft", "sent-back"]);

// The Picture tab's names for the signal range and the test patterns (frame-picture.tsx holds the
// controls' own copies; these name stored values).
const RANGES: Record<SignalRange, string> = { auto: "Auto", full: "Full", limited: "Limited" };
const PATTERNS: Record<TestPattern, string> = { white: "White", "grey-ramp": "Grey ramp", "black-level": "Black level" };

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
 * Photo Wall (by its picture-list name), the eight Photo Wall values, the LUT (with "made with an
 * older recipe" when `recipe`, the newest kept entry's, differs), the signal range, and the test
 * pattern when one was up.
 */
export function entryDetails(entry: PictureEntryView, rows: Rows, recipe: string | null): { label: string; value: string }[] {
  const { picture } = entry;
  const software = picture.software;
  const older = recipe !== null && picture.lut_recipe_id !== null && picture.lut_recipe_id !== recipe;
  const lut = software.lut === null ? "None" : older ? `${software.lut.name} (${OLDER_RECIPE})` : software.lut.name;
  return [
    ...Object.entries(picture.hardware).map(([code, value]) => ({
      label: hardwareLabel(rows, code), value: hardwareValue(rows, code, value),
    })),
    { label: "Brightness", value: `${software.brightness_percent} %` },
    { label: "Contrast", value: `${software.contrast_percent} %` },
    { label: "Colour temperature", value: `${software.colour_temperature_kelvin} K` },
    { label: "Gamma", value: (software.gamma_tenths / 10).toFixed(1) },
    { label: "Saturation", value: `${software.saturation_percent} %` },
    { label: "Soft highlights", value: `${software.highlight_rolloff_percent} %` },
    { label: "Black floor", value: blackFloorWords(software.black_floor_tenths) },
    { label: "LUT", value: lut },
    { label: "LUT amount", value: `${software.lut_amount_percent} %` },
    { label: "Signal range", value: RANGES[picture.signal_range] },
    ...(entry.test_pattern === null ? [] : [{ label: "Test pattern", value: PATTERNS[entry.test_pattern] }]),
  ];
}

/** The values of `details` that differ from `below`'s (the entry under it in the list), in one
 * line; all of them for the oldest shown. A display setting absent is "as the display has it", a
 * test pattern absent Off. */
export function summary(details: readonly { label: string; value: string }[],
  below: readonly { label: string; value: string }[] | null): string {
  const words = (pairs: readonly { label: string; value: string }[]) => pairs.map(({ label, value }) => `${label} ${value}`)
    .join(" · ");
  if (below === null) return words(details);
  const was = new Map(below.map(({ label, value }) => [label, value]));
  const now = new Map(details.map(({ label, value }) => [label, value]));
  const gone = below.filter(({ label }) => !now.has(label))
    .map(({ label }) => ({ label, value: label === "Test pattern" ? "Off" : AS_IT_HAS_IT }));
  const changed = [...details.filter(({ label, value }) => was.get(label) !== value), ...gone];
  return changed.length === 0 ? SAME : words(changed);
}

const epoch = (at: string) => Date.parse(at) / 1000;

/**
 * The entries as the list's days: grouped by the house's day (`dayLabel`), each row its time with
 * seconds (`timeOfDay`), its kind in words, a summary against the entry below it, every value, and
 * Restore as `restore` says.
 */
export function historyDays(entries: readonly PictureEntryView[], rows: Rows,
  restore: HistoryRow["restore"]): HistoryDay[] {
  const recipe = entries.find((entry) => entry.kind.startsWith("kept-") && entry.picture.lut_recipe_id !== null)
    ?.picture.lut_recipe_id ?? null;
  const details = entries.map((entry) => entryDetails(entry, rows, recipe));
  const days: { label: string; rows: HistoryRow[] }[] = [];
  entries.forEach((entry, index) => {
    const at = epoch(entry.at);
    const label = dayLabel(at);
    const row: HistoryRow = {
      id: entry.entry_id,
      time: timeOfDay(at),
      what: WHAT[entry.kind],
      notKept: NOT_KEPT.has(entry.kind),
      summary: summary(details[index], details[index + 1] ?? null),
      details: details[index],
      restore,
    };
    if (days.at(-1)?.label === label) days.at(-1)!.rows.push(row);
    else days.push({ label, rows: [row] });
  });
  return days;
}

/** What an accepted Restore did not bring back, in words (none: all of it came back). */
export function restoreNotes(result: PictureRestoreResult, rows: Rows): string[] {
  return [
    ...(result.hardware_skipped.length === 0 ? [] : [
      `Not restored: ${result.hardware_skipped.map((code) => rows[code]?.name ?? `setting ${code}`).join(", ")} `
        + "(this display does not offer them now).",
    ]),
    ...(result.lut_skipped ? ["The LUT is no longer on the list: the current one stays."] : []),
    ...(result.lut_recipe_changed ? ["The LUT is made with today's recipe."] : []),
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
  const newest = history?.entries ?? [];
  const seen = new Set(newest.map((entry) => entry.entry_id));
  const entries = [...newest, ...older.filter((entry) => !seen.has(entry.entry_id))];
  const notes = [
    ...(history?.unrecorded_change ? [UNRECORDED] : []),
    ...(outcome !== null && "notes" in outcome ? outcome.notes : []),
  ];
  const problem = readError !== null ? (
    <ProblemCard scope="central" variant="inline" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
      what="Photo Wall's server did not answer with this display's history." doing={null}
      action={{ label: "Retry", onAction: onRetry }}
      details={<Note>{`Photo Wall's answer: ${readError}`}</Note>} />
  ) : outcome !== null && "error" in outcome ? (
    <ProblemCard scope="central" variant="inline" live verdict={{ severity: "notice", label: "Not restored", receipt: null }}
      what={REFUSALS[outcome.error] ?? "Photo Wall refused the restore."} doing={null}
      details={<Note>{`Photo Wall's answer: ${outcome.error}`}</Note>} />
  ) : undefined;
  return (
    <HistoryList title={TITLE} intro={INTRO} notice={notes.length === 0 ? undefined : notes.join(" ")}
      days={historyDays(entries, rows, restoreState(session))} empty={EMPTY} busy={busy} problem={problem}
      restoring={restoring} onRestore={onRestore} onOlder={onOlder} />
  );
}

type Read = { history: PictureHistoryView | null; error: string | null; full: boolean } | null;

/**
 * The Picture tab's History (configuration snapshots; owner, 2026-10-13: "configuration snapshots"
 * that keep "a history of what has been done in the past", used to "View and restore"): every kept
 * and not-kept change of the Display's picture, newest first by day, read every CADENCE_MS while the
 * tab is shown and again after a Restore, Done or Revert. Restore loads an entry into the live
 * editor (pictureSession.js `restore`); the operator still presses Done. "Show older" reads the page
 * before the last entry shown; a later poll refreshes only the newest page.
 */
export function PictureHistory({ displayId, session, onRestored }: PictureHistoryProps) {
  const load = useCallback(async (previous: Read): Promise<Read> => {
    const result = await readPictureHistory(displayId, { limit: PAGE }) as
      { ok: true; history: PictureHistoryView } | { ok: false; error: string };
    return result.ok ? { history: result.history, error: null, full: result.history.entries.length === PAGE }
      : { history: previous?.history ?? null, error: result.error, full: previous?.full ?? false };
  }, [displayId]);
  const { value: read, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, initial: null as Read });
  const [older, setOlder] = useState<{ entries: readonly PictureEntryView[]; full: boolean } | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
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

  const entries = [...(read?.history?.entries ?? []), ...(older?.entries ?? [])];
  const more = older === null ? read?.full === true : older.full;
  const showOlder = async () => {
    const last = entries.at(-1);
    if (last === undefined || loadingOlder) return;
    setLoadingOlder(true);
    const result = await readPictureHistory(displayId, { before: last.at, limit: PAGE }) as
      { ok: true; history: PictureHistoryView } | { ok: false; error: string };
    setLoadingOlder(false);
    if (result.ok) {
      setOlder({ entries: [...(older?.entries ?? []), ...result.history.entries], full: result.history.entries.length === PAGE });
    }
  };

  return (
    <PictureHistoryList history={read?.history ?? null} older={older?.entries ?? []} session={session}
      busy={read === null || loadingOlder} readError={read?.error ?? null} outcome={outcome}
      restoring={restoring} onRestore={(id) => { void restore(id); }} onRetry={() => { void refresh(); }}
      onOlder={more ? () => { void showOlder(); } : undefined} />
  );
}
