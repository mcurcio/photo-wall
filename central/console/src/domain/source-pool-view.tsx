import { EmptyState } from "../patterns/entity-page";
import { Note } from "../patterns/fact-row";
import { HealthBadge } from "../patterns/health-badge";
import { MediaGrid } from "../patterns/media-grid";
import { Button } from "../ui/button";
import { Inline } from "../ui/stack";
import { checkWords, counted, FAILED_WORDS, INCOMPLETE_WORDS, PHOTOS_KEEP_PLAYING } from "../sourceWords.js";
import { captureDay, countText } from "../timeWords.js";

/** A served check (central/photo_library/model.py `CheckState`). */
export type CheckState =
  | { state: "never_checked" }
  | { state: "checking"; seen_so_far: number; started_at: number; last_complete_at: number | null }
  | { state: "complete"; at: number }
  | { state: "incomplete"; reason: "over_1000_at_one_instant"; last_complete_at: number | null }
  | { state: "failed"; problem: "connection" | "chosen_item_gone"; last_complete_at: number | null };

/** One photo of a pool page (central/photo_library/views.py `PoolItem`). */
export interface PoolItem {
  asset_id: string;
  kind: "image" | "video";
  taken_at: number;
  width: number;
  height: number;
  thumbnail: string;
}

/** A pool's counts and first page (central/photo_library/views.py `SourcePoolView`). */
export interface SourcePoolViewData {
  check: CheckState;
  in_pool: { photos: number; videos: number };
  waiting: number;
  left_out: { reason: "unreadable_answer" | "over_item_limits"; count: number }[];
  immich_up_to: number | null;
  items: PoolItem[];
  next: string | null;
  read_at: number;
}

const LEFT_OUT_WORDS = {
  unreadable_answer: (count: number) => `${countText(count)} could not be read`,
  over_item_limits: (count: number) => `${countText(count)} ${count === 1 ? "is" : "are"} too large`,
};

/**
 * The pool's line: what is in it, against its check (run ledger K2). The check's time is the
 * badge's alone ("Checked Sun 03:39"), so the page shows one time, not two.
 */
export function poolWords(view: SourcePoolViewData): string {
  const { check, in_pool: inPool } = view;
  const kinds = `${counted(inPool.photos, "photo")} · ${counted(inPool.videos, "video")}`;
  switch (check.state) {
    case "checking": {
      const upTo = view.immich_up_to === null ? "" : ` · up to ${countText(view.immich_up_to)} in Immich`;
      return `${countText(inPool.photos + inPool.videos)} in the pool so far${upTo}`;
    }
    case "complete":
      return kinds;
    case "incomplete":
      return `${kinds} · ${INCOMPLETE_WORDS}`;
    case "failed":
      return `${kinds} · ${FAILED_WORDS[check.problem]}. ${PHOTOS_KEEP_PLAYING}`;
    default:
      return "Not checked yet: photos appear here as Immich is read";
  }
}

/** One choice the query makes, as an empty pool names it, with how to clear it. */
export interface ActiveFilter { label: string; onClear: () => void }

export interface SourcePoolViewProps {
  /** The first page's answer; null until it is read. */
  view: SourcePoolViewData | null;
  /** Every page read so far, in order. */
  pages: readonly (readonly PoolItem[])[];
  /** More can be read after the last page ("Show more"). */
  hasMore: boolean;
  onLoadMore: () => void;
  loadingMore?: boolean;
  /** The last read failed (its code): the pool shown is the last one read. */
  error?: string | null;
  /** The query's choices, named by an empty pool, each with a Clear. */
  filters?: readonly ActiveFilter[];
}

/**
 * SourcePoolView: a draft's or a saved Source's pool as it fills (design-language T4's preview,
 * run ledger K2): its check's state and time, its line ("12,400 in the pool so far · up to 41,203
 * in Immich"), the photos still waiting or left out and why, and its thumbnails newest first, each
 * at its own ratio, with "Show more". A complete check that found nothing names the query's
 * choices, each with a Clear.
 */
export function SourcePoolView({ view, pages, hasMore, onLoadMore, loadingMore = false, error = null, filters = [] }: SourcePoolViewProps) {
  if (view === null) {
    return <Note live>{error === null ? "Reading the pool…" : "Photo Wall could not read the pool just now; it keeps trying."}</Note>;
  }
  const seen = new Set<string>();
  const items = pages.flat().filter((item) => (seen.has(item.asset_id) ? false : (seen.add(item.asset_id), true)));
  const verdict = checkWords(view.check, view.read_at);
  const empty = view.in_pool.photos + view.in_pool.videos === 0;
  return (
    <div>
      <HealthBadge verdict={{ severity: verdict.severity, label: verdict.label, receipt: null }} />
      <Note live>{poolWords(view)}</Note>
      {view.waiting > 0 && <Note>{`Immich is still reading ${counted(view.waiting, "photo")}`}</Note>}
      {view.left_out.filter((row) => row.count > 0).map((row) => (
        <Note key={row.reason}>{LEFT_OUT_WORDS[row.reason](row.count)}</Note>
      ))}
      {error !== null && <Note>Photo Wall could not read the pool just now; it keeps trying.</Note>}
      {empty ? (
        view.check.state === "complete" ? (
          <>
            <EmptyState>
              {filters.length === 0 ? "No photos match." : `No photos match all of these: ${filters.map((f) => f.label).join(" · ")}.`}
            </EmptyState>
            {filters.length > 0 && (
              <Inline label="Clear a choice">
                {filters.map((filter) => (
                  <Button key={filter.label} variant="ghost" onClick={filter.onClear}>{`Clear ${filter.label}`}</Button>
                ))}
              </Inline>
            )}
          </>
        ) : null
      ) : (
        <MediaGrid
          label="Photos in the pool"
          items={items.map((item) => ({
            key: item.asset_id,
            src: item.thumbnail,
            alt: `${item.kind === "video" ? "Video" : "Photo"} dated ${captureDay(item.taken_at)}`,
            width: item.width,
            height: item.height,
            video: item.kind === "video",
          }))}
          onMore={hasMore ? onLoadMore : undefined}
          moreBusy={loadingMore}
        />
      )}
    </div>
  );
}
