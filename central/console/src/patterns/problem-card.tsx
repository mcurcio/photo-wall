import type * as React from "react";

import { Button } from "../ui/button";
import { cn } from "../ui/cn";
import { Disclosure } from "../ui/disclosure";
import { Link } from "../ui/link";
import { StatusChip } from "../ui/status-chip";
import type { Verdict } from "./health-badge";

export type ProblemAction = { label: string; onAction: () => void } | { label: string; href: string };

interface ProblemBase {
  /** Severity and its plain words. */
  verdict: Verdict;
  /** The plain cause, naming the failing part. */
  what: string;
  /** What Photo Wall does about it by itself ("Photo Wall keeps trying."); null for nothing. */
  doing: string | null;
  /** The one thing to do about it; none for a value refused where it was typed. */
  action?: ProblemAction;
  /** The evidence (codes, ids), shown only on request under Details. */
  details?: React.ReactNode;
  /** `inline` sits in a form or beside a control; `card` stands alone in a page. */
  variant?: "card" | "inline";
  /** `alert` for a problem that arrives while the page is open; otherwise a region. */
  live?: boolean;
}

export type ProblemCardProps = ProblemBase & (
  | { scope: "live"; subject: string; since: string }
  | { scope: "central" }
  | { scope: "setup"; subject?: string }
);

/**
 * ProblemCard (design language §4, the error template): what is wrong in plain words, naming the
 * failing part — since when, for a live problem — what Photo Wall is doing about it — one
 * action; the evidence under **Details**.
 */
export function ProblemCard(props: ProblemCardProps) {
  const { verdict, what, doing, action, details, variant = "card", live = false } = props;
  const lead = props.scope === "live" ? `${props.subject}: ${verdict.label} since ${props.since}.`
    : props.scope === "setup" && props.subject ? `${props.subject}: ${verdict.label}.` : null;
  return (
    <div
      role={live ? "alert" : "group"}
      aria-label={live ? undefined : verdict.label}
      className={cn(
        "flex min-w-0 flex-col gap-2 text-sm text-text",
        variant === "card" ? "rounded-card border border-line bg-surface-raised px-4 py-3" : "border-l-2 border-line pl-3",
      )}
    >
      <span className="flex flex-wrap items-center gap-2">
        <StatusChip severity={verdict.severity}>{verdict.label}</StatusChip>
        {lead !== null && <span className="font-medium">{lead}</span>}
      </span>
      <p className="m-0 wrap-anywhere">
        {what}
        {doing !== null && ` ${doing}`}
      </p>
      {action !== undefined && (
        "href" in action ? (
          <Link href={action.href}>{action.label}</Link>
        ) : (
          <Button className="w-fit" onClick={action.onAction}>{action.label}</Button>
        )
      )}
      {details !== undefined && <Disclosure summary="Details">{details}</Disclosure>}
    </div>
  );
}
