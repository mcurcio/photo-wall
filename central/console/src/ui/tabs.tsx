import { Tabs as BaseTabs } from "@base-ui/react/tabs";
import type * as React from "react";

import { cn } from "./cn";

export interface TabItem {
  /** The tab's key: what `value` and `onValueChange` carry. */
  value: string;
  /** The tab's visible name, and its accessible one. */
  label: string;
}

export interface TabsProps {
  /** The tablist's accessible name. */
  label: string;
  tabs: readonly TabItem[];
  /** The selected tab's key (controlled: the caller keeps it, for example in the address). */
  value: string;
  onValueChange: (value: string) => void;
  /** The selected tab's panel only: a panel that is not shown is not mounted. */
  children: React.ReactNode;
}

/**
 * Tabs: one row of tabs over the selected tab's panel (Base UI Tabs, controlled). Arrow keys
 * move between tabs; only the selected panel is mounted, so a panel's effects run only while
 * it is shown.
 */
export function Tabs({ label, tabs, value, onValueChange, children }: TabsProps) {
  const selected = tabs.find((tab) => tab.value === value);
  return (
    <BaseTabs.Root
      value={value}
      onValueChange={(next) => onValueChange(String(next))}
      className="flex min-w-0 flex-col gap-4"
    >
      <BaseTabs.List aria-label={label} className="flex flex-wrap gap-1 border-b border-line">
        {tabs.map((tab) => (
          <BaseTabs.Tab
            key={tab.value}
            value={tab.value}
            className={cn(
              "-mb-px cursor-pointer border-0 border-b-2 border-transparent bg-transparent px-4 py-2",
              "font-sans text-sm text-muted hover:text-text",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
              "data-active:border-accent data-active:font-medium data-active:text-text",
            )}
          >
            {tab.label}
          </BaseTabs.Tab>
        ))}
      </BaseTabs.List>
      <BaseTabs.Panel value={value} aria-label={selected?.label} className="flex min-w-0 flex-col gap-4">
        {children}
      </BaseTabs.Panel>
    </BaseTabs.Root>
  );
}
