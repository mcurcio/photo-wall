import type * as React from "react";

import { buttonVariants } from "./button";
import { cn } from "./cn";

export interface FileChooserProps {
  /** The button's words, and the file input's accessible name. */
  children: string;
  /** The file types offered first (the input's `accept`: ".icc,.icm"). */
  accept?: string;
  disabled?: boolean;
  variant?: "outline" | "primary";
  /** Called once with the file chosen; never when the chooser is closed without one. */
  onChoose: (file: File) => void;
}

/**
 * FileChooser (design language §4): a button that opens the browser's file chooser. The native
 * file input is kept (so the keyboard, a screen reader and the browser's own chooser all work) and
 * drawn as a Button: it is hidden from sight inside its label, whose ring shows its focus. The
 * input is emptied after each choice, so the same file can be chosen again.
 */
export function FileChooser({ children, accept, disabled = false, variant, onChoose }: FileChooserProps) {
  const choose = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (file !== undefined) onChoose(file);
  };
  return (
    <label
      className={cn(
        buttonVariants({ variant }),
        "has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-focus",
        disabled && "cursor-not-allowed opacity-60",
      )}
    >
      <input type="file" className="sr-only" accept={accept} disabled={disabled} onChange={choose} />
      {children}
    </label>
  );
}
