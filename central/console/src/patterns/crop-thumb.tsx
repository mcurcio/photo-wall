import { useId } from "react";

import { cn } from "../ui/cn";

/** One photo drawn as a Frame would show it: the data a CropThumb or a FrameTile draws. */
export interface CropView {
  key: string;
  /** Central's own same-origin thumbnail URL. */
  src: string;
  /** What the photo is, in words ("Photo, 3:2"). */
  alt: string;
  /** The photo's own pixel size (upright, edited): it is drawn at this ratio. */
  width: number;
  height: number;
  /** The Frame's shape (width / height): the window's. */
  aspect: number;
  /** Where the window sits on the photo's free axis: 0 left or top, 1 right or bottom. */
  offset: number;
  /** The verdict in words ("fits", "a face would be cut"): read after `alt`. */
  verdict: string;
  /** The Frame would not show it: its window is drawn dashed. */
  refused?: boolean;
  video?: boolean;
}

/** The window as fractions of the photo (x, y, width, height). Drawing only: the window's one
 * home is contracts/photo_crop.py `crop_window`, which Central and the Player use; this draws
 * the same arithmetic for the offset Central chose, and judges nothing. */
export function windowOf(view: Pick<CropView, "width" | "height" | "aspect" | "offset">): [number, number, number, number] {
  const ratio = view.width / view.height;
  if (ratio > view.aspect) {
    const w = view.aspect / ratio;
    return [view.offset * (1 - w), 0, w, 1];
  }
  if (ratio < view.aspect) {
    const h = ratio / view.aspect;
    return [0, view.offset * (1 - h), 1, h];
  }
  return [0, 0, 1, 1];
}

export interface CropThumbProps {
  sample: CropView;
  /** Its height in pixels; its width follows the photo's ratio (capped at twice the height). */
  height?: number;
  /** Opens the photo (its sheet); without it the thumbnail is a picture only. */
  onOpen?: () => void;
}

/**
 * CropThumb (design language §4, follow-ups C1): a photo at its own ratio with the Frame's
 * window drawn on it, the kept part clear and the cut-away dimmed (Immich's crop tool look). A
 * window the Frame would not show is dashed. The window sits where Central put it, so what you
 * see is what the Frame would show.
 */
export function CropThumb({ sample, height = 96, onOpen }: CropThumbProps) {
  const clip = useId();
  const ratio = sample.width / sample.height;
  const shownWidth = Math.round(height * Math.min(ratio, 2));
  const W = 100 * ratio;
  const H = 100;
  const [x, y, w, h] = windowOf(sample);
  const label = `${sample.alt}, ${sample.verdict}`;
  const picture = (
    <svg role="img" aria-label={label} width={shownWidth} height={height} viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid slice" className="block rounded-input bg-surface-sunken">
      <defs>
        <clipPath id={clip}>
          <rect x={0} y={0} width={W} height={H} />
        </clipPath>
      </defs>
      <g clipPath={`url(#${clip})`}>
        <image href={sample.src} x={0} y={0} width={W} height={H} preserveAspectRatio="none" />
        <path
          fillRule="evenodd"
          className="fill-surface opacity-70"
          d={`M0 0H${W}V${H}H0Z M${x * W} ${y * H}h${w * W}v${h * H}h${-w * W}Z`}
        />
        <rect x={x * W} y={y * H} width={w * W} height={h * H}
          className="fill-none stroke-text" strokeWidth={1.5} vectorEffect="non-scaling-stroke"
          strokeDasharray={sample.refused ? "4 3" : undefined} />
      </g>
      {sample.video ? (
        <text x={W - 3} y={H - 5} textAnchor="end" className="fill-text text-xs">Video</text>
      ) : null}
    </svg>
  );
  if (onOpen === undefined) return picture;
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`Open ${label}`}
      className={cn(
        "cursor-pointer rounded-input border-0 bg-transparent p-0",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
      )}
    >
      {picture}
    </button>
  );
}

export interface FrameTileProps {
  /** The Frame's shape (its output's width / height). */
  aspect: number;
  /** What it would show; null draws the empty Frame with `emptyText`. */
  lead: CropView | null;
  /** Its height in pixels; its width follows its shape. */
  height?: number;
  /** The tile's accessible name ("Hall, 9:16"). */
  label: string;
  emptyText?: string;
}

/**
 * FrameTile (follow-ups C1): a Frame drawn at its output's shape, filled with one photo cropped
 * as that Frame would crop it (its window scaled to fill the tile), or empty.
 */
export function FrameTile({ aspect, lead, height = 96, label, emptyText = "Nothing fits" }: FrameTileProps) {
  const clip = useId();
  const W = 100 * aspect;
  const H = 100;
  const width = Math.round(height * Math.min(aspect, 3));
  let image = null;
  if (lead !== null) {
    const [x, y, w, h] = windowOf(lead);
    // The photo, scaled so its window fills the tile, and moved so the window sits on it.
    const photoW = W / w;
    const photoH = H / h;
    image = (
      <image href={lead.src} x={-x * photoW} y={-y * photoH} width={photoW} height={photoH}
        preserveAspectRatio="none" />
    );
  }
  return (
    <svg role="img" aria-label={lead !== null ? `${label}: ${lead.alt}` : emptyText === "" ? label : `${label}: ${emptyText}`}
      width={width} height={height} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet"
      className="block shrink-0">
      <defs>
        <clipPath id={clip}>
          <rect x={0} y={0} width={W} height={H} rx={3} />
        </clipPath>
      </defs>
      <g clipPath={`url(#${clip})`}>
        <rect x={0} y={0} width={W} height={H} className="fill-surface-sunken" />
        {image}
      </g>
      <rect x={0.5} y={0.5} width={W - 1} height={H - 1} rx={3}
        className="fill-none stroke-line-input" strokeWidth={1} vectorEffect="non-scaling-stroke"
        strokeDasharray={lead === null ? "4 3" : undefined} />
      {lead === null && emptyText !== "" && width >= 64 ? (
        <text x={W / 2} y={H / 2} textAnchor="middle" dominantBaseline="middle"
          className="fill-muted text-xs">{emptyText}</text>
      ) : null}
    </svg>
  );
}
