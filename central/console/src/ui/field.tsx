import * as React from "react";

import { cn } from "./cn";

/*
 * Field (design language §4): one control with its label, a help line and an error. The Field
 * owns the wiring: its label names the control, and the help and the error describe it
 * (`aria-describedby`). The control is the Field's one child (Slider, Select, Switch,
 * NumberInput), which reads the wiring from the Field's context, as Base UI's Field does for
 * its own controls; these are native inputs, so the console keeps a small context of its own.
 */

interface FieldWiring {
  id: string;
  describedBy: string | undefined;
  invalid: boolean;
}

const FieldContext = React.createContext<FieldWiring | null>(null);

/** The wiring of the Field a control sits in; a control outside a Field gets its own id. */
export function useFieldControl(): FieldWiring {
  const fallback = React.useId();
  return React.useContext(FieldContext) ?? { id: fallback, describedBy: undefined, invalid: false };
}

/** The look every text-like control shares (an input, a select). */
export const controlClass = cn(
  "min-w-0 rounded-input border border-line-input bg-surface-input px-3 py-1.5",
  "font-sans text-sm text-text",
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
  "disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-alarm",
);

export interface FieldProps {
  /** The control's visible name and its accessible one. */
  label: string;
  /** One plain sentence under the control. */
  help?: string;
  /** What is wrong with the value, in words; the control is marked invalid. */
  error?: string;
  /** The control. */
  children: React.ReactElement;
}

/** Field: a label over one control, with its help line and its error. */
export function Field({ label, help, error, children }: FieldProps) {
  const id = React.useId();
  const helpId = help ? `${id}-help` : null;
  const errorId = error ? `${id}-error` : null;
  const describedBy = [helpId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className="flex min-w-0 flex-col gap-1 text-sm text-label">
      <label htmlFor={id}>{label}</label>
      <FieldContext.Provider value={{ id, describedBy, invalid: Boolean(error) }}>{children}</FieldContext.Provider>
      {help ? <span id={helpId!} className="text-xs text-muted">{help}</span> : null}
      {error ? <span id={errorId!} className="text-xs font-medium text-text">{error}</span> : null}
    </div>
  );
}

export interface NumberInputProps {
  value: number | string;
  /** What is typed, as typed: the caller parses and validates it. */
  onChange: (value: string) => void;
  min?: number;
  max?: number;
  step?: number | "any";
  disabled?: boolean;
  autoFocus?: boolean;
}

/** NumberInput: a number typed in a Field. */
export function NumberInput({ value, onChange, min, max, step, disabled, autoFocus }: NumberInputProps) {
  const { id, describedBy, invalid } = useFieldControl();
  return (
    <input
      id={id}
      aria-describedby={describedBy}
      aria-invalid={invalid || undefined}
      type="number"
      className={cn(controlClass, "w-28")}
      value={value}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      autoFocus={autoFocus}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}
