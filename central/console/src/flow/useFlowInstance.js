import { createElement, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { fieldControl } from "../Field.jsx";
import {
  editedId,
  flowView,
  hashNamesInstance,
  instancePlace,
  NEW_KEY,
  shownStep,
  stepView,
} from "./instance.js";
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
 * STEPS. Continue checks the current step's problems only (a scoped `problems.check`:
 * later steps show no reason before their own Continue) and moves on; after a Change
 * link or a routed problem (`openField`) it returns toward the last step (Review),
 * stopping at a step that still has a problem. Back goes to the previous step, or
 * from the first step to the section. A step before the current one is `answered`
 * once it has been shown in this draft (an edit's stored values answer them all), so
 * a typed URL that skips steps ticks none of them. The container's final write checks
 * every problem itself (unscoped) and ends with `finish()`.
 *
 * FINISH. After the write, `finish(focusAfter?)` closes the draft. Only if the location
 * still names this instance (read at that moment: the operator may have left while
 * the write was in flight) does it replace the flow's history entry with the section
 * (and focus `focusAfter()` there); otherwise history is left alone, and the flow's
 * entry, if Back or Forward reaches it, is replaced with the section instead of
 * opening a fresh draft (§6 History: Back after Save never re-enters a finished flow).
 *
 * OPENING. `start(key, event)` is the in-app way in (New, Edit): a dirty draft of the
 * same instance resumes; another is opened, or, while the draft is dirty, the
 * container's confirmation (`confirm`, whose `onDone` must run `request.after`) asks to
 * discard it first. `onOpened(key)` runs when another instance opens, for the
 * container's own per-instance state. `markDraft(section, dirty)` tells the sidebar.
 *
 * HAND-OFF (flow/handOff.js). With `handOff` (a pending hand-off addressed to this
 * section, from flow/useHandOff.js `useHandOffTo`), the flow runs inline for another
 * flow: when a hand-off with a new id arrives, the new instance opens as `start` opens
 * it (its dirty draft resumes); leaving from the first step (`onBack`, or `leave()`)
 * settles it with null, and `finish(focusAfter, result)` settles it with `result`. The
 * origin then shows its own draft, so the flow neither returns to its section nor asks
 * for `focusAfter`; when the origin no longer takes it, the flow ends as it would alone.
 *
 * FOCUS. Moving to a step asks for its heading, with a request that lives only while
 * that step is on its way (flow/useFlowFocus.js LIFETIME); an instance that is missing
 * or unavailable asks for nothing.
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
 *          onOpened?: ((key: string) => void)|null,
 *          handOff?: ReturnType<typeof import("./useHandOff.js").useHandOffTo>}} options
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
  handOff = null,
}) {
  const routeKey = keys.fromRoute(route);
  const place = instancePlace({
    routeKey,
    available: routeKey === null ? "ok" : availability(routeKey),
    draftKey: draft.key,
    dirty: draft.dirty,
  });
  const step = shownStep(place, steps, route?.step);
  const view = flowView({ shown: route?.section === section, place, routeKey, step });
  const lastStepId = steps[steps.length - 1].id;

  const [returning, setReturning] = useState(false);
  // The steps this draft has shown (Stepper ticks).
  const [visited, setVisited] = useState(() => new Set());
  // The step the open draft last showed, for Resume.
  const lastStepRef = useRef(/** @type {string|null} */ (null));
  // The history entry of a flow finished while it was not shown: {key, step}.
  const finishedRef = useRef(/** @type {{key: string, step: string|null}|null} */ (null));

  const goToStep = useCallback(
    (stepId) => {
      if (draft.key !== null) {
        navigate(keys.toRoute(draft.key, stepId), { replace: true });
      }
    },
    [draft.key, keys, navigate],
  );
  const draftStepView = useCallback((stepId) => stepView(draft.key, stepId), [draft.key]);
  const focus = useFlowFocus({
    view,
    step,
    stepView: draftStepView,
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
      setVisited(new Set());
      lastStepRef.current = null;
      onOpened?.(key);
    }
    return true;
  };

  useLayoutEffect(() => {
    if (place === "opening") {
      const finished = finishedRef.current;
      finishedRef.current = null;
      if (finished !== null && finished.key === routeKey && finished.step === route.step) {
        navigate({ section }, { replace: true }); // Back onto a finished flow's entry
      } else {
        open(routeKey);
      }
    } else if (place === "open" && step === null) {
      navigate(keys.toRoute(routeKey, lastStepRef.current ?? keys.firstStep(routeKey)), {
        replace: true,
      });
    } else if (step !== null && !visited.has(step)) {
      setVisited((previous) => new Set(previous).add(step));
    }
  });

  if (step !== null) {
    lastStepRef.current = step;
  }

  /** Show `target` (a step route) and, when it can show, move focus to its heading. */
  const enter = (target, options) => {
    navigate(target, options);
    const key = keys.fromRoute(target);
    if (key !== null && availability(key) === "ok") {
      focus.focusStep(stepView(key, target.step));
    }
  };

  const firstRoute = (key) => keys.toRoute(key, keys.firstStep(key));
  const resumeRoute = () => keys.toRoute(draft.key, lastStepRef.current ?? keys.firstStep(draft.key));

  /** Close the draft, then `then()`. */
  const discard = (then) => {
    draft.discard();
    then?.();
  };

  /**
   * The `blocked` notice's Discard: close the dirty draft, so the instance the route
   * names opens, and move focus to its step.
   */
  const discardForRoute = () => discard(() => focus.focusStep(stepView(routeKey)));

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
    finishedRef.current = null;
    confirm.setStatus(null);
    if (draft.key === key && draft.dirty) {
      enter(resumeRoute());
    } else if (open(key)) {
      enter(firstRoute(key));
    } else {
      confirm.open(event ?? null, discardRequest(() => enter(firstRoute(key))));
    }
  };

  // A hand-off addressed to this section opens the new instance, once per hand-off.
  const handedRef = useRef(/** @type {number|null} */ (null));
  const handOffId = handOff?.id ?? null;
  useEffect(() => {
    if (handOffId !== null && handOffId !== handedRef.current) {
      handedRef.current = handOffId;
      start(NEW_KEY);
    }
  });

  /**
   * Leave the flow without its write: back to a hand-off's origin (settled with null),
   * else to the section. The draft is kept.
   */
  const leave = () => {
    if (handOff === null || !handOff.settle(null, { show: true })) {
      navigate({ section }, { replace: true });
    }
  };

  /** Show a step and move focus to its heading (the stepper). */
  const showStep = (stepId) => {
    goToStep(stepId);
    focus.focusStep(draftStepView(stepId));
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
    if (!problems.check(own, { scoped: true })) {
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
      leave();
      return;
    }
    showStep(previous);
  };

  /**
   * End the flow after its write (see FINISH): true when the flow was still shown and
   * the section (or a hand-off's origin) replaced its entry. `result` is what a
   * hand-off's origin receives (see HAND-OFF).
   *
   * @param {(() => HTMLElement|null)|null} [focusAfter]
   * @param {object|null} [result]
   */
  const finish = (focusAfter = null, result = null) => {
    const key = draft.key;
    const here = hashNamesInstance(keys, window.location.hash, key);
    finishedRef.current = here ? null : { key, step: lastStepRef.current };
    draft.discard();
    problems.reset();
    focus.reset();
    setReturning(false);
    setVisited(new Set());
    lastStepRef.current = null;
    if (handOff !== null && handOff.settle(result, { show: here })) {
      return here;
    }
    if (here) {
      navigate({ section }, { replace: true });
      if (focusAfter !== null) {
        focus.focusWhenShown(focusAfter, flowView({ shown: true, place: "list", routeKey: null }));
      }
    }
    return here;
  };

  return {
    place,
    step,
    routeKey,
    focus,
    answered: (stepId) => editedId(draft.key) !== null || visited.has(stepId),
    start,
    resume: (options) => enter(resumeRoute(), options),
    discard,
    leave,
    discardForRoute,
    discardRequest,
    showStep,
    openField,
    onContinue,
    onBack,
    finish,
  };
}
