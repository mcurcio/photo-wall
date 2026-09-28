import React from "react";

import { CheckIcon } from "../icons.jsx";

/**
 * Where the operator is in a flow (flow design §6 frozen surface; presentational).
 *
 * Wide, an ordered list of the steps: the steps before the current one are buttons
 * (named by their label) that call `onStep(id)`; the current step carries
 * `aria-current="step"`; later steps are text. Under 850 px the list collapses to one
 * line, "Step 2 of 5 · Frames" (index.css swaps the two by the breakpoint, with
 * `display`, so only one is ever in the accessibility tree).
 *
 * @param {{steps: ReadonlyArray<{id: string, label: string}>, current: string,
 *          onStep?: (id: string) => void}} props
 */
export function Stepper({ steps, current, onStep }) {
  const index = Math.max(0, steps.findIndex((step) => step.id === current));
  return (
    <nav className="stepper" aria-label="Steps">
      <ol className="stepper__list">
        {steps.map((step, position) => {
          const state = position < index ? "done" : position === index ? "current" : "todo";
          const mark = (
            <span className="stepper__mark" aria-hidden="true">
              {state === "done" ? <CheckIcon /> : position + 1}
            </span>
          );
          return (
            <li
              key={step.id}
              className={`stepper__item stepper__item--${state}`}
              aria-current={state === "current" ? "step" : undefined}
            >
              {state === "done" && onStep !== undefined ? (
                <button type="button" className="stepper__link" onClick={() => onStep(step.id)}>
                  {mark}
                  <span>{step.label}</span>
                </button>
              ) : (
                <span className="stepper__text">
                  {mark}
                  <span>{step.label}</span>
                </span>
              )}
            </li>
          );
        })}
      </ol>
      <p className="stepper__compact">{`Step ${index + 1} of ${steps.length} · ${steps[index]?.label ?? ""}`}</p>
    </nav>
  );
}
