import { useRef } from "react";

import { useFieldControl } from "./field";

export interface SliderProps {
  value: number;
  min: number;
  max: number;
  step: number;
  /** Read after the value, as text beside the slider and to a screen reader ("150 %"). */
  unit: string;
  /** Every value while a pointer drags the slider (a live adjustment shows each one). */
  onPreview?: (value: number) => void;
  /** The value where the slider was let go, a key press's value, or a value set any other way. */
  onCommit: (value: number) => void;
  disabled?: boolean;
}

/**
 * Slider (design language §4): a value in a range, inside a Field, its value and unit shown as
 * text. It previews while a pointer drags it and commits where it is let go; a key press, or a
 * value set any other way, commits at once. The accent marks the control as one you can act on
 * (an allowed use of the accent).
 */
export function Slider({ value, min, max, step, unit, onPreview, onCommit, disabled }: SliderProps) {
  const { id, describedBy } = useFieldControl();
  const dragging = useRef(false);
  const text = `${value} ${unit}`;
  return (
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
        aria-valuetext={text}
        onPointerDown={() => {
          dragging.current = true;
        }}
        onChange={(event) => {
          const next = Number(event.target.value);
          if (onPreview && dragging.current) onPreview(next);
          else onCommit(next);
        }}
        onPointerUp={(event) => {
          dragging.current = false;
          onCommit(Number(event.currentTarget.value));
        }}
        onPointerCancel={(event) => {
          dragging.current = false;
          onCommit(Number(event.currentTarget.value));
        }}
        onKeyUp={(event) => onCommit(Number(event.currentTarget.value))}
      />
      <span aria-hidden="true" className="text-text tabular-nums">{text}</span>
    </span>
  );
}
