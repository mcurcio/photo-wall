import { Dialog as BaseDialog } from "@base-ui/react/dialog";
import * as React from "react";

import { Button, type ButtonProps } from "./button";
import { cn } from "./cn";

const BusyContext = React.createContext(false);

export interface DialogProps {
  open: boolean;
  /** Called with `false` on Escape, an outside press or a {@link DialogClose}; never while busy. */
  onOpenChange: (open: boolean) => void;
  /**
   * A request is in flight: Escape, an outside press and every {@link DialogClose} do
   * nothing, and focus moves to the dialog itself (its buttons are disabled then, so it
   * does not fall out of the dialog). The rule of ConfirmAction.jsx.
   */
  busy?: boolean;
  title: React.ReactNode;
  children: React.ReactNode;
  /** The action row: the confirm button and a {@link DialogClose}. */
  actions?: React.ReactNode;
}

/** Dialog: a modal over the page, titled, with an action row. */
export function Dialog({ open, onOpenChange, busy = false, title, children, actions }: DialogProps) {
  const popupRef = React.useRef<HTMLDivElement | null>(null);
  const busyRef = React.useRef(busy);
  busyRef.current = busy;

  React.useEffect(() => {
    if (busy) {
      popupRef.current?.focus();
    }
  }, [busy]);

  return (
    <BaseDialog.Root
      open={open}
      disablePointerDismissal={busy}
      onOpenChange={(next, details) => {
        if (!next && busyRef.current) {
          details.cancel();
          return;
        }
        onOpenChange(next);
      }}
    >
      <BaseDialog.Portal>
        <BaseDialog.Backdrop className="fixed inset-0 bg-surface/70" />
        <BaseDialog.Popup
          ref={popupRef}
          tabIndex={-1}
          aria-busy={busy}
          className={cn(
            "fixed inset-x-4 top-1/2 mx-auto max-w-120 -translate-y-1/2",
            "rounded-card border border-line bg-surface-raised p-6 font-sans text-text shadow-lg shadow-shadow",
            "focus-visible:outline-2 focus-visible:outline-focus",
        )}
        >
          <BaseDialog.Title className="m-0 mb-3 text-lg font-semibold">{title}</BaseDialog.Title>
          <div className="text-sm">{children}</div>
          {actions !== undefined && (
            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <BusyContext.Provider value={busy}>{actions}</BusyContext.Provider>
            </div>
          )}
        </BaseDialog.Popup>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}

/** The dialog's Cancel or Close: disabled while the dialog is busy. */
export function DialogClose({ children, disabled, ...props }: ButtonProps) {
  const busy = React.useContext(BusyContext);
  return (
    <BaseDialog.Close render={<Button {...props} disabled={busy || disabled} />}>
      {children}
    </BaseDialog.Close>
  );
}
