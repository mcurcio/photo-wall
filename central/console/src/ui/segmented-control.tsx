import { Radio } from "@base-ui/react/radio";
import { RadioGroup } from "@base-ui/react/radio-group";

import { cn } from "./cn";
import { useFieldControl } from "./field";

export interface SegmentedControlProps<V extends string> {
  /** The choice's visible name and the group's accessible one. */
  label: string;
  value: V;
  /** Two to five exclusive choices. */
  options: readonly { value: V; label: string }[];
  onChange: (value: V) => void;
  disabled?: boolean;
}

/**
 * SegmentedControl (design language §4): two to five exclusive choices side by side (Base UI
 * RadioGroup: a radio group to a screen reader, arrow keys move the choice). The chosen segment
 * is drawn in the accent, as a selection.
 */
export function SegmentedControl<V extends string>({ label, value, options, onChange, disabled }: SegmentedControlProps<V>) {
  // Inside a Field the Field's label names the group (and is the one shown), and its help
  // describes it; alone, the control shows and carries its own label.
  const { labelId, describedBy } = useFieldControl();
  return (
    <div className="flex min-w-0 flex-col gap-1 text-sm text-label">
      {labelId ? null : <span aria-hidden="true">{label}</span>}
      <RadioGroup
        aria-label={labelId ? undefined : label}
        aria-labelledby={labelId}
        aria-describedby={describedBy}
        value={value}
        disabled={disabled}
        onValueChange={(next) => onChange(next as V)}
        className="inline-flex w-fit overflow-hidden rounded-input border border-line-input"
      >
        {options.map((option) => (
          <Radio.Root
            key={option.value}
            value={option.value}
            className={cn(
              "cursor-pointer border-0 border-l border-line-input bg-surface-input px-3 py-1.5 first:border-l-0",
              "font-sans text-sm text-text",
              "focus-visible:relative focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
              "data-checked:bg-accent data-checked:text-on-accent",
              "data-disabled:cursor-not-allowed data-disabled:opacity-60",
            )}
          >
            {option.label}
          </Radio.Root>
        ))}
      </RadioGroup>
    </div>
  );
}
