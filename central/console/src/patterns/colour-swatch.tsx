import type { ReactNode } from "react";
import type { TruthKind } from "../design/tokens";

/** A colour line as the console shows it: its words, its truth kind and its swatch (a CSS colour, or
 * null for words alone). One shape, worked out by a domain's model (room lights' `colourLineSaid`)
 * and drawn by any context that shows it (House › Lights, a Frame's Lighting tab). */
export interface ColourLine {
  words: string;
  tone: TruthKind;
  swatch: string | null;
}

// The swatch's size, about the text's x-height, and its corner.
const SWATCH = { width: 20, height: 12, corner: 3 } as const;
const HALF = 0.5;   // the outline's half width, so it falls inside the box

/**
 * ColourSwatch: words about a colour, drawn after a small swatch of it (design language, ColourLine:
 * "a bulb's rgb as its colour swatch"). The words name the colour too, so it is never the only
 * signal (WCAG 1.4.1); the swatch is decorative. `colour` is a CSS colour, already worked out by
 * the domain's model; null draws the words alone.
 */
export function ColourSwatch({ colour, children }: { colour: string | null; children: ReactNode }) {
  if (colour === null) return <>{children}</>;
  return (
    <span className="inline-flex items-center gap-2">
      <svg width={SWATCH.width} height={SWATCH.height} aria-hidden="true" data-swatch={colour} className="shrink-0">
        <rect x={HALF} y={HALF} width={SWATCH.width - 1} height={SWATCH.height - 1} rx={SWATCH.corner} fill={colour}
          strokeWidth={1} className="stroke-line" />
      </svg>
      <span>{children}</span>
    </span>
  );
}
