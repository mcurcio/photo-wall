import { useCallback, useLayoutEffect, useRef, useState } from "react";

import { viewMatches } from "./instance.js";
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
 * `focusStep(target)` asks the same for the step heading of view `target`, and
 * `focusWhenShown(find, target?)` for any element `find()` returns (a problem summary,
 * a saved message). A newer request replaces an older one.
 *
 * LIFETIME. A request is made FOR a view (flow/instance.js VIEWS; by default the one
 * shown when it is made). It waits while that view is shown, while the view it was
 * made in is still shown (the move has not landed yet) and while the flow is between
 * views; the first other view drops it. So a request never outlives the route change
 * or place it was made for: it cannot fire later, on a step reached another way.
 *
 * Attach `rootRef` to the flow's root element: `focusStep` looks for the step heading
 * (`data-flow-step-heading`, StepForm) inside it.
 *
 * Which steps have their Advanced open is kept here, per step id, by the flow
 * container that never unmounts; `reset()` closes them all (a new draft). A pending
 * focus request survives it, so an action that opens an instance can still ask for
 * the new step's heading.
 *
 * @param {{view: string|null, step: string|null, stepView: (step: string) => string,
 *          fieldStep: Readonly<Record<string, string>>,
 *          advancedFields: ReadonlySet<string>, goToStep: (step: string) => void,
 *          controlFor: (field: string) => HTMLElement|null}} options
 *   `view` is the view shown; `stepView(step)` the view of the open draft's step.
 */
export function useFlowFocus({ view, step, stepView, fieldStep, advancedFields, goToStep, controlFor }) {
  const rootRef = useRef(/** @type {HTMLElement|null} */ (null));
  const [advanced, setAdvanced] = useState(() => new Set());
  // The pending request: what to focus, the view it is for and the view it was made in.
  const [request, setRequest] = useState(
    /** @type {{find: () => HTMLElement|null, target: string|null, madeAt: string|null}|null} */ (
      null
    ),
  );
  const viewRef = useRef(view);
  viewRef.current = view;
  const controlForRef = useRef(controlFor);
  controlForRef.current = controlFor;

  const focusWhenShown = useCallback(
    (find, target = viewRef.current) => setRequest({ find, target, madeAt: viewRef.current }),
    [],
  );

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
      focusWhenShown(
        () => controlForRef.current(field),
        target === step ? viewRef.current : stepView(target),
      );
      if (target !== step) {
        goToStep(target);
      }
    },
    [fieldStep, advancedFields, openAdvanced, focusWhenShown, step, stepView, goToStep],
  );

  const focusStep = useCallback(
    (target) =>
      focusWhenShown(
        () => rootRef.current?.querySelector("[data-flow-step-heading]") ?? null,
        target,
      ),
    [focusWhenShown],
  );

  const reset = useCallback(() => setAdvanced(new Set()), []);

  useLayoutEffect(() => {
    if (request === null || view === null) {
      return; // nothing asked, or between views
    }
    if (request.target !== null && viewMatches(request.target, view)) {
      const element = request.find();
      if (element != null && element.closest("[hidden]") === null) {
        element.focus();
        setRequest(null);
      }
    } else if (view !== request.madeAt) {
      setRequest(null); // another view: the request's moment has passed
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
