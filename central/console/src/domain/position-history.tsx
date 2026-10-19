import { useCallback, useEffect, useState } from "react";

import { readPositionHistory, useHistoryPages } from "../historyApi.js";
import type { PositionEntryKind, PositionEntryView, PositionHistoryView } from "../generated/contract";
import { refusalWords } from "../liveAdjustment.js";
import { changedSummary, type HistoryDay, HistoryList, type HistoryRow } from "../patterns/history-list";
import { unitWords } from "../patterns/units";
import { countText } from "../timeWords.js";
import { inkWords, lightDetails, type PaperProfileChoice, profileWords } from "./frame-lighting/model";
import type { PixelSize } from "./frame-position";
import { boxPixels, screenOf, shapeWords } from "./position-box";
import { HANGS } from "./turn-words";
import type { Adjustment } from "./live-adjustment";
import { daysOf, historyProblem, shownEntries } from "./picture-history";

/** The read's shapes (historyApi.js, mirroring central/fleet/position_views.py), for this module's users. */
export type { PositionEntryView, PositionHistoryView };

export interface PositionHistoryProps {
  frameId: string;
  /** The tab's live adjustment: Restore is one of its edits. */
  adjustment: Adjustment;
  /** The Output's mode as built, as the Pi reported it (null: none reported): each entry's pixels are
   * of this screen as it hung then, as the editor says them. */
  size: PixelSize | null;
  /** The Frame page's Hardware tab, where the screen is turned back to restore an entry made for another turn. */
  hardwareHref: string;
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
// The live adjustment's phases with a session the Pi shows: Restore is offered only then.
const LIVE = new Set(["sending", "shown"]);

/** Each kind of entry in the glossary's words (Done, Revert), never "snapshot", "draft" or "trial". */
export const WHAT: Record<PositionEntryKind, string> = {
  "kept-created": "Created",
  "kept-saved": "Kept (Done)",
  "kept-profile-changed": "Kept (screen size changed)",
  "kept-turn-changed": "Kept (screen turned)",
  "kept-unrecorded": "Changed outside Photo Wall",
  "sent-draft": "Tried, not kept",
  "sent-back": "Put back",
};
const NOT_KEPT = new Set<PositionEntryKind>(["sent-draft", "sent-back"]);

// The values a row's summary names, in the editor's words: the box's left and top edges and its size
// (in pixels of the screen when its size is known, else as shares of it), how the screen hung, and
// the lighting's mat and lights (the rest of the lighting under Details).
const POSITION = ["Left and top", "Size"];
const FIRST_LINE = [...POSITION, "Screen", "Mat depth", "Lights"];
// Under Details only: the same box as shares of the screen (they change with the pixels).
const SHARES = new Set(["Left and top (share of the screen)", "Size (share of the screen)"]);

const percent = (fraction: number) => `${(fraction * 100).toFixed(1)} %`;
type Detail = { label: string; value: string };
const line = (details: readonly Detail[], labels: readonly string[]) =>
  labels.flatMap((label) => details.filter((detail) => detail.label === label))
    .map(({ label, value }) => `${label} ${value}`).join(" · ");

/**
 * A row's summary against the row below it (`below`; null: the oldest shown), in the editor's words:
 * a turn says the screen turned and the position it kept; the oldest row its position, how the
 * screen hung and its mat and lights; any other row what changed (the pattern's one rule,
 * `changedSummary`; a light the row below had and this one has not is said by the lights' count),
 * or "No change" with its position.
 */
export function positionSummary(kind: PositionEntryKind, details: readonly Detail[], below: readonly Detail[] | null,
  turn: PositionEntryView["made_for"]["turn"]): string {
  if (kind === "kept-turn-changed") {
    return `Screen turned: it hangs ${HANGS[turn]}. Position kept: ${line(details, POSITION)}`;
  }
  if (below === null) return line(details, FIRST_LINE);
  const shown = (rows: readonly Detail[]) => rows.filter((detail) => !SHARES.has(detail.label));
  return changedSummary(shown(details), shown(below), {
    absent: () => null, same: `No change: ${line(details, POSITION)}`,
  });
}

/** Every value of an entry: the box's left and top edges and its size in pixels of the screen as it
 * hung (the Pi's reported mode `size`, turned by the entry's turn; position-box.ts), or as shares of
 * the screen when no size is known, how it hung, the lighting (the mat's depth, each light's values,
 * the paper), then (with a known size) the box as shares of the screen. `names`: the room lights' names by id; `profiles`: the paper profiles on the list. */
export function positionDetails(entry: PositionEntryView, size: PixelSize | null = null,
  names: Readonly<Record<string, string>> = {}, profiles: readonly PaperProfileChoice[] = []): Detail[] {
  const { lighting } = entry;
  const box = { left: entry.fit.left ?? 0, top: entry.fit.top ?? 0, width: entry.fit.width ?? 1, height: entry.fit.height ?? 1 };
  const screen = screenOf(size === null ? null : [size.width, size.height], entry.made_for.turn);
  const shares = [
    { label: "Left and top", value: `${percent(box.left)} across, ${percent(box.top)} down` },
    { label: "Size", value: `${percent(box.width)} × ${percent(box.height)} of the screen` },
  ];
  const placed = screen === null ? null : boxPixels(box, screen);
  return [
    ...(placed === null ? shares : [
      { label: "Left and top", value: `${countText(placed.x)} across, ${countText(placed.y)} down` },
      { label: "Size", value: shapeWords(placed) },
    ]),
    { label: "Screen", value: `hung ${HANGS[entry.made_for.turn]}` },
    { label: "Mat depth", value: unitWords(lighting.mat_depth_mm, "mm") },
    { label: "Lights", value: String(lighting.lights.length) },
    ...lighting.lights.flatMap((light, index) => lightDetails(light, index + 1, names)),
    { label: "Paper whiteness", value: lighting.paper.whiteness.toFixed(2) },
    { label: "Ink density", value: inkWords(lighting.paper.ink_density) },
    { label: "Paper profile", value: profileWords(lighting.paper.profile, profiles) },
    ...(placed === null ? [] : shares.map(({ label, value }) => ({ label: `${label} (share of the screen)`, value }))),
  ];
}

/** Restore's state for one entry: refused for a screen hung another way (with the way to turn it
 * back) or another display (Central's `restore_refusal`, in words), else offered only
 * while the live adjustment runs. */
export function restoreState(entry: PositionEntryView, adjustment: Adjustment, hardwareHref = ""): HistoryRow["restore"] {
  switch (entry.restore_refusal) {
    case "made_for_other_turn":
      return {
        disabled: true,
        reason: `Made when the screen hung ${HANGS[entry.made_for.turn]}. Turn the screen back on the Hardware tab to restore it.`,
        link: { text: "Open the Hardware tab", href: hardwareHref },
      };
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
  { size = null, hardwareHref = "", names = {}, profiles = [] }: {
    size?: PixelSize | null; hardwareHref?: string;
    names?: Readonly<Record<string, string>>; profiles?: readonly PaperProfileChoice[];
  } = {}): HistoryDay[] {
  const details = entries.map((entry) => positionDetails(entry, size, names, profiles));
  return daysOf(entries, (entry, index) => ({
    what: WHAT[entry.kind],
    notKept: NOT_KEPT.has(entry.kind),
    summary: positionSummary(entry.kind, details[index], details[index + 1] ?? null, entry.made_for.turn),
    details: details[index],
    restore: restoreState(entry, adjustment, hardwareHref),
    session: entry.session_id,
  }));
}

export interface PositionHistoryListProps {
  /** The latest read (null: none yet). */
  history: PositionHistoryView | null;
  /** Older entries loaded by "Show older", after the read's. */
  older?: readonly PositionEntryView[];
  adjustment: Adjustment;
  /** The Output's mode as built (PositionHistoryProps `size`). */
  size?: PixelSize | null;
  /** The Frame page's Hardware tab (PositionHistoryProps `hardwareHref`). */
  hardwareHref?: string;
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
  history, older = [], adjustment, size = null, hardwareHref = "", roomNames = {}, profiles = [], busy, readError = null,
  refused = null, restoring, onRestore, onRetry, onOlder,
}: PositionHistoryListProps) {
  const entries = shownEntries(history?.entries ?? [], older);
  return (
    <HistoryList title={TITLE} intro={INTRO} notice={history?.unrecorded_change ? UNRECORDED : undefined}
      days={positionDays(entries, adjustment, { size, hardwareHref, names: roomNames, profiles })} empty={EMPTY} busy={busy}
      problem={historyProblem({ readError, refused, what: "this Frame's history", words: refusalWords, onRetry })}
      restoring={restoring} onRestore={onRestore} onOlder={onOlder} />
  );
}

/**
 * The Position tab's History (configuration snapshots; owner, 2026-10-13: "configuration snapshots"
 * that keep "a history of what has been done in the past", used to "View and restore"): every kept
 * and not-kept Position of the Frame (its box and lighting), newest first by
 * day, read while the tab is shown (historyApi.js `useHistoryPages`) and again after a Restore, Done
 * or Revert. Restore replaces the operator's draft with an entry (liveAdjustment.js `restore`); the
 * live session shows it, Done keeps it and Revert returns to the kept Position. Each row says its
 * Position in pixels of its screen (position-box.ts). An entry made for another screen size, a
 * screen hung another way or another display cannot be restored, and says so (a turn with the way to
 * the Hardware tab).
 */
export function PositionHistory({ frameId, adjustment, size, hardwareHref, roomNames, profiles }: PositionHistoryProps) {
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
      size={size} hardwareHref={hardwareHref} roomNames={roomNames} profiles={profiles}
      busy={pages.loading} readError={pages.error} refused={refused} restoring={restoring}
      onRestore={(id) => { void restore(id); }} onRetry={() => { void refresh(); }}
      onOlder={pages.more ? () => { void pages.older(); } : undefined} />
  );
}
