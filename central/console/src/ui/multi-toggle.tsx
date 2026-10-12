import { Toggle } from "@base-ui/react/toggle";
import { ToggleGroup } from "@base-ui/react/toggle-group";

import { cn } from "./cn";
import { useFieldControl } from "./field";

export interface MultiToggleProps<V extends string> {
  /** The group's accessible name when it sits outside a Field. */
  label: string;
  /** The choices that are on. */
  value: readonly V[];
  /** Two to seven choices (the days of a week), any of them on together. */
  options: readonly { value: V; label: string }[];
  /** The words each choice shows for its state, on and off: "on" and "off" by default; a
   * screen where on and off mean display power says "✓" and nothing instead. */
  stateWords?: readonly [string, string];
  onChange: (value: V[]) => void;
  /** At least one choice stays on: the last one on cannot be turned off. */
  required?: boolean;
  disabled?: boolean;
}

/**
 * MultiToggle (design language §4): two to seven choices side by side, any of them on together
 * (Base UI ToggleGroup: each a pressed button to a screen reader). Each choice shows its state
 * (`stateWords`) as well as the accent, so its state never rests on colour alone.
 */
export function MultiToggle<V extends string>({ label, value, options, onChange, required = false, disabled,
  stateWords = ["on", "off"] }: MultiToggleProps<V>) {
  const { labelId, describedBy } = useFieldControl();
  return (
    <ToggleGroup
      multiple
      aria-label={labelId ? undefined : label}
      aria-labelledby={labelId}
      aria-describedby={describedBy}
      value={value}
      disabled={disabled}
      onValueChange={(next) => {
        if (required && next.length === 0) return;
        onChange(next as V[]);
      }}
      className="inline-flex w-fit flex-wrap overflow-hidden rounded-input border border-line-input"
    >
      {options.map((option) => {
        const on = value.includes(option.value);
        return (
          <Toggle
            key={option.value}
            value={option.value}
            className={cn(
              "inline-flex cursor-pointer items-baseline gap-1.5 border-0 border-l border-line-input bg-surface-input px-3 py-1.5 first:border-l-0",
              "font-sans text-sm text-text",
              "focus-visible:relative focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
              "data-pressed:bg-accent data-pressed:text-on-accent",
              "data-disabled:cursor-not-allowed data-disabled:opacity-60",
            )}
          >
            {option.label}
            <span className="text-xs" aria-hidden="true">{on ? stateWords[0] : stateWords[1]}</span>
          </Toggle>
        );
      })}
    </ToggleGroup>
  );
}
