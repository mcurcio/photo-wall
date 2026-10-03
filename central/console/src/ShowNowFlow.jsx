import React, { useId, useMemo, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { activationProblems } from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { PriorityField, useProblems } from "./Field.jsx";
import { Advanced } from "./flow/Advanced.jsx";
import { CheckAnswers, NotChosen } from "./flow/CheckAnswers.jsx";
import { FlowFrame } from "./flow/FlowFrame.jsx";
import { OfferedScene } from "./flow/InstanceNotice.jsx";
import { inStepOrder } from "./flow/steps.js";
import { useFlowDraft } from "./flow/useFlowDraft.js";
import { useFlowInstance, useFlowRefs } from "./flow/useFlowInstance.js";
import { useFlowWrite } from "./flow/useFlowWrite.js";
import { useSceneHandOver } from "./flow/useSceneHandOver.js";
import { mediaNow, sourceState } from "./mediaHealth.js";
import { ScenePicker } from "./ScenePicker.jsx";
import {
  activationAnswer,
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
  protectingRuns,
  protectorOf,
  sceneFrames,
  sceneHasAuthoredMedia,
  sceneProtectedFrames,
  sceneSourceRefs,
  underneathSentence,
} from "./showState.js";
import { sourceName } from "./sourceNames.js";
import { FrameChips } from "./TargetPicker.jsx";
import { sourceRefreshMessage, useSourceRefresh } from "./useSourceRefresh.js";

const EMPTY = {};

// Each step's heading: the one question it asks.
const HEADINGS = { scene: "Which Scene?", review: "Check and show it now" };

// Said while the last activation's outcome is unknown (its key is kept).
const UNKNOWN_ACTIVATION =
  "Outcome unknown. Try again; it will not start twice. " +
  "Changing the form makes this a new activation.";

/**
 * Show now (flow design §7 J7), on the Now page: Scene → Review, then the
 * activation (POST `/v1/operator/activations`), whose synchronous Admission is said at
 * the moment, from served facts only (slice 3 §11).
 *
 * THE CONTAINER. It owns the flow's one draft (`useFlowDraft`; showNowModel.js has its
 * shape) and never unmounts (the shell keeps Show sections mounted, rule 2), so a step
 * or section change, a Wall visit, a poll or the sign-in overlay keep the draft and its
 * ACTIVATION KEY. The key is minted with the draft and again by every change the
 * operator makes (`editActivation`); an unknown outcome keeps it, so a retry cannot
 * start the Scene twice, and so does a refusal before the Runtime (the session, the
 * origin: showNowModel.js `activationAnswer`); a known outcome ends the flow
 * (`finish`). The Scene step is
 * prefilled from the shell's `recentScene`: a clean draft follows each hand-over
 * (a Scene card's or the Scene flow's "Show now"); a dirty one, or one whose outcome is
 * unknown, is kept, and its Scene step offers the handed-over Scene instead (the kit's
 * `useSceneHandOver`, shared with the Schedule flow).
 *
 * PRIORITY defaults to showState.js `coveringPriority` of the Scene's frames, read from
 * the current snapshot until the operator sets one under Advanced. Review always shows
 * it; a priority below that default holds Advanced open with where the Run would stay
 * underneath (or, when the Scene protects frames a higher Run covers, that Central will
 * refuse it). When a live Run protects any of the frames, Central refuses it at any
 * priority: Review says so, holds Advanced open, and never promises "on top".
 * Refusals keep slice 3's wording (`admissionSentence`).
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          recentScene: {sceneId: string, seq: number}|null,
 *          markDraft: (section: string, dirty: boolean) => void}} props
 */
export function ShowNowFlow({ snapshot, route, navigate, recentScene, markDraft }) {
  const definitions = snapshot?.runtime?.definitions ?? EMPTY;
  const draft = useFlowDraft(seedShowNow(recentScene?.sceneId ?? null, definitions));
  const value = draft.value ?? seedShowNow(null, EMPTY, () => "")();

  // The last outcome, held here only; null until an activation. An Admission is put in
  // words on each render, so the Run that refused it is named once served.
  const [outcome, setOutcome] = useState(
    /** @type {null|{unknown: true}|{text: string}|{asked: object, admission: object}} */ (null),
  );
  const refs = useFlowRefs();
  const unknown = outcome !== null && "unknown" in outcome;

  const scene = definitions[value.sceneId];
  const frames = useMemo(() => sceneFrames(scene), [scene]);
  const sourceRefs = useMemo(() => sceneSourceRefs(scene), [scene]);
  const authoredMedia = useMemo(() => sceneHasAuthoredMedia(scene), [scene]);
  const sourceRefresh = useSourceRefresh(JSON.stringify([value.sceneId, scene?.revision ?? null]));
  const covering = coveringPriority(snapshot, frames);
  // Whether a live Run covers the frames at all: one may itself have priority 0.
  const covered = coveringRuns(snapshot, frames).length > 0;
  // Whether one protects any of them: Central refuses the activation at any priority.
  const protectedNow = protectingRuns(snapshot, frames).length > 0;
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
    () => refs.newRef.current?.focus(),
    (_result, request) => request.after?.(),
  );

  const flow = useFlowInstance({
    section: "now",
    draft,
    route,
    navigate,
    markDraft,
    keys: SHOW_KEYS,
    refs,
    steps: SHOW_STEPS,
    fieldStep: SHOW_FIELD_STEP,
    advancedFields: SHOW_ADVANCED_FIELDS,
    problemList,
    problems,
    confirm,
    onOpened: () => {
      setOutcome(null);
      handOver.clear();
    },
  });
  const { step } = flow;
  const write = useFlowWrite({ draft, confirm, failure: "Not started" });

  // A clean draft follows the Scene the operator last saved or picked, while it is
  // stored; a dirty one, or one whose outcome is unknown (its key must be kept for the
  // retry), is kept and offered it instead.
  const handOver = useSceneHandOver({
    recentScene,
    draft,
    sceneId: value.sceneId,
    held: unknown,
    accepts: (sceneId) => definitions[sceneId] !== undefined,
    choose: (sceneId) => edit({ sceneId }, "scene"),
  });

  const edit = (changes, field = null) => {
    draft.patch(editActivation(changes));
    if (field !== null) {
      problems.touch(field);
    }
  };

  const activate = () =>
    write.send(flow, async (sent) => {
      const asked = { sceneId: value.sceneId, priority: Number(priority) };
      const body = {
        scene_id: asked.sceneId,
        activation_id: value.activationKey,
        priority: asked.priority,
        repeat: value.repeat,
      };
      setOutcome(null);
      const result = await sent.request(() =>
        apiWrite("/v1/operator/activations", { method: "POST", body }),
      );
      const answer = activationAnswer(result);
      if (answer.kind === "unknown") {
        setOutcome({ unknown: true }); // the draft, and its key, stay for the retry
        return;
      }
      if (answer.kind === "kept") {
        // Refused before it reached the Runtime (the session, the page's origin, the
        // request): nothing started, and the draft and its key stay for the retry.
        setOutcome({ text: answer.text });
        return;
      }
      setOutcome(
        result.ok
          ? { asked, admission: result.data }
          : { text: `Not started: ${result.error ?? `HTTP ${result.status}`}.` },
      );
      sent.finish();
    });

  const priorityValid = !problemList.some((problem) => problem.field === "priority");
  const stepProps = { value, problems, edit, snapshot, frames, sourceRefs, authoredMedia, sourceRefresh };
  const views = {
    scene: () => (
      <SceneStep
        {...stepProps}
        definitions={definitions}
        offered={handOver.offered}
        onTake={handOver.take}
      />
    ),
    review: () => (
      <ReviewStep
        {...stepProps}
        definitions={definitions}
        covering={covering}
        covered={covered}
        protectedNow={protectedNow}
        priority={priority}
        priorityValid={priorityValid}
        advanced={flow.advanced("review")}
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
      sectionLabel="Now"
      confirm={confirm}
      problems={problems}
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
      busy={write.busy}
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
  return frames.length > 0 ? (
    <FrameChips snapshot={snapshot} frameIds={frames} recoveryLinks />
  ) : "none";
}

/**
 * Step 1, Scene: the one Scene picker (slice 3 §3), then the frames it reaches. `offered`
 * is a Scene handed over while the draft keeps its own: a button offers it instead.
 */
function SceneStep({ value, problems, edit, snapshot, frames, sourceRefs, authoredMedia, sourceRefresh, definitions, offered, onTake }) {
  return (
    <>
      <OfferedScene
        offered={offered}
        current={value.sceneId}
        drafts="shows"
        action="Show"
        onTake={onTake}
      />
      <ScenePicker
        id={problems.idFor("scene")}
        label="Scene to activate"
        reason={problems.reasonFor("scene")}
        definitions={definitions}
        value={value.sceneId}
        onChange={(sceneId) => edit({ sceneId }, "scene")}
      />
      {value.sceneId !== "" && (
        <>
          <dl className="record">
            <dt>Frames</dt>
            <dd>
              <SceneFramesValue snapshot={snapshot} frames={frames} />
            </dd>
          </dl>
          <SourceFreshness snapshot={snapshot} refs={sourceRefs} authoredMedia={authoredMedia} refresh={sourceRefresh} />
        </>
      )}
    </>
  );
}

/**
 * Review's words for the priority: its value, and where a default comes from. `covered`
 * says whether any live Run covers the frames (a covering Run may have priority 0), and
 * `protectedNow` whether one protects any of them (no priority shows it on top then).
 */
function priorityWords(value, priority, covering, covered, protectedNow) {
  if (value.priority !== null) {
    return Number(priority) < covering
      ? `${priority} (below ${covering}, the highest Run on its frames)`
      : String(priority);
  }
  if (!covered) {
    return "0 (the default: no Run covers its frames)";
  }
  const lead = `${priority} (the default: the highest Run on its frames has priority ${covering}`;
  return protectedNow ? `${lead})` : `${lead}; at equal priority the newer Run shows on top)`;
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
  sourceRefs,
  authoredMedia,
  sourceRefresh,
  definitions,
  covering,
  covered,
  protectedNow,
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
          {
            label: "Priority",
            field: "priority",
            value: priorityWords(value, priority, covering, covered, protectedNow),
          },
          { label: "If it is already running", field: "repeat", value: REPEAT_LABELS[value.repeat] },
        ]}
      />
      {value.sceneId !== "" && (
        <SourceFreshness snapshot={snapshot} refs={sourceRefs} authoredMedia={authoredMedia} refresh={sourceRefresh} />
      )}
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

/** Current catalog freshness for the selected Scene's saved live Source refs. */
function SourceFreshness({ snapshot, refs, authoredMedia, refresh }) {
  const sources = snapshot?.media?.sources ?? [];
  const now = mediaNow(snapshot);
  const rows = refs.map((ref) => {
    const source = sources.find((candidate) => candidate.source_ref === ref) ?? null;
    return { ref, source, state: source === null ? null : sourceState(source, now, false) };
  });
  const needsAttention = rows.some(({ state }) => state === null || state.severity !== "ok");
  return (
    <section
      className={`notice show-now__sources${needsAttention ? " notice--warn" : ""}`}
      aria-label="Saved Source freshness"
    >
      <p><strong>Saved Source freshness</strong> is Central's latest catalog status. It does not confirm prepared media or visible playback.</p>
      {refs.length === 0 ? (
        <p>{authoredMedia
          ? "This Scene uses hand-picked media; refreshing a Source does not change its chosen items."
          : "This Scene has no live Sources to refresh."}</p>
      ) : (
        <ul className="show-now__source-list">
          {rows.map(({ ref, source, state }) => {
            const feedback = refresh.feedback[ref] ?? null;
            const message = sourceRefreshMessage(feedback, source, state);
            return (
              <li key={ref}>
                <span>{sourceName(source ?? ref)}: {state?.label ?? "Current Source status is unavailable."}</span>
                {state !== null && state.state !== "ok" && (
                  <button
                    type="button"
                    disabled={refresh.pending !== null}
                    aria-label={`Refresh ${sourceName(source)}`}
                    onClick={() => refresh.run(ref)}
                  >
                    {refresh.pending === ref ? "Requesting refresh…" : "Refresh Source"}
                  </button>
                )}
                {message !== null && <p role="status">{message}</p>}
              </li>
            );
          })}
        </ul>
      )}
    </section>
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
