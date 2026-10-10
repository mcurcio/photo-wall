import { cva } from "class-variance-authority";

import type { Severity } from "../design/tokens";

/*
 * The ONE home of how the severity scale renders (console design system): a chip's tint, a
 * mark's text colour, a row's bar and a fact line's band all take their classes from here,
 * so a severity is never re-tabled in a component. They are `cva` variants on purpose: the
 * console's colour lint (eslint.config.js) reads `cva` calls, so a palette class or an
 * arbitrary value in a tone fails the build. Whole class names, so Tailwind's scan finds each.
 */

/** A chip's tint and border; no severity is the neutral border (a plain link). */
export const severityTint = cva("", {
  variants: {
    severity: {
      none: "border-line bg-transparent",
      ok: "border-ok bg-ok/12",
      todo: "border-todo bg-todo/12",
      notice: "border-notice bg-notice/12",
      alarm: "border-alarm bg-alarm/12",
      unknown: "border-unknown bg-unknown/12",
    },
  },
  defaultVariants: { severity: "none" },
});

/** The colour of a severity's mark. */
export const severityText = cva("", {
  variants: {
    severity: {
      ok: "text-ok",
      todo: "text-todo",
      notice: "text-notice",
      alarm: "text-alarm",
      unknown: "text-unknown",
    },
  },
});

/** A table row's left bar: only the severities that ask for attention draw one. */
export const severityBar = cva("", {
  variants: {
    severity: {
      ok: "",
      todo: "",
      notice: "border-l-3 border-l-notice",
      alarm: "border-l-3 border-l-alarm",
      unknown: "",
    },
  },
});

/** A judged line's band, for the eye only: its words already carry it. */
export const severityBand = cva("", {
  variants: {
    severity: {
      ok: "",
      todo: "border-l-2 border-todo pl-2",
      notice: "border-l-2 border-notice pl-2",
      alarm: "border-l-2 border-alarm pl-2",
      unknown: "",
    },
  },
});

/** A severity's shape: severity never relies on colour alone. */
const MARK: Record<Severity, string> = {
  ok: "●",
  todo: "■",
  notice: "◆",
  alarm: "▲",
  unknown: "○",
};

/** SeverityMark: the severity's shape in its colour, hidden from the accessible name. */
export function SeverityMark({ severity }: { severity: Severity }) {
  return (
    <span aria-hidden="true" className={severityText({ severity })}>
      {MARK[severity]}
    </span>
  );
}
