import { cn } from "./cn";
import { controlClass, useFieldControl } from "./field";

export interface SelectProps<V extends string> {
  value: V | null;
  options: readonly { value: V; label: string }[];
  onChange: (value: V) => void;
  /** Shown while nothing is chosen. */
  placeholder?: string;
  disabled?: boolean;
}

/**
 * Select (design language §4): one choice from a list too long for a SegmentedControl, inside a
 * Field (the Field names it). A native select, so every platform's own picker opens.
 */
export function Select<V extends string>({ value, options, onChange, placeholder, disabled }: SelectProps<V>) {
  const { id, describedBy } = useFieldControl();
  return (
    <select
      id={id}
      aria-describedby={describedBy}
      className={cn(controlClass, "w-48")}
      value={value ?? ""}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value as V)}
    >
      {value === null && <option value="" disabled>{placeholder ?? "Choose…"}</option>}
      {options.map((option) => (
        <option key={option.value} value={option.value}>{option.label}</option>
      ))}
    </select>
  );
}
