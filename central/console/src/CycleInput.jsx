import React from "react";

import { Field } from "./Field.jsx";

/**
 * The one cycle input (pass 2 slice 3 §4): "Seconds per cycle" (`cycle_seconds`,
 * field `cycle`). The Scene flow asks it on its Playback step.
 *
 * @param {{problems: ReturnType<typeof import("./Field.jsx").useProblems>,
 *          seconds: string|number, onSeconds: (value: string) => void}} props
 */
export function CycleField({ problems, seconds, onSeconds }) {
  return (
    <Field id={problems.idFor("cycle")} label="Seconds per cycle" reason={problems.reasonFor("cycle")}>
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
  );
}

/**
 * The one loop input (pass 2 slice 3 §4): "Keep playing until the Program ends"
 * (`loop`, field `loop`). Without `loop`, a Run asks to finish at the end of its
 * first cycle (central/runtime.py `_process`), so a Program Scene would play once.
 * The Scene flow keeps it under Playback's Advanced, on by default.
 *
 * @param {{problems: ReturnType<typeof import("./Field.jsx").useProblems>,
 *          loop: boolean, onLoop: (value: boolean) => void}} props
 */
export function LoopField({ problems, loop, onLoop }) {
  const loopId = problems.idFor("loop");
  return (
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
        Without a Program, it plays until you Finish or Cancel it. It stops at the end of the
        cycle running when the Program ends, so it can overrun by up to one cycle.
      </p>
    </div>
  );
}
