import { useId } from "react";
import type * as React from "react";

import { cn } from "../ui/cn";
import { Disclosure } from "../ui/disclosure";
import { Link } from "../ui/link";
import { type CropView, CropThumb, FrameTile } from "./crop-thumb";

/** One part of a Frame's bar: the photos that fit, or those left off for one reason. */
export interface FitSegment {
  key: string;
  /** Its words ("fit", "doesn't fit this Frame's shape"). */
  label: string;
  count: number;
  /** Its count in words ("1,520"). */
  countText: string;
}

/** The photos left off a Frame for one reason, with a few of them. */
export interface FitReason {
  key: string;
  label: string;
  countText: string;
  samples: readonly CropView[];
}

/** One Frame as a Fit board shows it: plain words and pictures, judged by Central. */
export interface FrameFitView {
  key: string;
  name: string;
  /** Its output's shape (width / height), and that shape in words ("9:16"). */
  aspect: number;
  ratio: string;
  /** What it would show first, cropped as it would crop it; null when there is none to draw. */
  lead: CropView | null;
  /** The empty tile's words when `lead` is null ("Nothing fits"). */
  emptyText: string;
  /** "1,520 of 1,689 fit". */
  count: string;
  /** The bar: what fits first, then each reason in the fit rule's order (each with photos). */
  segments: readonly FitSegment[];
  /** A few photos that fit, cropped as this Frame crops them. */
  samples: readonly CropView[];
  /** The sampled check of faces and the interesting part, in words, or null. */
  checked: string | null;
  /** The pixels it is judged at, in words ("Frame output 1,512 × 1,008 px · on a 3,840 × 2,160 display"). */
  detail: string;
  /** A short warning drawn under its tile on a wall ("Nothing fits", "Only 1 photo"), or null. */
  alert: string | null;
  /** "Why 169 don't" and its reasons, or null when every photo fits. */
  why: { summary: string; reasons: readonly FitReason[] } | null;
  /** A badge for a Frame judged with unsaved values ("As you are setting it"), or null. */
  draft: string | null;
  /** Where the name leads (the Frame's page), if anywhere. */
  href?: string;
}

export interface FrameFitRowProps {
  frame: FrameFitView;
  /** `full` draws the samples and each reason's photos; `compact` the counts only. */
  size: "full" | "compact";
  /** Opens a photo (its sheet). */
  onOpen?: (sample: CropView) => void;
  /** Called when the Frame's tile is pressed (the board focuses its band). */
  onFocus?: () => void;
  /** One action for this Frame, from its host ("Raise the crop limit to 45 %"). */
  action?: React.ReactNode;
  /** Its pictures dimmed: an older answer, while a newer one is read or after a failed read
   * (the words keep their contrast). */
  faded?: boolean;
  /** False when its host already names the Frame (the Frame's own page): the row leads with its
   * count, not the Frame's name and shape again. */
  named?: boolean;
}

/** The bar's fills, in order: solid for what fits, then a neutral pattern per reason. Never a
 * severity colour: the bar is a count, not a status. */
const FILLS = ["solid", "hatch", "dots", "cross", "dash", "grid"] as const;

function Swatch({ fill, pattern }: { fill: (typeof FILLS)[number]; pattern: string }) {
  return (
    <svg aria-hidden="true" width={12} height={12} viewBox="0 0 12 12" className="block shrink-0">
      <rect x={0.5} y={0.5} width={11} height={11} rx={2}
        className={fill === "solid" ? "fill-text stroke-line-input" : "stroke-line-input"}
        fill={fill === "solid" ? undefined : `url(#${pattern}-${fill})`} />
    </svg>
  );
}

function Patterns({ id }: { id: string }) {
  return (
    <defs>
      <pattern id={`${id}-hatch`} width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width={6} height={6} className="fill-surface-sunken" />
        <line x1={0} y1={0} x2={0} y2={6} className="stroke-muted" strokeWidth={2} />
      </pattern>
      <pattern id={`${id}-dots`} width={5} height={5} patternUnits="userSpaceOnUse">
        <rect width={5} height={5} className="fill-surface-sunken" />
        <circle cx={2.5} cy={2.5} r={1.2} className="fill-muted" />
      </pattern>
      <pattern id={`${id}-cross`} width={6} height={6} patternUnits="userSpaceOnUse">
        <rect width={6} height={6} className="fill-surface-sunken" />
        <path d="M0 0L6 6M6 0L0 6" className="stroke-muted" strokeWidth={1} />
      </pattern>
      <pattern id={`${id}-dash`} width={6} height={6} patternUnits="userSpaceOnUse">
        <rect width={6} height={6} className="fill-surface-sunken" />
        <line x1={0} y1={3} x2={6} y2={3} className="stroke-muted" strokeWidth={1.5} />
      </pattern>
      <pattern id={`${id}-grid`} width={6} height={6} patternUnits="userSpaceOnUse">
        <rect width={6} height={6} className="fill-surface-sunken" />
        <path d="M0 0H6M0 0V6" className="stroke-muted" strokeWidth={1} />
      </pattern>
    </defs>
  );
}

/** The stacked bar and its legend: the legend is the bar's words. */
function FitBar({ segments, label }: { segments: readonly FitSegment[]; label: string }) {
  const id = useId().replace(/:/g, "");
  const total = segments.reduce((sum, segment) => sum + segment.count, 0);
  let x = 0;
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <svg aria-hidden="true" width="100%" height={10} className="block">
        <Patterns id={id} />
        <rect x={0} y={0} width="100%" height={10} rx={3} className="fill-surface-sunken" />
        {total > 0 && segments.map((segment, index) => {
          const share = (100 * segment.count) / total;
          const start = x;
          x += share;
          if (segment.count === 0) return null;
          const fill = FILLS[Math.min(index, FILLS.length - 1)];
          return (
            <rect key={segment.key} x={`${start}%`} y={0} width={`${share}%`} height={10}
              className={fill === "solid" ? "fill-text" : undefined}
              fill={fill === "solid" ? undefined : `url(#${id}-${fill})`} />
          );
        })}
      </svg>
      <ul aria-label={label} className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 p-0 text-xs text-muted">
        {segments.filter((segment) => segment.count > 0).map((segment) => {
          const index = segments.indexOf(segment);
          return (
            <li key={segment.key} className="flex items-center gap-1.5">
              <Swatch fill={FILLS[Math.min(index, FILLS.length - 1)]} pattern={id} />
              <span>{`${segment.label}: ${segment.countText}`}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/**
 * FrameFitRow (design language §4, follow-ups C1): one Frame of a Fit board. Its tile at its
 * output's shape, filled with what it would show; its name and shape; how many photos fit and a
 * bar of fits and refusals (neutral patterns, never a severity colour); in `full`, a few photos
 * cropped as it crops them; "Why N don't" opens the reasons, each with its photos.
 */
export function FrameFitRow({ frame, size, onOpen, onFocus, action, faded = false, named = true }: FrameFitRowProps) {
  const full = size === "full";
  const dim = faded ? "opacity-60" : "";
  const tile = (
    <span className={cn("block", dim)}><FrameTile aspect={frame.aspect} lead={frame.lead} height={full ? 88 : 56} label={`${frame.name}, ${frame.ratio}`}
      emptyText={frame.emptyText} /></span>
  );
  return (
    <article aria-label={`Frame ${frame.name}`} className="flex min-w-0 flex-col gap-2 border-b border-line py-3 last:border-b-0">
      <div className="flex min-w-0 items-start gap-3">
        {onFocus ? (
          <button type="button" onClick={onFocus} aria-label={`Show ${frame.name}'s shapes`}
            className="cursor-pointer border-0 bg-transparent p-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
            {tile}
          </button>
        ) : tile}
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          {named || frame.draft !== null ? (
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              {named && (
                <>
                  <h4 className="m-0 text-base font-medium wrap-anywhere text-text">
                    {frame.href ? <Link href={frame.href}>{frame.name}</Link> : frame.name}
                  </h4>
                  <span className="text-sm text-muted">{frame.ratio}</span>
                </>
              )}
              {frame.draft !== null && (
                <span className="rounded-pill border border-line-input px-2.5 py-0.5 text-xs text-text">{frame.draft}</span>
              )}
            </div>
          ) : null}
          <p className="m-0 text-sm text-text tabular-nums">{frame.count}</p>
          <FitBar segments={frame.segments} label={`${frame.name}: fit and left off`} />
        </div>
      </div>
      {full && frame.samples.length > 0 && (
        <ul aria-label={`Photos ${frame.name} would show`} className={cn("m-0 flex min-w-0 list-none flex-wrap gap-2 p-0", dim)}>
          {frame.samples.map((sample) => (
            <li key={sample.key}>
              <CropThumb sample={sample} height={72} onOpen={onOpen ? () => onOpen(sample) : undefined} />
            </li>
          ))}
        </ul>
      )}
      {frame.checked !== null && <p className="m-0 text-xs text-muted">{frame.checked}</p>}
      <p className="m-0 text-xs text-muted">{frame.detail}</p>
      {action}
      {frame.why !== null && (
        <Disclosure summary={frame.why.summary}>
          <ul className="m-0 flex list-none flex-col gap-3 p-0">
            {frame.why.reasons.map((reason) => (
              <li key={reason.key} className="flex min-w-0 flex-col gap-1.5">
                <span className="text-sm text-text">{`${reason.label}: ${reason.countText}`}</span>
                {full && reason.samples.length > 0 && (
                  <ul aria-label={`${reason.label}: some of them`} className="m-0 flex list-none flex-wrap gap-2 p-0">
                    {reason.samples.map((sample) => (
                      <li key={sample.key}>
                        <CropThumb sample={sample} height={56} onOpen={onOpen ? () => onOpen(sample) : undefined} />
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </Disclosure>
      )}
    </article>
  );
}
