import { Dialog as BaseDialog } from "@base-ui/react/dialog";
import type * as React from "react";

import { Button } from "./button";
import { cn } from "./cn";
import { Split } from "./split";

export interface SheetProps {
  open: boolean;
  /** Called with `false` on Escape, an outside press or Close. */
  onOpenChange: (open: boolean) => void;
  title: string;
  children: React.ReactNode;
}

/**
 * Sheet (design language §4): a panel over the page, from the bottom on a phone and from the
 * side above `md` (Base UI Dialog: focus is trapped and Escape closes it). Its title names it,
 * with Close beside it; it sits above the page's sticky bars and toasts.
 */
export function Sheet({ open, onOpenChange, title, children }: SheetProps) {
  return (
    <BaseDialog.Root open={open} onOpenChange={(next) => onOpenChange(next)}>
      <BaseDialog.Portal>
        <BaseDialog.Backdrop className="fixed inset-0 z-30 bg-surface/70" />
        <BaseDialog.Popup
          className={cn(
            "fixed inset-x-0 bottom-0 z-30 flex max-h-5/6 flex-col gap-3 overflow-y-auto",
            "rounded-t-card border border-line bg-surface-raised p-4 font-sans text-text shadow-lg shadow-shadow",
            "md:inset-y-0 md:right-0 md:left-auto md:max-h-none md:w-120 md:rounded-none md:p-6",
            "focus-visible:outline-2 focus-visible:outline-focus",
          )}
        >
          <Split action={<BaseDialog.Close render={<Button variant="ghost" />}>Close</BaseDialog.Close>}>
            <BaseDialog.Title className="m-0 text-lg font-semibold wrap-anywhere">{title}</BaseDialog.Title>
          </Split>
          <div className="min-w-0 text-sm">{children}</div>
        </BaseDialog.Popup>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}
