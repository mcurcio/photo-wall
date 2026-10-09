import type { Severity } from "../design/tokens";
import { cn } from "../ui/cn";

/**
 * A fact outside its owning domain is only this (design rule H1): its words, where its
 * owner judges it, and the owner's severity. The href is already formatted: a pattern never
 * formats a route.
 */
export interface OwnerLink {
  /** "pi-07 · throttled now" */
  text: string;
  href: string;
  severity: Severity;
}

const TONE: Record<Severity, string> = {
  ok: "border-ok bg-ok/12",
  todo: "border-todo bg-todo/12",
  notice: "border-notice bg-notice/12",
  alarm: "border-alarm bg-alarm/12",
  unknown: "border-unknown bg-unknown/12",
};

/** LinkToOwner: a read-only link chip naming a fact and leading to its owning page. */
export function LinkToOwner({ text, href, severity }: OwnerLink) {
  return (
    <a
      href={href}
      data-severity={severity}
      className={cn(
        "inline-flex max-w-full items-center rounded-pill border px-2.5 py-0.5 text-xs text-text",
        "wrap-anywhere no-underline hover:underline",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        TONE[severity],
      )}
    >
      {text}
    </a>
  );
}

/** OwnerLinks: several owner links in one wrapping line. */
export function OwnerLinks({ label, links }: { label?: string; links: readonly OwnerLink[] }) {
  if (links.length === 0) return null;
  return (
    <p className="m-0 mt-1 flex flex-wrap items-center gap-1.5 text-sm text-muted">
      {label ? <span>{label}</span> : null}
      {links.map((link) => (
        <LinkToOwner key={link.href} {...link} />
      ))}
    </p>
  );
}
