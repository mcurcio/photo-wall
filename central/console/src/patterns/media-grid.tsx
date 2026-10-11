import { Button } from "../ui/button";

export interface MediaGridItem {
  key: string;
  /** Central's own same-origin thumbnail URL. */
  src: string;
  /** What the thumbnail shows, in words ("Photo dated 3 Mar 2025"). */
  alt: string;
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
}

/**
 * MediaGrid (design language §4, delivery 4): thumbnails in a grid, newest first, with "Show
 * more" when more can be read. The thumbnails are Central's; nothing here reaches the library.
 */
export function MediaGrid({ label, items, onMore, moreBusy = false }: MediaGridProps) {
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <ul aria-label={label} className="m-0 grid min-w-0 list-none grid-cols-3 gap-2 p-0 sm:grid-cols-5 lg:grid-cols-6">
        {items.map((item) => (
          <li key={item.key} className="relative aspect-square min-w-0 overflow-hidden rounded-input bg-surface-sunken">
            <img src={item.src} alt={item.alt} loading="lazy" className="size-full object-cover" />
            {item.video ? (
              <span className="absolute right-1 bottom-1 rounded-pill bg-surface px-2 text-xs text-text">Video</span>
            ) : null}
          </li>
        ))}
      </ul>
      {onMore ? (
        <Button className="w-fit" disabled={moreBusy} onClick={onMore}>{moreBusy ? "Reading…" : "Show more"}</Button>
      ) : null}
    </div>
  );
}
