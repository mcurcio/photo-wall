import { Menu as BaseMenu } from "@base-ui/react/menu";
import { useState } from "react";

import { Button, buttonVariants } from "./button";
import { cn } from "./cn";
import { Sheet } from "./sheet";
import { usePhone } from "./use-phone";

/** One entry: an action done here, or a link to the page that does it. */
export type MenuItem = { label: string; disabled?: boolean } & (
  | { onAction: () => void; href?: never }
  | { href: string; onAction?: never }
);

export interface MenuProps {
  /** The trigger's accessible name: "More actions for garden". */
  label: string;
  items: readonly MenuItem[];
  /** How it opens: `auto` is a list under the button, and a sheet from the bottom on a phone
   * (design language §2); the other two fix one. */
  as?: "auto" | "popup" | "sheet";
}

const ITEM = cn(
  "block cursor-pointer px-4 py-2 text-left text-text no-underline outline-none select-none",
  "data-highlighted:bg-accent-tint data-highlighted:text-accent",
  "data-disabled:cursor-not-allowed data-disabled:opacity-60",
);
const SHEET_ITEM = cn(
  "flex min-h-11 w-full cursor-pointer items-center rounded-button border border-transparent bg-transparent px-3",
  "text-left font-sans text-sm text-text no-underline hover:bg-accent-tint",
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
  "disabled:cursor-not-allowed disabled:opacity-60",
);

/**
 * Menu (design language §4, overflow actions): a "More" button that opens a list of actions
 * (Base UI Menu: a menu button and menu items to a screen reader; arrow keys move between them).
 * Actions are words; the trigger shows "More" and is named for what it acts on. An entry is an
 * action or a link. On a phone the same entries open in a `Sheet` from the bottom, each a 44 px
 * target; an entry that is not offered is absent from `items`, never disabled for that.
 */
export function Menu({ label, items, as = "auto" }: MenuProps) {
  const phone = usePhone();
  const [open, setOpen] = useState(false);
  if (as === "sheet" || (as === "auto" && phone)) {
    return (
      <>
        <Button variant="ghost" aria-label={label} aria-haspopup="dialog" onClick={() => setOpen(true)}>More</Button>
        <Sheet open={open} onOpenChange={setOpen} title={label}>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {items.map((item) => (
              <li key={item.label}>
                {item.href !== undefined ? (
                  <a href={item.href} className={SHEET_ITEM} onClick={() => setOpen(false)}>{item.label}</a>
                ) : (
                  <button type="button" disabled={item.disabled} className={SHEET_ITEM}
                    onClick={() => { setOpen(false); item.onAction(); }}>
                    {item.label}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </Sheet>
      </>
    );
  }
  return (
    <BaseMenu.Root>
      <BaseMenu.Trigger aria-label={label} className={buttonVariants({ variant: "ghost" })}>
        More
      </BaseMenu.Trigger>
      <BaseMenu.Portal>
        <BaseMenu.Positioner sideOffset={4} align="end" className="z-30">
          <BaseMenu.Popup
            className={cn(
              "flex min-w-40 flex-col rounded-card border border-line bg-surface-raised py-1 font-sans text-sm text-text",
              "shadow-lg shadow-shadow focus-visible:outline-none",
            )}
          >
            {items.map((item) => (item.href !== undefined ? (
              <BaseMenu.LinkItem key={item.label} href={item.href} closeOnClick className={ITEM}>
                {item.label}
              </BaseMenu.LinkItem>
            ) : (
              <BaseMenu.Item key={item.label} disabled={item.disabled} onClick={item.onAction} className={ITEM}>
                {item.label}
              </BaseMenu.Item>
            )))}
          </BaseMenu.Popup>
        </BaseMenu.Positioner>
      </BaseMenu.Portal>
    </BaseMenu.Root>
  );
}
