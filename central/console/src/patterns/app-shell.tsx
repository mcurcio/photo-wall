import type * as React from "react";

import { cn } from "../ui/cn";
import { Icon, type IconName } from "../ui/icon";
import { StatusChip } from "../ui/status-chip";
import type { Verdict } from "./health-badge";

/** One bottom tab: a place to go, or (the last one) a button that opens the rest of the menu. */
export type BottomTab = { key: string; label: string; icon: IconName } & (
  | { href: string; current: boolean; onChoose?: (event: React.MouseEvent<HTMLAnchorElement>) => void }
  | { onOpen: () => void; expanded: boolean }
);

export interface BottomTabsProps {
  /** The navigation's name ("Main"). */
  label: string;
  tabs: readonly BottomTab[];
  /** Pinned to the bottom of a phone's screen and absent above the switch (the app); false draws
   * it in place at any width (the catalogue). */
  pinned?: boolean;
}

const TAB = cn(
  "flex h-full min-h-11 min-w-0 cursor-pointer flex-col items-center justify-center gap-0.5 rounded-none border-0 border-t-3",
  "border-transparent bg-transparent px-1 py-0 font-sans text-xs font-medium text-text no-underline",
  "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus",
);

/**
 * BottomTabs (design language §4 `AppShell`, F6): the phone's navigation, pinned to the bottom of
 * the screen below the one structural switch and absent above it, where the rail is. At most four
 * tabs, each a 44 px target or more, its icon over its word; the current one is marked by weight
 * and a bar as well as the accent, and said as the current page. The last tab may be a button
 * (More) that opens the full menu. It is `--spacing-tabs` high (design/tokens.css): a page's own
 * sticky bar sits above it.
 */
export function BottomTabs({ label, tabs, pinned = true }: BottomTabsProps) {
  return (
    <nav aria-label={label}
      className={cn("grid h-14 auto-cols-fr grid-flow-col border-t border-line bg-surface",
        pinned && "fixed inset-x-0 bottom-0 z-20 md:hidden")}>
      {tabs.map((tab) => ("href" in tab ? (
        <a key={tab.key} href={tab.href} aria-current={tab.current ? "page" : undefined} onClick={tab.onChoose}
          className={cn(TAB, tab.current && "border-accent font-semibold text-accent")}>
          <Icon name={tab.icon} size="tab" decorative />
          <span className="max-w-full truncate">{tab.label}</span>
        </a>
      ) : (
        <button key={tab.key} type="button" aria-haspopup="dialog" aria-expanded={tab.expanded} onClick={tab.onOpen}
          className={TAB}>
          <Icon name={tab.icon} size="tab" decorative />
          <span className="max-w-full truncate">{tab.label}</span>
        </button>
      )))}
    </nav>
  );
}

export interface HouseWordProps {
  /** The house's word, already judged and worded ("All good", "Not showing"). */
  verdict: Verdict;
  /** Home, where the word is explained. */
  href: string;
  onChoose?: (event: React.MouseEvent<HTMLAnchorElement>) => void;
}

/**
 * HouseWord (design language §4 `AppShell`): the house word in the top bar, on every page: the one
 * chip, as a link to Home, where the word is explained. Its name says what it is ("House: All good").
 */
export function HouseWord({ verdict, href, onChoose }: HouseWordProps) {
  return (
    <a href={href} onClick={onChoose} aria-label={`House: ${verdict.label}`}
      className={cn("inline-flex rounded-pill no-underline",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus")}>
      <StatusChip severity={verdict.severity}>{verdict.label}</StatusChip>
    </a>
  );
}

/**
 * WideOnly: something drawn only above the phone switch, because below it the bottom tabs carry it
 * (Home's **Show now** quick action). Absent from the phone's layout and its accessibility tree.
 */
export function WideOnly({ children }: { children: React.ReactNode }) {
  return <span className="contents max-md:hidden">{children}</span>;
}
