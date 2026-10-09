import type { Severity } from "../design/tokens";
import { cn } from "../ui/cn";
import { SeverityMark, severityTint } from "../ui/severity";

/**
 * A fact outside its owning domain is only this (design rule H1): its words, where its
 * owner judges it, and the owner's severity (none for a link that only navigates). The href is already formatted: a pattern never
 * formats a route.
 */
export interface OwnerLink {
  /** "pi-07 · throttled now" */
  text: string;
  href: string;
  /** Absent: a plain navigation link, in the neutral tone, not a judgement. */
  severity?: Severity;
}

/** LinkToOwner: a read-only link chip naming a fact and leading to its owning page. */
export function LinkToOwner({ text, href, severity }: OwnerLink) {
  return (
    <a
      href={href}
      data-severity={severity ?? "none"}
      className={cn(
        "inline-flex max-w-full items-center gap-1.5 rounded-pill border px-2.5 py-0.5 text-xs text-text",
        "wrap-anywhere no-underline hover:underline",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        severityTint({ severity }),
      )}
    >
      {severity ? <SeverityMark severity={severity} /> : null}
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
