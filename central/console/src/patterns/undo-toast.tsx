import { useEffect, useRef, useState } from "react";

import { Button } from "../ui/button";
import { Split } from "../ui/split";

export interface UndoToastProps {
  message: string;
  onUndo: () => void;
  /** The time ran out without Undo: the action is final now. */
  onExpire: () => void;
  /** How long Undo is offered; the count pauses while the toast is hovered or focused. */
  seconds?: number;
}

/**
 * UndoToast (design language §4): a transient line at the bottom of the view with one action,
 * Undo, offered for `seconds` (10 by default). It never takes focus (`role="status"`), and the
 * count pauses while the pointer or the focus is on it. Material's snackbar with an action.
 */
export function UndoToast({ message, onUndo, onExpire, seconds = 10 }: UndoToastProps) {
  const [paused, setPaused] = useState(false);
  const expire = useRef(onExpire);
  expire.current = onExpire;
  useEffect(() => {
    if (paused) return undefined;
    const timer = setTimeout(() => expire.current(), seconds * 1000);
    return () => clearTimeout(timer);
  }, [paused, seconds]);
  return (
    <div
      role="status"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      className={
        "fixed inset-x-4 bottom-4 z-20 mx-auto flex max-w-120 items-center "
        + "rounded-card border border-line bg-surface-raised px-4 py-2 font-sans text-sm text-text shadow-lg shadow-shadow"
      }
    >
      <Split action={<Button variant="ghost" onClick={onUndo}>Undo</Button>}>{message}</Split>
    </div>
  );
}
