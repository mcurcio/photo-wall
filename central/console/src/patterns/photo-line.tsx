import type * as React from "react";

export interface PhotoLineProps {
  /** Central's own same-origin thumbnail URL. */
  src: string;
  /** What the picture shows, in words, when it says more than the words beside it; "" (the
   * default) when the words beside it say it all. */
  alt?: string;
  /** The words beside it. */
  children: React.ReactNode;
}

/**
 * PhotoLine: one photo as a list row's lead: its thumbnail whole (never cropped) in a fixed
 * 64 px box, its words beside it. A thumbnail that does not load leaves the box empty, never a
 * broken-image mark taking the words' place.
 */
export function PhotoLine({ src, alt = "", children }: PhotoLineProps) {
  return (
    <span className="flex min-w-0 items-center gap-3">
      <img src={src} alt={alt} loading="lazy" className="size-16 shrink-0 rounded-button bg-surface-sunken object-contain" />
      <span className="min-w-0 wrap-anywhere">{children}</span>
    </span>
  );
}
