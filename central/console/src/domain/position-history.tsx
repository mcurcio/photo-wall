import { useCallback, useEffect, useState } from "react";

import { readPositionHistory, useHistoryPages } from "../historyApi.js";
import type { PositionEntryKind, PositionEntryView, PositionHistoryView } from "../generated/contract";
import { refusalWords } from "../liveAdjustment.js";
import { changedSummary, type HistoryDay, HistoryList, type HistoryRow } from "../patterns/history-list";
import { unitWords } from "../patterns/units";
import { pixelSize } from "../timeWords.js";
import { inkWords, lightDetails, type PaperProfileChoice, profileWords } from "./frame-lighting/model";
import type { Adjustment } from "./live-adjustment";
import { daysOf, historyProblem, shownEntries } from "./picture-history";

/** The read's shapes (historyApi.js, mirroring central/fleet/position_views.py), for this module's users. */
export type { PositionEntryView, PositionHistoryView };

export interface PositionHistoryProps {
  frameId: string;
  /** The tab's live adjustment: Restore is one of its edits. */
  adjustment: Adjustment;
  /** The room lights' names by id, for the lighting's rows (an id not here shows as itself). */
  roomNames?: Readonly<Record<string, string>>;
  /** The paper profiles on the list, for the paper's name. */
  profiles?: readonly PaperProfileChoice[];
}

const TITLE = "History";
const INTRO = "Every change to this Frame's position and lighting, newest first. Restore puts one back in "
  + "the editor; press Done to keep it.";
const UNRECORDED = "This Frame's position changed outside Photo Wall since its last kept change. "
  + "The next Done records it.";
const EMPTY = "No changes yet. They appear here when you adjust this Frame's position or press Done.";
const OPEN_FIRST = "Open while the editor runs";
const SAME = "Same values as the change below";
// The live adjustment's phases with a session the Pi shows: Restore is offered only then.
const LIVE = new Set(["sending", "shown"]);

/** Each kind of entry in the glossary's words (Done, Revert), never "snapshot", "draft" or "trial". */
export const WHAT: Record<PositionEntryKind, string> = {
  "kept-created": "Created",
  "kept-saved": "Kept (Done)",
  "kept-profile-changed": "Kept (screen size changed)",
  "kept-unrecorded": "Changed outside Photo Wall",
  "sent-draft": "Tried, not kept",
  "sent-back": "Put back",
};
const NOT_KEPT = new Set<PositionEntryKind>(["sent-draft", "sent-back"]);

const percent = (fraction: number) => `${(fraction * 100).toFixed(1)} %`;

/** The pattern's one summary rule (`changedSummary`) over two entries' details: what differs from
 * the entry below; a light the entry below had and this one has not is said by the lights' count. */
export function positionSummary(details: readonly { label: string; value: string }[],
  below: readonly { label: string; value: string }[] | null): string {
  return changedSummary(details, below, { absent: () => null, same: SAME });
}

/** Every value of an entry: the picture's centre and width as shares of the screen as it hangs; the
 * lighting (the mat's depth, each light's values, the paper), and the screen size it was made for.
 * `names`: the room lights' names by id; `profiles`: the paper profiles on the list. */
export function positionDetails(entry: PositionEntryView,
  names: Readonly<Record<string, string>> = {}, profiles: readonly PaperProfileChoice[] = []): { label: string; value: string }[] {
  const { lighting } = entry;
  const { width, height } = entry.made_for.profile_px;
  const { fit } = entry;
  return [
    { label: "Centre", value: `${percent(fit.centre_x ?? 0.5)} across, ${percent(fit.centre_y ?? 0.5)} down` },
    { label: "Width", value: `${percent(fit.width ?? 1)} of the screen` },
    { label: "Mat depth", value: unitWords(lighting.mat_depth_mm, "mm") },
    { label: "Lights", value: String(lighting.lights.length) },
    ...lighting.lights.flatMap((light, index) => lightDetails(light, index + 1, names)),
    { label: "Paper whiteness", value: lighting.paper.whiteness.toFixed(2) },
    { label: "Ink density", value: inkWords(lighting.paper.ink_density) },
    { label: "Paper profile", value: profileWords(lighting.paper.profile, profiles) },
    { label: "Made for", value: `${pixelSize(width, height)} px` },
  ];
}

/** Restore's state for one entry: refused for another screen size or display (Central's
 * `restore_refusal`, in words), else offered only while the live adjustment runs. */
export function restoreState(entry: PositionEntryView, adjustment: Adjustment): HistoryRow["restore"] {
  const { width, height } = entry.made_for.profile_px;
  switch (entry.restore_refusal) {
    case "made_for_other_profile":
      return { disabled: true, reason: `Made for another screen size (${pixelSize(width, height)} px).` };
    case "made_for_other_display":
      return { disabled: true, reason: refusalWords("made_for_other_display") };
    case "other_frame":
      return { disabled: true };
    default:
      return LIVE.has(adjustment.phase) ? { disabled: adjustment.busy } : { disabled: true, reason: OPEN_FIRST };
  }
}

/** The entries as the list's days (`daysOf`), each summary against the entry below it. */
export function positionDays(entries: readonly PositionEntryView[], adjustment: Adjustment,
  names: Readonly<Record<string, string>> = {},
  profiles: readonly PaperProfileChoice[] = []): HistoryDay[] {
  const details = entries.map((entry) => positionDetails(entry, names, profiles));
  return daysOf(entries, (entry, index) => ({
    what: WHAT[entry.kind],
    notKept: NOT_KEPT.has(entry.kind),
    summary: positionSummary(details[index], details[index + 1] ?? null),
    details: details[index],
    restore: restoreState(entry, adjustment),
    session: entry.session_id,
  }));
}

export interface PositionHistoryListProps {
  /** The latest read (null: none yet). */
  history: PositionHistoryView | null;
  /** Older entries loaded by "Show older", after the read's. */
  older?: readonly PositionEntryView[];
  adjustment: Adjustment;
  /** The room lights' names by id. */
  roomNames?: Readonly<Record<string, string>>;
  /** The paper profiles on the list. */
  profiles?: readonly PaperProfileChoice[];
  busy: boolean;
  /** The read failed: its code. */
  readError?: string | null;
  /** The last Restore's refusal code. */
  refused?: string | null;
  restoring: string | null;
  onRestore: (id: string) => void;
  onRetry: () => void;
  onOlder?: () => void;
}

/** The list as read: its words, the unrecorded change's Note, a Restore's refusal and the read's
 * problem. Plain props, so its stories need no server. */
export function PositionHistoryList({
  history, older = [], adjustment, roomNames = {}, profiles = [], busy, readError = null, refused = null, restoring,
  onRestore, onRetry, onOlder,
}: PositionHistoryListProps) {
  const entries = shownEntries(history?.entries ?? [], older);
  return (
    <HistoryList title={TITLE} intro={INTRO} notice={history?.unrecorded_change ? UNRECORDED : undefined}
      days={positionDays(entries, adjustment, roomNames, profiles)} empty={EMPTY} busy={busy}
      problem={historyProblem({ readError, refused, what: "this Frame's history", words: refusalWords, onRetry })}
      restoring={restoring} onRestore={onRestore} onOlder={onOlder} />
  );
}

/**
 * The Position tab's History (configuration snapshots; owner, 2026-10-13: "configuration snapshots"
 * that keep "a history of what has been done in the past", used to "View and restore"): every kept
 * and not-kept Position of the Frame (its centre, width and lighting), newest first by
 * day, read while the tab is shown (historyApi.js `useHistoryPages`) and again after a Restore, Done
 * or Revert. Restore replaces the operator's draft with an entry (liveAdjustment.js `restore`); the
 * live session shows it, Done keeps it and Revert returns to the kept Position. An entry made for
 * another screen size or display cannot be restored, and says so.
 */
export function PositionHistory({ frameId, adjustment, roomNames, profiles }: PositionHistoryProps) {
  const read = useCallback((page: { before?: string; limit: number }) => readPositionHistory(frameId, page) as
    Promise<{ ok: true; history: PositionHistoryView } | { ok: false; error: string }>, [frameId]);
  const pages = useHistoryPages(read);
  const { refresh } = pages;
  const [restoring, setRestoring] = useState<string | null>(null);
  const [refused, setRefused] = useState<string | null>(null);

  // Done (savedAt) and a session's end (Revert, a lapse) write entries: read again at once.
  const live = LIVE.has(adjustment.phase);
  useEffect(() => { void refresh(); }, [refresh, adjustment.savedAt, live]);

  const restore = async (id: string) => {
    setRestoring(id);
    setRefused(null);
    const result = await adjustment.restore(id);
    setRestoring(null);
    if (!result.ok && result.error !== "busy") setRefused(result.error);
    void refresh();
  };

  return (
    <PositionHistoryList history={pages.history} older={pages.olderEntries} adjustment={adjustment}
      roomNames={roomNames} profiles={profiles}
      busy={pages.loading} readError={pages.error} refused={refused} restoring={restoring}
      onRestore={(id) => { void restore(id); }} onRetry={() => { void refresh(); }}
      onOlder={pages.more ? () => { void pages.older(); } : undefined} />
  );
}
