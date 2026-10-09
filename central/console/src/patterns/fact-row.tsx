import type * as React from "react";

import type { Severity, TruthKind } from "../design/tokens";
import { cn } from "../ui/cn";

/** Whole class names, so Tailwind's scan finds each one. */
const TONE: Record<TruthKind, string> = {
  set: "text-truth-set",
  reported: "text-truth-reported",
  claimed: "text-truth-claimed",
  derived: "text-truth-derived",
  planned: "text-truth-planned",
  unknown: "text-truth-unknown",
};

/** A judged line's band, for the eye only: its words already carry it. */
const BAND: Record<Severity, string> = {
  ok: "",
  todo: "border-l-2 border-todo pl-2",
  notice: "border-l-2 border-notice pl-2",
  alarm: "border-l-2 border-alarm pl-2",
  unknown: "",
};

export interface FactRowProps {
  /** The fact's name; with none the line stands alone. */
  label?: string;
  /** Its truth kind, carried as a tone so an unknown reads differently at a glance. */
  tone: TruthKind;
  /** The judge's band for this line, if any (`data-band`); never decided here. */
  band?: Severity | null;
  /** The fact in its model's one wording. */
  children: React.ReactNode;
}

/** FactRow: one labelled fact line in its truth kind's tone. */
export function FactRow({ label, tone, band = null, children }: FactRowProps) {
  return (
    <p
      data-truth={tone}
      data-band={band ?? "none"}
      className={cn("m-0 mt-0.5 min-w-0 text-sm wrap-anywhere", TONE[tone], band && BAND[band])}
    >
      {label ? <span className="text-muted">{`${label}: `}</span> : null}
      {children}
    </p>
  );
}

export interface FactGroupProps {
  /** The group's heading and its accessible name. */
  title: string;
  children: React.ReactNode;
}

/** FactGroup: a named group of fact lines inside a section. */
export function FactGroup({ title, children }: FactGroupProps) {
  return (
    <div role="group" aria-label={title} className="mt-3 min-w-0">
      <h4 className="m-0 mb-1 text-sm font-medium text-label">{title}</h4>
      {children}
    </div>
  );
}

/** Note: a plain line beside a page's facts (a read time, a read that failed). */
export function Note({ children }: { children: React.ReactNode }) {
  return <p className="m-0 mt-1 text-sm text-muted">{children}</p>;
}
