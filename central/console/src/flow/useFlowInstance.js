import { createElement, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { fieldControl } from "../Field.jsx";
import { instancePlace, shownStep } from "./instance.js";
import { nextStep, previousStep, problemsOf, stepOfField } from "./steps.js";
import { useFlowFocus } from "./useFlowFocus.js";

/**
 * One flow's instance on its section (flow design §6, §7): which instance the route
 * names, what the section shows for it, and moving through its steps. Every flow
 * container (Scene, Source, Schedule, Show now) uses this, so the rules are written
 * once; the container keeps its own seed, effects, write and step views.
 *
 * ROUTES. `keys` (flow/instance.js `flowKeys`) map instances to routes. Steps move
 * with `replace`, so a flow is one history entry and browser Back leaves it with the
 * draft kept. A route naming the open instance at an unknown or inapplicable step is
 * replaced by the step it last showed, else its first step. A route naming another
 * instance opens it when the draft is clean or closed (`opening`), and otherwise
 * shows `blocked` ("Unsaved draft for X: Resume or Discard"); an edit whose record is
 * gone or cannot be authored shows `missing` or `unavailable` (`availability`).
 *
 * STEPS. Continue checks the current step's problems (`problemsOf`) and moves on;
 * after a Change link or a routed problem (`openField`) it returns toward the last
 * step (Review), stopping at a step that still has a problem. Back goes to the
 * previous step, or from the first step to the section. The container's final write
 * checks every problem itself and ends with `finish()`.
 *
 * OPENING. `start(key, event)` is the in-app way in (New, Edit): the open instance
 * resumes; another is opened, or, while the draft is dirty, the container's
 * confirmation (`confirm`, whose `onDone` must run `request.after`) asks to discard it
 * first. `onOpened(key)` runs when another instance opens, for the container's own
 * per-instance state. `markDraft(section, dirty)` tells the shell's sidebar.
 *
 * @template T
 * @param {{section: string,
 *          draft: ReturnType<typeof import("./useFlowDraft.js").useFlowDraft<T>>,
 *          route: import("../routes.js").Route|null,
 *          navigate: (route: import("../routes.js").Route, options?: {replace?: boolean}) => void,
 *          markDraft: (section: string, dirty: boolean) => void,
 *          keys: import("./instance.js").FlowKeys,
 *          availability: (key: string) => import("./instance.js").Availability,
 *          steps: ReadonlyArray<import("./steps.js").Step>,
 *          fieldStep: Readonly<Record<string, string>>,
 *          advancedFields: ReadonlySet<string>,
 *          problemList: ReadonlyArray<import("./steps.js").Problem>,
 *          problems: ReturnType<typeof import("../Field.jsx").useProblems>,
 *          confirm: ReturnType<typeof import("../ConfirmAction.jsx").useConfirm>,
 *          onOpened?: ((key: string) => void)|null}} options
 */
export function useFlowInstance({
  section,
  draft,
  route,
  navigate,
  markDraft,
  keys,
  availability,
  steps,
  fieldStep,
  advancedFields,
  problemList,
  problems,
  confirm,
  onOpened = null,
}) {
  const routeKey = keys.fromRoute(route);
  const place = instancePlace({
    routeKey,
    available: routeKey === null ? "ok" : availability(routeKey),
    draftKey: draft.key,
    dirty: draft.dirty,
  });
  const step = shownStep(place, steps, route?.step);
  const lastStepId = steps[steps.length - 1].id;

  const [returning, setReturning] = useState(false);
  // The step the open draft last showed, for Resume.
  const lastStepRef = useRef(/** @type {string|null} */ (null));

  const goToStep = useCallback(
    (stepId) => {
      if (draft.key !== null) {
        navigate(keys.toRoute(draft.key, stepId), { replace: true });
      }
    },
    [draft.key, keys, navigate],
  );
  const focus = useFlowFocus({
    step,
    fieldStep,
    advancedFields,
    goToStep,
    controlFor: (field) => fieldControl(problems.idFor(field)),
  });

  useEffect(() => {
    markDraft(section, draft.dirty);
  }, [markDraft, section, draft.dirty]);

  /** Open `key`; false when a dirty draft of another instance refused it. */
  const open = (key) => {
    const before = draft.key;
    if (draft.open(key) !== key) {
      return false;
    }
    if (before !== key) {
      problems.reset();
      focus.reset();
      setReturning(false);
      lastStepRef.current = null;
      onOpened?.(key);
    }
    return true;
  };

  useLayoutEffect(() => {
    if (place === "opening") {
      open(routeKey);
    } else if (place === "open" && step === null) {
      navigate(keys.toRoute(routeKey, lastStepRef.current ?? keys.firstStep(routeKey)), {
        replace: true,
      });
    }
  });

  if (step !== null) {
    lastStepRef.current = step;
  }

  /** Show `target` (a step route) and move focus to its step heading. */
  const enter = (target, options) => {
    navigate(target, options);
    focus.focusStep();
  };

  const firstRoute = (key) => keys.toRoute(key, keys.firstStep(key));
  const resumeRoute = () => keys.toRoute(draft.key, lastStepRef.current ?? keys.firstStep(draft.key));

  /** Close the draft, then `then()`. */
  const discard = (then) => {
    draft.discard();
    then?.();
  };

  /** The confirmation that discards the dirty draft, then runs `then()`. */
  const discardRequest = (then) => {
    const described = keys.describe(draft.key);
    return {
      key: `discard:${draft.key}`,
      title: "Discard your unsaved draft?",
      confirmLabel: "Discard draft",
      body: createElement("p", null, `Your unsaved changes to ${described} will be lost.`),
      run: async () => ({ state: "done", message: `Discarded the draft for ${described}.` }),
      after: () => discard(then),
    };
  };

  /**
   * The in-app way into instance `key` (New, Edit): its dirty draft resumes; otherwise
   * it opens at its first step, after asking to discard another instance's dirty draft.
   */
  const start = (key, event) => {
    if (draft.key === key && draft.dirty) {
      enter(resumeRoute());
    } else if (open(key)) {
      enter(firstRoute(key));
    } else {
      confirm.open(event ?? null, discardRequest(() => enter(firstRoute(key))));
    }
  };

  /** Show a step and move focus to its heading (the stepper). */
  const showStep = (stepId) => {
    goToStep(stepId);
    focus.focusStep();
  };

  /** Route a problem (a summary entry) or a Change link to its field. */
  const openField = (field) => {
    if (stepOfField(fieldStep, field) !== lastStepId) {
      setReturning(true);
    }
    focus.openField(field);
  };

  const onContinue = () => {
    const own = problemsOf(problemList, fieldStep, step);
    if (!problems.check(own)) {
      focus.openField(own[0].field);
      return;
    }
    const problemSteps = new Set(problemList.map((problem) => stepOfField(fieldStep, problem.field)));
    const next = nextStep(steps, step, { returning, problemSteps });
    if (next === lastStepId) {
      setReturning(false);
    }
    showStep(next);
  };

  const onBack = () => {
    const previous = previousStep(steps, step);
    if (previous === null) {
      navigate({ section }, { replace: true });
      return;
    }
    showStep(previous);
  };

  /** End the flow after its write: close the draft and return to the section. */
  const finish = () => {
    draft.discard();
    problems.reset();
    focus.reset();
    setReturning(false);
    lastStepRef.current = null;
    navigate({ section }, { replace: true });
  };

  return {
    place,
    step,
    routeKey,
    focus,
    start,
    resume: (options) => enter(resumeRoute(), options),
    discard,
    discardRequest,
    showStep,
    openField,
    onContinue,
    onBack,
    finish,
  };
}
