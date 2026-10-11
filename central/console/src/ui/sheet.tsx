import { Dialog as BaseDialog } from "@base-ui/react/dialog";
import type * as React from "react";

import { Button } from "./button";
import { cn } from "./cn";

export interface SheetProps {
  open: boolean;
  /** Called with `false` on Escape, an outside press or Close. */
  onOpenChange: (open: boolean) => void;
  title: string;
  children: React.ReactNode;
}

/**
 * Sheet (design language §4): a panel over the page, from the bottom on a phone and from the
 * side above `md` (Base UI Dialog: focus is trapped and Escape closes it). Its title names it;
 * Close is always its last button.
 */
export function Sheet({ open, onOpenChange, title, children }: SheetProps) {
  return (
    <BaseDialog.Root open={open} onOpenChange={(next) => onOpenChange(next)}>
      <BaseDialog.Portal>
        <BaseDialog.Backdrop className="fixed inset-0 bg-surface/70" />
        <BaseDialog.Popup
          className={cn(
            "fixed inset-x-0 bottom-0 flex max-h-5/6 flex-col gap-3 overflow-y-auto",
            "rounded-t-card border border-line bg-surface-raised p-4 font-sans text-text shadow-lg shadow-shadow",
            "md:inset-y-0 md:right-0 md:left-auto md:max-h-none md:w-120 md:rounded-none md:p-6",
            "focus-visible:outline-2 focus-visible:outline-focus",
          )}
        >
          <div className="flex items-center justify-between gap-3">
            <BaseDialog.Title className="m-0 text-lg font-semibold">{title}</BaseDialog.Title>
            <BaseDialog.Close render={<Button variant="ghost" />}>Close</BaseDialog.Close>
          </div>
          <div className="min-w-0 text-sm">{children}</div>
        </BaseDialog.Popup>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}
