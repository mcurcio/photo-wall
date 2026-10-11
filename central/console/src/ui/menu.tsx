import { Menu as BaseMenu } from "@base-ui/react/menu";

import { buttonVariants } from "./button";
import { cn } from "./cn";

export interface MenuItem {
  label: string;
  onAction: () => void;
  disabled?: boolean;
}

export interface MenuProps {
  /** The trigger's accessible name: "More actions for garden". */
  label: string;
  items: readonly MenuItem[];
}

/**
 * Menu (design language §4, overflow actions): a "More" button that opens a list of actions
 * (Base UI Menu: a menu button and menu items to a screen reader; arrow keys move between them).
 * Actions are words; the trigger shows "More" and is named for what it acts on.
 */
export function Menu({ label, items }: MenuProps) {
  return (
    <BaseMenu.Root>
      <BaseMenu.Trigger aria-label={label} className={buttonVariants({ variant: "ghost" })}>
        More
      </BaseMenu.Trigger>
      <BaseMenu.Portal>
        <BaseMenu.Positioner sideOffset={4} align="end">
          <BaseMenu.Popup
            className={cn(
              "flex min-w-40 flex-col rounded-card border border-line bg-surface-raised py-1 font-sans text-sm text-text",
              "shadow-lg shadow-shadow focus-visible:outline-none",
            )}
          >
            {items.map((item) => (
              <BaseMenu.Item
                key={item.label}
                disabled={item.disabled}
                onClick={item.onAction}
                className={cn(
                  "cursor-pointer px-4 py-2 outline-none select-none",
                  "data-highlighted:bg-accent-tint data-highlighted:text-accent",
                  "data-disabled:cursor-not-allowed data-disabled:opacity-60",
                )}
              >
                {item.label}
              </BaseMenu.Item>
            ))}
          </BaseMenu.Popup>
        </BaseMenu.Positioner>
      </BaseMenu.Portal>
    </BaseMenu.Root>
  );
}
