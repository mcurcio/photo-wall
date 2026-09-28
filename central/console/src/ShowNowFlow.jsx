import React, { useEffect, useId, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { activationProblems } from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { PriorityField, useProblems } from "./Field.jsx";
import { Advanced } from "./flow/Advanced.jsx";
import { CheckAnswers, NotChosen } from "./flow/CheckAnswers.jsx";
import { FlowFrame } from "./flow/FlowFrame.jsx";
import { inStepOrder } from "./flow/steps.js";
import { useFlowDraft } from "./flow/useFlowDraft.js";
import { useFlowInstance } from "./flow/useFlowInstance.js";
import { ScenePicker } from "./ScenePicker.jsx";
import {
  editActivation,
  REPEAT_LABELS,
  seedShowNow,
  shownPriority,
  SHOW_ADVANCED_FIELDS,
  SHOW_FIELD_STEP,
  SHOW_KEYS,
  SHOW_STEPS,
} from "./showNowModel.js";
import {
  coveringPriority,
  coveringRuns,
  cycleWording,
  protectorOf,
  sceneFrames,
  sceneProtectedFrames,
  underneathSentence,
} from "./showState.js";
import { FrameChips } from "./TargetPicker.jsx";
import { useMutate } from "./useMutate.js";

const EMPTY = {};

// Each step's heading: the one question it asks.
const HEADINGS = { scene: "Which Scene?", review: "Check and show it now" };

// Said while the last activation's outcome is unknown (its key is kept).
const UNKNOWN_ACTIVATION =
  "Outcome unknown. Try again; it will not start twice. " +
  "Changing the form makes this a new activation.";

/**
 * Show now (flow design §7 J7), on the Now showing page: Scene → Review, then the
 * activation (POST `/v1/operator/activations`), whose synchronous Admission is said at
 * the moment, from served facts only (slice 3 §11).
 *
 * THE CONTAINER. It owns the flow's one draft (`useFlowDraft`; showNowModel.js has its
 * shape) and never unmounts (the shell keeps Show sections mounted, rule 2), so a step
 * or section change, a Wall visit, a poll or the sign-in overlay keep the draft and its
 * ACTIVATION KEY. The key is minted with the draft and again by every change the
 * operator makes (`editActivation`); an unknown outcome keeps it, so a retry cannot
 * start the Scene twice; a known outcome ends the flow (`finish`). The Scene step is
 * prefilled from the shell's `recentSceneId`: a clean draft follows it when it changes
 * (a Scene card's or the Scene flow's "Show now"); a dirty one is never replaced.
 *
 * PRIORITY defaults to showState.js `coveringPriority` of the Scene's frames, read from
 * the current snapshot until the operator sets one under Advanced. Review always shows
 * it; a priority below that default holds Advanced open with where the Run would stay
 * underneath (or, when the Scene protects frames a higher Run covers, that Central will
 * refuse it). Refusals keep slice 3's wording (`admissionSentence`).
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          recentSceneId: string|null,
 *          markDraft: (section: string, dirty: boolean) => void}} props
 */
export function ShowNowFlow({ snapshot, route, navigate, recentSceneId, markDraft }) {
  const definitions = snapshot?.runtime?.definitions ?? EMPTY;
  const draft = useFlowDraft(seedShowNow(recentSceneId, definitions));
  const value = draft.value ?? seedShowNow(null, EMPTY, () => "")();
  const mutate = useMutate();

  // The last outcome, held here only; null until an activation. An Admission is put in
  // words on each render, so the Run that refused it is named once served.
  const [outcome, setOutcome] = useState(
    /** @type {null|{unknown: true}|{text: string}|{asked: object, admission: object}} */ (null),
  );
  const [activating, setActivating] = useState(false);
  const newRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const outcomeRef = useRef(/** @type {HTMLDivElement|null} */ (null));
  const summaryRef = useRef(/** @type {HTMLDivElement|null} */ (null));
  const unknown = outcome !== null && "unknown" in outcome;

  const frames = useMemo(() => sceneFrames(definitions[value.sceneId]), [definitions, value.sceneId]);
  const covering = coveringPriority(snapshot, frames);
  // Whether a live Run covers the frames at all: one may itself have priority 0.
  const covered = coveringRuns(snapshot, frames).length > 0;
  const priority = shownPriority(value, covering);

  const problemList = useMemo(
    () =>
      inStepOrder(
        activationProblems({ sceneId: value.sceneId, priority }),
        SHOW_FIELD_STEP,
        SHOW_STEPS,
      ),
    [value.sceneId, priority],
  );
  const problems = useProblems(problemList);
  // One confirmation for this flow: discarding its draft (its `after` runs once done).
  const confirm = useConfirm(
    () => newRef.current?.focus(),
    (_result, request) => request.after?.(),
  );

  const flow = useFlowInstance({
    section: "now",
    draft,
    route,
    navigate,
    markDraft,
    keys: SHOW_KEYS,
    availability: () => "ok",
    steps: SHOW_STEPS,
    fieldStep: SHOW_FIELD_STEP,
    advancedFields: SHOW_ADVANCED_FIELDS,
    problemList,
    problems,
    confirm,
    onOpened: () => setOutcome(null),
  });
  const { step, focus } = flow;

  // A clean draft follows the Scene the operator last saved or picked; a dirty one, or
  // one whose outcome is unknown (its key must be kept for the retry), is never replaced.
  useEffect(() => {
    if (
      draft.key !== null &&
      !draft.dirty &&
      !unknown &&
      recentSceneId !== null &&
      definitions[recentSceneId] !== undefined &&
      value.sceneId !== recentSceneId
    ) {
      draft.reseed();
    }
    // Only a new recent Scene asks for this; the rest is read as it is then.
  }, [recentSceneId]);

  const edit = (changes, field = null) => {
    draft.patch(editActivation(changes));
    if (field !== null) {
      problems.touch(field);
    }
  };

  const activate = async () => {
    if (activating || !flow.checkAll(() => summaryRef.current)) {
      return;
    }
    const asked = { sceneId: value.sceneId, priority: Number(priority) };
    const body = {
      scene_id: asked.sceneId,
      activation_id: value.activationKey,
      priority: asked.priority,
      repeat: value.repeat,
    };
    setActivating(true);
    setOutcome(null);
    let result = null;
    try {
      result = await mutate(() => apiWrite("/v1/operator/activations", { method: "POST", body }));
    } catch {
      result = null;
    } finally {
      setActivating(false);
    }
    if (result === null || result.status >= 500) {
      setOutcome({ unknown: true }); // the draft, and its key, stay for the retry
      return;
    }
    if (result.status === 401) {
      // Refused before it reached the Runtime: nothing started. The session has ended
      // (the refresh after this write shows sign-in), and the draft stays for after it.
      setOutcome({ text: "Not started: the session ended. Sign in again, then activate." });
      return;
    }
    setOutcome(
      result.ok
        ? { asked, admission: result.data }
        : { text: `Not started: ${result.error ?? `HTTP ${result.status}`}.` },
    );
    flow.finish(() => outcomeRef.current);
  };

  const priorityValid = !problemList.some((problem) => problem.field === "priority");
  const stepProps = { value, problems, edit, snapshot, frames };
  const views = {
    scene: () => <SceneStep {...stepProps} definitions={definitions} />,
    review: () => (
      <ReviewStep
        {...stepProps}
        definitions={definitions}
        covering={covering}
        covered={covered}
        priority={priority}
        priorityValid={priorityValid}
        advanced={{
          open: focus.advancedOpen("review"),
          onToggle: () => focus.toggleAdvanced("review"),
        }}
        onChange={flow.openField}
      />
    ),
  };

  return (
    <FlowFrame
      className="show-now"
      flow={flow}
      draft={draft}
      keys={SHOW_KEYS}
      noun="activation"
      sectionLabel="Now showing"
      confirm={confirm}
      problems={problems}
      savedRef={outcomeRef}
      newRef={newRef}
      summaryRef={summaryRef}
      said={
        // The SYNCHRONOUS activation outcome — shown at the moment, only after an
        // activation (null until then).
        outcome !== null && (
          <p className="run-control__outcome flow__said" role="status" aria-label="Activation outcome">
            {unknown
              ? UNKNOWN_ACTIVATION
              : "text" in outcome
                ? outcome.text
                : admissionSentence(snapshot, outcome.asked, outcome.admission)}
          </p>
        )
      }
      newLabel="Show now"
      replace
      title="Show a Scene now"
      steps={SHOW_STEPS}
      formLabel="Activate a Scene"
      heading={HEADINGS[step]}
      busy={activating}
      onWrite={activate}
      writeLabel="Activate now"
      problemsLabel="Activation problems"
    >
      {step !== null && views[step]()}
    </FlowFrame>
  );
}

/** The Scene's frames with their health, or why there are none. */
function SceneFramesValue({ snapshot, frames }) {
  return frames.length > 0 ? <FrameChips snapshot={snapshot} frameIds={frames} /> : "none";
}

/** Step 1, Scene: the one Scene picker (slice 3 §3), then the frames it reaches. */
function SceneStep({ value, problems, edit, snapshot, frames, definitions }) {
  return (
    <>
      <ScenePicker
        id={problems.idFor("scene")}
        label="Scene to activate"
        reason={problems.reasonFor("scene")}
        definitions={definitions}
        value={value.sceneId}
        onChange={(sceneId) => edit({ sceneId }, "scene")}
      />
      {value.sceneId !== "" && (
        <dl className="record">
          <dt>Frames</dt>
          <dd>
            <SceneFramesValue snapshot={snapshot} frames={frames} />
          </dd>
        </dl>
      )}
    </>
  );
}

/**
 * Review's words for the priority: its value, and where a default comes from. `covered`
 * says whether any live Run covers the frames (a covering Run may have priority 0).
 */
function priorityWords(value, priority, covering, covered) {
  if (value.priority !== null) {
    return Number(priority) < covering
      ? `${priority} (below ${covering}, the highest Run on its frames)`
      : String(priority);
  }
  return !covered
    ? "0 (the default: no Run covers its frames)"
    : `${priority} (the default: the highest Run on its frames has priority ${covering}; ` +
        "at equal priority the newer Run shows on top)";
}

/**
 * Step 2, Review: a check-answers list of every value, the advanced ones included;
 * under Advanced, "Activation priority" and "If it is already running". A priority
 * below the default holds Advanced open and says where the Run stays underneath, or,
 * for a Scene that protects those frames, that Central will refuse it.
 */
function ReviewStep({
  value,
  problems,
  edit,
  snapshot,
  frames,
  definitions,
  covering,
  covered,
  priority,
  priorityValid,
  advanced,
  onChange,
}) {
  const repeatName = useId();
  const hintId = useId();
  const underneath = priorityValid
    ? underneathSentence(
        snapshot,
        frames,
        Number(priority),
        sceneProtectedFrames(definitions[value.sceneId]),
      )
    : null;
  const restartsFor = cycleWording(definitions[value.sceneId]) ?? "plays until finished";
  return (
    <>
      <CheckAnswers
        onChange={onChange}
        rows={[
          { label: "Scene", field: "scene", value: value.sceneId === "" ? <NotChosen /> : value.sceneId },
          { label: "Frames", value: <SceneFramesValue snapshot={snapshot} frames={frames} /> },
          { label: "Priority", field: "priority", value: priorityWords(value, priority, covering, covered) },
          { label: "If it is already running", field: "repeat", value: REPEAT_LABELS[value.repeat] },
        ]}
      />
      <Advanced
        summary={`Priority ${priority} · ${REPEAT_LABELS[value.repeat]}`}
        open={advanced.open}
        lockedOpen={underneath !== null}
        onToggle={advanced.onToggle}
      >
        <PriorityField
          label="Activation priority"
          problems={problems}
          value={priority}
          onChange={(next) => edit({ priority: next }, "priority")}
        />
        {underneath !== null && (
          <p className="show-now__underneath notice notice--warn" role="status">
            {underneath}
          </p>
        )}
        <fieldset
          id={problems.idFor("repeat")}
          className="run-control__repeat"
          aria-label="If it is already running"
        >
          <legend>If it is already running</legend>
          {Object.entries(REPEAT_LABELS).map(([repeat, label]) => (
            <label key={repeat} className="run-control__repeat-option">
              <input
                type="radio"
                name={repeatName}
                aria-describedby={repeat === "restart" ? hintId : undefined}
                checked={value.repeat === repeat}
                onChange={() => edit({ repeat })}
              />
              {label}
            </label>
          ))}
          {value.repeat === "restart" && (
            <p id={hintId} className="field__hint">
              {"Ends the current Run and starts a new one now. A restarted Run has no Program " +
                `end; it ${restartsFor}.`}
            </p>
          )}
        </fieldset>
      </Advanced>
    </>
  );
}

/**
 * An Admission in words (slice 3 §11), from served facts only: a refusal names the
 * Run Central says refused it (`blocking_run_id`) once the snapshot serves it.
 *
 * @param {object|null} snapshot the current snapshot
 * @param {{sceneId: string, priority: number}} asked
 * @param {{status: string, reason: string|null, blocking_run_id?: string|null}} admission
 * @returns {string}
 */
function admissionSentence(snapshot, asked, admission) {
  switch (admission?.status) {
    case "admitted":
      return `Started: Central admitted a Run of ${asked.sceneId}.`;
    case "ignored":
      return `Not started: ${asked.sceneId} is already running, left as is.`;
    case "queued":
      return `Queued: ${asked.sceneId} starts when its running Run ends.`;
    case "expired":
      return "Not started: it expired before it could start.";
    case "rejected":
      break;
    default:
      return UNKNOWN_ACTIVATION;
  }
  const { run, frames } = protectorOf(snapshot, admission);
  if (admission.reason === "protected_frames") {
    return run !== null
      ? `Not started: ${frames} is protected by the Run of ${run.scene_id}.`
      : "Not started: its frames are protected by another Run.";
  }
  if (admission.reason === "protection_not_visible") {
    return run !== null
      ? `Not started: this Scene protects frames that ${run.scene_id}'s Run ` +
          `(priority ${run.priority}) covers; use priority at least ${run.priority}.`
      : "Not started: this Scene protects frames that a higher-priority Run covers.";
  }
  if (admission.reason === "queue_full") {
    return "Not started: 16 activations are already waiting.";
  }
  return `Not started: ${admission.reason ?? "refused"}.`;
}
