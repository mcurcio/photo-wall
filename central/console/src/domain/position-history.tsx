import { useCallback, useEffect, useState } from "react";

import { readPositionHistory, useHistoryPages } from "../historyApi.js";
import type { PositionEntryKind, PositionEntryView, PositionHistoryView } from "../historyApi.js";
import { refusalWords } from "../liveAdjustment.js";
import { changedSummary, type HistoryDay, HistoryList, type HistoryRow } from "../patterns/history-list";
import { pixelSize } from "../timeWords.js";
import { degreesWords, depthWords, fromWords, strengthWords } from "./frame-mat";
import type { PixelSize } from "./frame-position";
import type { Adjustment } from "./live-adjustment";
import { daysOf, historyProblem, shownEntries } from "./picture-history";

/** The read's shapes (historyApi.js, mirroring central/fleet/position_views.py), for this module's users. */
export type { PositionEntryView, PositionHistoryView };

export interface PositionHistoryProps {
  frameId: string;
  /** The tab's live adjustment: Restore is one of its edits. */
  adjustment: Adjustment;
  /** The output's size in pixels (null: not reported), for each corner's pixels. */
  size: PixelSize | null;
}

const TITLE = "History";
const INTRO = "Every change to this Frame's position, mat and lights, newest first. Restore puts one back in "
  + "the editor; press Done to keep it.";
const UNRECORDED = "This Frame's position changed outside Photo Wall since its last kept change. "
  + "The next Done records it.";
const EMPTY = "No changes yet. They appear here when you adjust this Frame's position or press Done.";
const OPEN_FIRST = "Open while the editor runs";
const SAME = "Same values as the change below";
const CORNERS = ["Top-left", "Top-right", "Bottom-right", "Bottom-left"] as const;
// The live adjustment's phases with a session the Pi shows: Restore is offered only then.
const LIVE = new Set(["sending", "shown"]);

/** Each kind of entry in the glossary's words (Done, Revert), never "snapshot", "draft" or "trial". */
export const WHAT: Record<PositionEntryKind, string> = {
  "kept-saved": "Kept (Done)",
  "kept-profile-changed": "Kept (screen size changed)",
  "kept-unrecorded": "Changed outside Photo Wall",
  "sent-draft": "Tried, not kept",
  "sent-back": "Put back",
};
const NOT_KEPT = new Set<PositionEntryKind>(["sent-draft", "sent-back"]);

const percent = (fraction: number) => `${(fraction * 100).toFixed(1)} %`;
const FITS = { cover: "Cover", corners: "Corners" } as const;

/** The pattern's one summary rule (`changedSummary`) over two entries' details: what differs from
 * the entry below; a light the entry below had and this one has not is said by the lights' count. */
export function positionSummary(details: readonly { label: string; value: string }[],
  below: readonly { label: string; value: string }[] | null): string {
  return changedSummary(details, below, { absent: () => null, same: SAME });
}

/** Every value of an entry: each corner as a share of the output (with its pixels when the output's
 * size is known), the rotation, the mat's depth, each light's values, and the screen size it was made for. */
export function positionDetails(entry: PositionEntryView, size: PixelSize | null): { label: string; value: string }[] {
  const { width, height } = entry.made_for.profile_px;
  return [
    { label: "Fit", value: FITS[entry.fit.mode] },
    ...entry.fit.corners.map(([x, y], index) => ({
      label: CORNERS[index] ?? `Corner ${index + 1}`,
      value: `${percent(x)} across, ${percent(y)} down`
        + (size === null ? "" : ` (${Math.round(x * size.width)}, ${Math.round(y * size.height)} px)`),
    })),
    { label: "Rotation", value: degreesWords(entry.fit.rotation) },
    { label: "Mat depth", value: depthWords(entry.mat.depth_mm) },
    { label: "Lights", value: String(entry.mat.lights.length) },
    ...entry.mat.lights.flatMap((light, index) => {
      const name = `Light ${index + 1}`;
      return [
        { label: `${name} comes from`, value: fromWords(light.from_degrees) },
        { label: `${name} steepness`, value: degreesWords(light.steep_degrees) },
        { label: `${name} softness`, value: degreesWords(light.softness_degrees) },
        { label: `${name} strength`, value: strengthWords(light.strength_tenths) },
        { label: `${name} colour`, value: `${light.colour_kelvin} K` },
      ];
    }),
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
  size: PixelSize | null): HistoryDay[] {
  const details = entries.map((entry) => positionDetails(entry, size));
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
  size: PixelSize | null;
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
  history, older = [], adjustment, size, busy, readError = null, refused = null, restoring, onRestore, onRetry, onOlder,
}: PositionHistoryListProps) {
  const entries = shownEntries(history?.entries ?? [], older);
  return (
    <HistoryList title={TITLE} intro={INTRO} notice={history?.unrecorded_change ? UNRECORDED : undefined}
      days={positionDays(entries, adjustment, size)} empty={EMPTY} busy={busy}
      problem={historyProblem({ readError, refused, what: "this Frame's history", words: refusalWords, onRetry })}
      restoring={restoring} onRestore={onRestore} onOlder={onOlder} />
  );
}

/**
 * The Position tab's History (configuration snapshots; owner, 2026-10-13: "configuration snapshots"
 * that keep "a history of what has been done in the past", used to "View and restore"): every kept
 * and not-kept Position of the Frame (its fit, corners, rotation, mat and lights), newest first by
 * day, read while the tab is shown (historyApi.js `useHistoryPages`) and again after a Restore, Done
 * or Revert. Restore replaces the operator's draft with an entry (liveAdjustment.js `restore`); the
 * live session shows it, Done keeps it and Revert returns to the kept Position. An entry made for
 * another screen size or display cannot be restored, and says so.
 */
export function PositionHistory({ frameId, adjustment, size }: PositionHistoryProps) {
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
    <PositionHistoryList history={pages.history} older={pages.olderEntries} adjustment={adjustment} size={size}
      busy={pages.loading} readError={pages.error} refused={refused} restoring={restoring}
      onRestore={(id) => { void restore(id); }} onRetry={() => { void refresh(); }}
      onOlder={pages.more ? () => { void pages.older(); } : undefined} />
  );
}
