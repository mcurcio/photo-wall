import { Switch as BaseSwitch } from "@base-ui/react/switch";
import * as React from "react";

import { cn } from "./cn";

export interface SwitchProps {
  /** The switch's label, shown beside it and its accessible name. */
  label: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  /** One plain sentence under the label: the switch's description. */
  hint?: string;
  disabled?: boolean;
}

/**
 * Switch (design language §4): an on/off setting. Base UI's Switch (role="switch"), labelled
 * beside it like the field family's CheckboxField (ui/field.tsx), with an optional hint.
 */
export function Switch({ label, checked, onCheckedChange, hint, disabled }: SwitchProps) {
  const id = React.useId();
  return (
    <div className="flex min-w-0 flex-col gap-1 text-sm text-label">
      <span className="inline-flex items-center gap-3">
        <BaseSwitch.Root
          id={id}
          checked={checked}
          disabled={disabled}
          aria-describedby={hint ? `${id}-hint` : undefined}
          onCheckedChange={(next) => onCheckedChange(next)}
          className={cn(
            "relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-pill border",
            "border-line-input bg-surface-input",
            "data-checked:border-accent data-checked:bg-accent",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
            "data-disabled:cursor-not-allowed data-disabled:opacity-60",
          )}
        >
          <BaseSwitch.Thumb
            className={cn(
              "block size-4 translate-x-1 rounded-pill bg-muted",
              "data-checked:translate-x-6 data-checked:bg-on-accent",
            )}
          />
        </BaseSwitch.Root>
        <label htmlFor={id}>{label}</label>
      </span>
      {hint ? <span id={`${id}-hint`} className="text-xs text-muted">{hint}</span> : null}
    </div>
  );
}
