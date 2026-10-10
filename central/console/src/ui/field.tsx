import * as React from "react";

import { cn } from "./cn";

/*
 * Form fields: a label over one native control, in the console's input style. The label (and
 * only the label) is the control's accessible name; a `hint` below it is its description.
 */

const control = cn(
  "min-w-0 rounded-input border border-line-input bg-surface-input px-3 py-1.5",
  "font-sans text-sm text-text",
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
  "disabled:cursor-not-allowed disabled:opacity-60",
);

/** A field's control id, and its hint's id (the control's `aria-describedby`) if it has one. */
function useField(hint?: string) {
  const id = React.useId();
  return { id, describedBy: hint ? `${id}-hint` : undefined };
}

interface FieldLabelProps {
  id: string;
  label: string;
  hint?: string;
  children: React.ReactNode;
}

function FieldLabel({ id, label, hint, children }: FieldLabelProps) {
  return (
    <div className="flex min-w-0 flex-col gap-1 text-sm text-label">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint ? <span id={`${id}-hint`} className="text-xs text-muted">{hint}</span> : null}
    </div>
  );
}

export interface NumberFieldProps {
  label: string;
  value: number | string;
  onValueChange: (value: string) => void;
  min?: number;
  max?: number;
  step?: number | "any";
  /** A unit or a word on what is typed ("px", "%"): the field's description. */
  hint?: string;
  disabled?: boolean;
  autoFocus?: boolean;
}

/** NumberField: a labelled number input; the caller parses and validates what is typed. */
export function NumberField({ label, value, onValueChange, min, max, step, hint, disabled, autoFocus }: NumberFieldProps) {
  const { id, describedBy } = useField(hint);
  return (
    <FieldLabel id={id} label={label} hint={hint}>
      <input
        id={id}
        aria-describedby={describedBy}
        type="number"
        className={cn(control, "w-28")}
        value={value}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        autoFocus={autoFocus}
        onChange={(event) => onValueChange(event.target.value)}
      />
    </FieldLabel>
  );
}

export interface SelectFieldProps {
  label: string;
  value: string;
  options: readonly { value: string; label: string }[];
  onValueChange: (value: string) => void;
  disabled?: boolean;
}

/** SelectField: a labelled native select. */
export function SelectField({ label, value, options, onValueChange, disabled }: SelectFieldProps) {
  const { id } = useField();
  return (
    <FieldLabel id={id} label={label}>
      <select
        id={id}
        className={cn(control, "w-48")}
        value={value}
        disabled={disabled}
        onChange={(event) => onValueChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </FieldLabel>
  );
}

export interface RangeFieldProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onValueChange: (value: number) => void;
  /** How the value reads beside the slider ("120 %"). */
  valueText: string;
  hint?: string;
  disabled?: boolean;
}

/** RangeField: a labelled slider with its value in words beside it. */
export function RangeField({ label, value, min, max, step, onValueChange, valueText, hint, disabled }: RangeFieldProps) {
  const { id, describedBy } = useField(hint);
  return (
    <FieldLabel id={id} label={label} hint={hint}>
      <span className="flex min-w-0 items-center gap-3">
        <input
          id={id}
          aria-describedby={describedBy}
          type="range"
          className="w-64 max-w-full accent-accent"
          value={value}
          min={min}
          max={max}
          step={step}
          disabled={disabled}
          aria-valuetext={valueText}
          onChange={(event) => onValueChange(Number(event.target.value))}
        />
        <span aria-hidden="true" className="text-text tabular-nums">{valueText}</span>
      </span>
    </FieldLabel>
  );
}

export interface CheckboxFieldProps {
  label: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
}

/** CheckboxField: a checkbox with its label beside it. */
export function CheckboxField({ label, checked, onCheckedChange, disabled }: CheckboxFieldProps) {
  return (
    <label className="inline-flex items-center gap-2 text-sm text-label">
      <input
        type="checkbox"
        className="accent-accent"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onCheckedChange(event.target.checked)}
      />
      {label}
    </label>
  );
}
