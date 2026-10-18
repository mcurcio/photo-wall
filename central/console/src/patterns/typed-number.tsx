import { useEffect, useState } from "react";

import { Field, NumberInput } from "../ui/field";
import { type Unit, unitWords } from "./units";

export interface TypedNumberProps {
  label: string;
  /** One line under the label: what to type, where to read it. */
  help?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit: Unit;
  /** `min` itself is refused (a lux above 0). */
  exclusiveMin?: boolean;
  disabled?: boolean;
  /** Called with each typed value inside the range, never with one outside it. */
  onChange(next: number): void;
}

/** Whether `typed` is a number in the range. */
function inRange(typed: string, min: number, max: number, exclusiveMin: boolean): boolean {
  const number = Number(typed);
  return typed.trim() !== "" && Number.isFinite(number) && (exclusiveMin ? number > min : number >= min) && number <= max;
}

/**
 * TypedNumber: a measured value typed in a Field (a light meter's lux, a place in millimetres). The
 * text stays as typed and is applied once it is a number in the range; until then the Field says
 * the range and the value keeps what it was. A value changed elsewhere (a Restore, a Revert)
 * replaces what was typed.
 */
export function TypedNumber({ label, help, value, min, max, step, unit, exclusiveMin = false, disabled = false,
  onChange }: TypedNumberProps) {
  const [text, setText] = useState(String(value));
  useEffect(() => { setText((typed) => (Number(typed) === value ? typed : String(value))); }, [value]);
  const range = `${exclusiveMin ? "Above" : "From"} ${unitWords(min, unit)} to ${unitWords(max, unit)}`;
  return (
    <Field label={label} help={help} error={inRange(text, min, max, exclusiveMin) ? undefined : range}>
      <NumberInput value={text} min={min} max={max} step={step} disabled={disabled}
        onChange={(raw) => {
          setText(raw);
          if (inRange(raw, min, max, exclusiveMin)) onChange(Number(raw));
        }} />
    </Field>
  );
}
