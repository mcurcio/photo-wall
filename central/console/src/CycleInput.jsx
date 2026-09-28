import React from "react";

import { Field } from "./Field.jsx";

/**
 * The one cycle-and-loop input (pass 2 slice 3 §4), in both authoring modes:
 * "Seconds per cycle" (`cycle_seconds`) and "Keep playing until the Program
 * ends" (`loop`). Without `loop`, a Run asks to finish at the end of its first
 * cycle (central/runtime.py `_process`), so a Program Scene would play once.
 *
 * @param {{problems: ReturnType<typeof import("./Field.jsx").useProblems>,
 *          seconds: string|number, onSeconds: (value: string) => void,
 *          loop: boolean, onLoop: (value: boolean) => void}} props
 */
export function CycleInput({ problems, seconds, onSeconds, loop, onLoop }) {
  const loopId = problems.idFor("loop");
  return (
    <>
      <Field
        id={problems.idFor("cycle")}
        label="Seconds per cycle"
        reason={problems.reasonFor("cycle")}
      >
        {(props) => (
          <input
            {...props}
            type="number"
            min="1"
            value={seconds}
            onChange={(event) => {
              onSeconds(event.target.value);
              problems.touch("cycle");
            }}
          />
        )}
      </Field>
      <div className="field field--check">
        <label className="field__label" htmlFor={loopId}>
          <input
            id={loopId}
            type="checkbox"
            checked={loop}
            aria-describedby={`${loopId}-hint`}
            onChange={(event) => onLoop(event.target.checked)}
          />
          Keep playing until the Program ends
        </label>
        <p id={`${loopId}-hint`} className="field__hint">
          Without a Program, it plays until you Finish or Cancel it. It stops at the end of
          the cycle running when the Program ends, so it can overrun by up to one cycle.
        </p>
      </div>
    </>
  );
}
