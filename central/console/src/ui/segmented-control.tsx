import * as React from "react";

import { cn } from "./cn";

export interface SegmentedControlProps<V extends string> {
  /** The group's name, shown above the choices and its accessible name. */
  label: string;
  value: V;
  /** Two to five exclusive choices. */
  options: readonly { value: V; label: string }[];
  onValueChange: (value: V) => void;
  /** One plain sentence under the choices: the group's description. */
  hint?: string;
  disabled?: boolean;
}

/**
 * SegmentedControl (design language §4): two to five exclusive choices side by side. Native
 * radios in a `radiogroup` named by the label, so arrow keys move the choice and each choice
 * is a radio named by its own label. (A group element, not a fieldset, so no page's fieldset
 * rule restyles it.)
 */
export function SegmentedControl<V extends string>({
  label,
  value,
  options,
  onValueChange,
  hint,
  disabled,
}: SegmentedControlProps<V>) {
  const id = React.useId();
  return (
    <div className="flex min-w-0 flex-col gap-1 text-sm text-label">
      <span id={`${id}-label`}>{label}</span>
      <span
        role="radiogroup"
        aria-labelledby={`${id}-label`}
        aria-describedby={hint ? `${id}-hint` : undefined}
        aria-disabled={disabled || undefined}
        className="inline-flex w-fit flex-wrap overflow-hidden rounded-input border border-line-input"
      >
        {options.map((option) => (
          <label
            key={option.value}
            className={cn(
              "relative cursor-pointer px-3 py-1.5 text-text",
              "not-first:border-l not-first:border-line-input",
              "has-checked:bg-accent has-checked:text-on-accent",
              "has-focus-visible:outline-2 has-focus-visible:-outline-offset-2 has-focus-visible:outline-focus",
              "has-disabled:cursor-not-allowed has-disabled:opacity-60",
            )}
          >
            <input
              type="radio"
              className="absolute inset-0 m-0 cursor-pointer opacity-0"
              name={id}
              value={option.value}
              checked={option.value === value}
              disabled={disabled}
              onChange={() => onValueChange(option.value)}
            />
            {option.label}
          </label>
        ))}
      </span>
      {hint ? <span id={`${id}-hint`} className="text-xs text-muted">{hint}</span> : null}
    </div>
  );
}
