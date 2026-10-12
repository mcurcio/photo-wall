import { useLayoutEffect, useRef, useState } from "react";

import { Button } from "../ui/button";
import { cn } from "../ui/cn";

export interface MediaGridItem {
  key: string;
  /** Central's own same-origin thumbnail URL. */
  src: string;
  /** What the thumbnail shows, in words ("Photo dated 3 Mar 2025"). */
  alt: string;
  /** The photo's own pixel size: the grid draws each thumbnail at this ratio, never cropped square. */
  width: number;
  height: number;
  /** A video is marked as one, in words as well as by the mark. */
  video?: boolean;
}

export interface MediaGridProps {
  /** The grid's accessible name. */
  label: string;
  items: readonly MediaGridItem[];
  /** "Show more", when more can be read. */
  onMore?: () => void;
  moreBusy?: boolean;
  /** Its thumbnails dimmed: an older read, while a newer one runs. */
  faded?: boolean;
}

/** The space between thumbnails, in pixels: the `gap-2` the list draws (2 x the 4 px spacing). */
const GAP = 8;

/** A row of a justified layout: its items' indexes and the height they share. */
export interface JustifiedRow { items: number[]; height: number }

/**
 * Immich's timeline layout (justified rows): each row holds as many thumbnails, each at its own
 * ratio, as fit `width` at about `target` pixels high, then grows them to fill the row exactly.
 * The last row keeps the target height rather than stretch a lone photo. Pure, so it is tested
 * through the grid and reused by any grid of photos.
 */
export function justifiedRows(ratios: readonly number[], width: number, target: number, gap = GAP): JustifiedRow[] {
  const rows: JustifiedRow[] = [];
  let current: number[] = [];
  let sum = 0;
  ratios.forEach((ratio, index) => {
    current.push(index);
    sum += ratio;
    const natural = sum * target + gap * (current.length - 1);
    if (natural >= width) {
      rows.push({ items: current, height: (width - gap * (current.length - 1)) / sum });
      current = [];
      sum = 0;
    }
  });
  if (current.length > 0) rows.push({ items: current, height: target });
  return rows;
}

/** The list's own width, kept current as the page resizes; 0 until it is laid out. */
function useWidth() {
  const ref = useRef<HTMLUListElement | null>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (element === null) return undefined;
    setWidth(element.clientWidth);
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => setWidth(element.clientWidth));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

const ratioOf = (item: MediaGridItem) => (item.width > 0 && item.height > 0 ? item.width / item.height : 1);

/**
 * MediaGrid (design language §4, delivery 4): thumbnails newest first in justified rows, each at
 * the photo's own ratio (Immich's timeline), with "Show more" when more can be read. A photo's
 * shape is what a Frame judges it by, so a thumbnail is never cropped to a square. The thumbnails
 * are Central's; nothing here reaches the library.
 */
export function MediaGrid({ label, items, onMore, moreBusy = false, faded = false }: MediaGridProps) {
  const [ref, width] = useWidth();
  const target = width > 0 && width < 600 ? 110 : 160;
  const ratios = items.map(ratioOf);
  // Until the list is laid out every row is the target height (a ragged right edge, never a crop).
  const heights = new Array<number>(items.length).fill(target);
  if (width > 0) {
    for (const row of justifiedRows(ratios, width, target)) {
      for (const index of row.items) heights[index] = row.height;
    }
  }
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <ul ref={ref} aria-label={label} aria-busy={faded || undefined}
        className={cn("m-0 flex min-w-0 list-none flex-wrap gap-2 p-0", faded ? "opacity-60" : "")}>
        {items.map((item, index) => {
          const height = Math.max(1, Math.floor(heights[index]));
          const shown = Math.max(1, Math.min(Math.floor(heights[index] * ratios[index]), width > 0 ? width : Infinity));
          return (
            <li key={item.key} className="relative min-w-0 overflow-hidden rounded-input bg-surface-sunken">
              <img src={item.src} alt={item.alt} loading="lazy" width={shown} height={height}
                className="block object-cover" />
              {item.video ? (
                <span className="absolute right-1 bottom-1 rounded-pill bg-surface px-2 text-xs text-text">Video</span>
              ) : null}
            </li>
          );
        })}
      </ul>
      {onMore ? (
        <Button className="w-fit" disabled={moreBusy} onClick={onMore}>{moreBusy ? "Reading…" : "Show more"}</Button>
      ) : null}
    </div>
  );
}
