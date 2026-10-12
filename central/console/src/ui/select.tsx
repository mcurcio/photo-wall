import { cn } from "./cn";
import { controlClass, useFieldControl } from "./field";

export interface SelectProps<V extends string> {
  value: V | null;
  options: readonly { value: V; label: string }[];
  onChange: (value: V) => void;
  /** Shown while nothing is chosen. */
  placeholder?: string;
  disabled?: boolean;
  /** "fixed" (the default): one width for every Select, so a column of them lines up; "fit":
   * as wide as its longest choice, up to its column, for choices whose names are long and alike
   * (two papers from one maker). */
  width?: "fixed" | "fit";
}

/**
 * Select (design language §4): one choice from a list too long for a SegmentedControl, inside a
 * Field (the Field names it). A native select, so every platform's own picker opens.
 */
export function Select<V extends string>({ value, options, onChange, placeholder, disabled, width = "fixed" }: SelectProps<V>) {
  const { id, describedBy } = useFieldControl();
  return (
    <select
      id={id}
      aria-describedby={describedBy}
      className={cn(controlClass, width === "fit" ? "w-auto max-w-full self-start" : "w-48")}
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
