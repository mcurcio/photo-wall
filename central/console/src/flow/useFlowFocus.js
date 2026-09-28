import { useCallback, useLayoutEffect, useRef, useState } from "react";

import { fieldKind, stepOfField } from "./steps.js";

/**
 * A flow's step focus and its Advanced disclosures (flow design §7 "Review").
 *
 * `openField(field)` routes a problem to its field: it opens the owning step's
 * Advanced when the field lives there (`advancedFields`), moves to the owning step
 * (`goToStep`, from `FIELD_STEP`) and leaves a ONE-SHOT focus request that is spent
 * once the field is in the page: after each commit the request looks for the control
 * (`controlFor(field)`), outside any `hidden` ancestor, focuses it and is cleared, so
 * focus reaches a field whose step or disclosure mounts later, and never twice.
 * `focusStep()` asks the same for the current step's heading after a step change, and
 * `focusWhenShown(find)` for any element `find()` returns (a problem summary, a
 * saved message). A newer request replaces an older one.
 *
 * Attach `rootRef` to the flow's root element: `focusStep` looks for the step heading
 * (`data-flow-step-heading`, StepForm) inside it.
 *
 * Which steps have their Advanced open is kept here, per step id, by the flow
 * container that never unmounts; `reset()` closes them all (a new draft). A pending
 * focus request survives it, so an action that opens an instance can still ask for
 * the new step's heading.
 *
 * @param {{step: string|null, fieldStep: Readonly<Record<string, string>>,
 *          advancedFields: ReadonlySet<string>, goToStep: (step: string) => void,
 *          controlFor: (field: string) => HTMLElement|null}} options
 */
export function useFlowFocus({ step, fieldStep, advancedFields, goToStep, controlFor }) {
  const rootRef = useRef(/** @type {HTMLElement|null} */ (null));
  const [advanced, setAdvanced] = useState(() => new Set());
  // The pending request: a function that finds the element to focus, or null.
  const [request, setRequest] = useState(
    /** @type {{find: () => HTMLElement|null}|null} */ (null),
  );
  const controlForRef = useRef(controlFor);
  controlForRef.current = controlFor;

  const focusWhenShown = useCallback((find) => setRequest({ find }), []);

  const openAdvanced = useCallback((stepId) => {
    setAdvanced((previous) => (previous.has(stepId) ? previous : new Set(previous).add(stepId)));
  }, []);

  const toggleAdvanced = useCallback((stepId) => {
    setAdvanced((previous) => {
      const next = new Set(previous);
      if (!next.delete(stepId)) {
        next.add(stepId);
      }
      return next;
    });
  }, []);

  const openField = useCallback(
    (field) => {
      const target = stepOfField(fieldStep, field);
      if (target === undefined) {
        return;
      }
      if (advancedFields.has(fieldKind(field))) {
        openAdvanced(target);
      }
      focusWhenShown(() => controlForRef.current(field));
      if (target !== step) {
        goToStep(target);
      }
    },
    [fieldStep, advancedFields, openAdvanced, focusWhenShown, step, goToStep],
  );

  const focusStep = useCallback(
    () => focusWhenShown(() => rootRef.current?.querySelector("[data-flow-step-heading]") ?? null),
    [focusWhenShown],
  );

  const reset = useCallback(() => setAdvanced(new Set()), []);

  useLayoutEffect(() => {
    if (request === null) {
      return;
    }
    const element = request.find();
    if (element != null && element.closest("[hidden]") === null) {
      element.focus();
      setRequest(null);
    }
  });

  return {
    rootRef,
    advancedOpen: (stepId) => advanced.has(stepId),
    toggleAdvanced,
    openField,
    focusStep,
    focusWhenShown,
    reset,
  };
}
