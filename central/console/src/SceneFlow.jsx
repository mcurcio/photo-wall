import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import {
  buildSave,
  draftId,
  NEW_SCENE_DRAFT,
  normalizeScene,
  sceneEditDraft,
  sceneProblems,
  UNAUTHORABLE_REASON,
} from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { CHANGED_MESSAGE, UNKNOWN_MESSAGE } from "./equipmentApi.js";
import { fieldControl, ProblemSummary, useProblems } from "./Field.jsx";
import { StepForm } from "./flow/StepForm.jsx";
import { Stepper } from "./flow/Stepper.jsx";
import { inStepOrder, nextStep, previousStep, problemsOf, stepOfField } from "./flow/steps.js";
import { useFlowDraft } from "./flow/useFlowDraft.js";
import { useFlowFocus } from "./flow/useFlowFocus.js";
import {
  changedSceneFields,
  describeKey,
  editedId,
  firstStep,
  NEW_KEY,
  SCENE_ADVANCED_FIELDS,
  SCENE_FIELD_STEP,
  sceneKey,
  sceneRoute,
  sceneSteps,
  seedScene,
} from "./sceneFlowModel.js";
import { SceneList } from "./SceneList.jsx";
import { FramesStep, KindStep, MediaStep, PhotosStep, PlaybackStep, ReviewStep } from "./SceneSteps.jsx";
import { useCandidates } from "./useCandidates.js";
import { useMutate } from "./useMutate.js";

const EMPTY = {};

// Each step's heading: the one question it asks.
const HEADINGS = {
  kind: "What kind of Scene?",
  photos: "Which photos?",
  frames: "Which frames?",
  media: "Which item on each frame?",
  playback: "How long each item shows",
  review: "Check your Scene",
};

/**
 * The Scenes section (flow design §7 J4, "Make a Scene"): the Scene cards and the
 * Scene flow, Kind → Photos → Frames → Media per frame (hand-picked only) → Playback
 * → Review.
 *
 * THE CONTAINER. This component owns the flow's one draft (`useFlowDraft`) and EVERY
 * draft effect: the vanished-frame prune, the candidates read (`useCandidates`) and
 * the candidate prune. It never unmounts (the shell keeps Show sections mounted,
 * rule 2), so they run whichever step, or section, is showing, and a step change, a
 * section change, a poll or the sign-in overlay cannot lose the draft. The prunes are
 * no-ops while the snapshot is null (§6 (c)). The step views (SceneSteps.jsx) hold no
 * draft state.
 *
 * ROUTES. `#/scenes` shows the cards; `#/scenes/new/<step>` and
 * `#/scenes/<id>/edit/<step>` show a step. Steps move with `replace`, so a flow is one
 * history entry and browser Back leaves it with the draft kept (Question 2); Save
 * replaces the flow entry with `#/scenes`. An unknown or inapplicable step is replaced
 * by the instance's first step (Kind for new, Review for an edit). Opening another
 * instance never replaces a dirty draft silently: an in-app Edit asks through
 * `useConfirm`, and a typed or Back URL shows "Unsaved draft for X: Resume or Discard".
 * An edit route whose Scene the loaded snapshot does not list says "This Scene no
 * longer exists".
 *
 * STEPS. Continue checks the current step's problems; Review checks them all. Problems
 * route through `SCENE_FIELD_STEP` (sceneFlowModel.js): a summary entry opens its step (and
 * its Advanced, for "loop" and "id") and focuses the field once it mounts
 * (flow/useFlowFocus.js). After a Change link or a routed problem, Continue returns
 * toward Review.
 *
 * EDIT opens at Review, seeded from the stored Scene, with `baseRevision`. When a poll
 * shows a newer stored revision, Review says so, withholds Replace and offers Reload
 * (reseed; the changed values are named). Central's 409 `scene_revision_conflict`
 * remains the backstop for a change between polls. Replace goes through ConfirmAction
 * (slice 3 §13).
 *
 * SAVE writes ONE request (authoring.js `buildSave`) inside `useMutate()`, discards the
 * draft, remembers the Scene for the next flows (`rememberScene`, the shell's
 * `recentSceneId`) and offers "Show now" and "Schedule it".
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          rememberScene: (sceneId: string) => void,
 *          markDraft: (section: string, dirty: boolean) => void}} props
 */
export function SceneFlow({ snapshot, route, navigate, rememberScene, markDraft }) {
  const definitions = snapshot?.runtime?.definitions ?? EMPTY;
  const existingIds = useMemo(() => new Set(Object.keys(definitions)), [definitions]);
  const sources = snapshot?.media?.sources ?? [];
  const loaded = snapshot != null;

  const draft = useFlowDraft(seedScene(definitions));
  const { patch } = draft;
  const value = draft.value ?? NEW_SCENE_DRAFT;
  const editingId = editedId(draft.key);
  const mutate = useMutate();

  const [vanished, setVanished] = useState(/** @type {string|null} */ (null));
  const [reloaded, setReloaded] = useState(/** @type {string|null} */ (null));
  const [returning, setReturning] = useState(false);
  const [saving, setSaving] = useState(false);
  // The Scene just saved, for the next actions (Show now, Schedule it).
  const [saved, setSaved] = useState(/** @type {string|null} */ (null));
  // The step the open draft last showed, for Resume.
  const lastStepRef = useRef(/** @type {string|null} */ (null));
  const saveRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const summaryRef = useRef(/** @type {HTMLDivElement|null} */ (null));
  const savedRef = useRef(/** @type {HTMLDivElement|null} */ (null));

  // --- Where the route points, and what the draft can show there.
  const routeKey = sceneKey(route);
  const routeId = editedId(routeKey);
  const routeScene = routeId === null ? undefined : definitions[routeId];
  let place = "list";
  if (routeKey !== null) {
    if (routeId !== null && routeScene === undefined) {
      place = "missing";
    } else if (routeId !== null && sceneEditDraft(routeScene) === null) {
      place = "unauthorable";
    } else if (routeKey === draft.key) {
      place = "open";
    } else {
      place = draft.dirty ? "blocked" : "opening";
    }
  }
  const steps = sceneSteps(value.mode);
  const step =
    place === "open" && steps.some((candidate) => candidate.id === route.step) ? route.step : null;

  // --- The draft effects: they run whichever step (or section) is showing.
  const frameKey = (snapshot?.inventory?.frames ?? []).map((frame) => frame.id).join(" ");
  useEffect(() => {
    if (!loaded || draft.key === null) {
      return;
    }
    const listed = new Set(frameKey === "" ? [] : frameKey.split(" "));
    const gone = value.targets.filter((frameId) => !listed.has(frameId));
    if (gone.length === 0) {
      return;
    }
    patch((current) => ({
      targets: current.targets.filter((frameId) => listed.has(frameId)),
      selections: Object.fromEntries(
        Object.entries(current.selections).filter(([frameId]) => listed.has(frameId)),
      ),
    }));
    setVanished(
      `${gone.join(", ")} ${gone.length === 1 ? "was" : "were"} deleted and removed from this Scene.`,
    );
  }, [loaded, frameKey, value.targets, draft.key, patch]);

  const candidates = useCandidates(
    draft.key !== null && value.mode === "authored" ? value.sourceRef : "",
    value.targets,
  );

  // A per-frame choice belongs to one Source's catalog: once a frame's candidates are
  // read, a choice not among them is dropped. An edited hand-picked Scene keeps its
  // stored items only while they are still candidates.
  useEffect(() => {
    if (!loaded || !candidates.ready) {
      return;
    }
    patch((current) => {
      const kept = Object.entries(current.selections).filter(([frameId, assetId]) =>
        (candidates.byFrame[frameId] ?? []).some((candidate) => candidate.asset_id === assetId),
      );
      return kept.length === Object.keys(current.selections).length
        ? null
        : { selections: Object.fromEntries(kept) };
    });
  }, [loaded, candidates.ready, candidates.byFrame, patch]);

  useEffect(() => {
    markDraft("scenes", draft.dirty);
  }, [markDraft, draft.dirty]);

  // --- Problems, in step order.
  const problemList = useMemo(
    () =>
      inStepOrder(
        sceneProblems({ ...value, loadingMedia: candidates.loading }, existingIds, {
          editing: editingId !== null,
        }),
        SCENE_FIELD_STEP,
        steps,
      ),
    [value, candidates.loading, existingIds, editingId, steps],
  );
  const problems = useProblems(problemList);

  const goToStep = useCallback(
    (stepId) => {
      if (draft.key !== null) {
        navigate(sceneRoute(draft.key, stepId), { replace: true });
      }
    },
    [draft.key, navigate],
  );
  const focus = useFlowFocus({
    step,
    fieldStep: SCENE_FIELD_STEP,
    advancedFields: SCENE_ADVANCED_FIELDS,
    goToStep,
    controlFor: (field) => fieldControl(problems.idFor(field)),
  });

  // One confirmation (ConfirmAction) for this section: Replace, and discarding a
  // draft. A request's `after` runs once it is done.
  const confirm = useConfirm(
    () => saveRef.current?.focus(),
    (_result, request) => request.after?.(),
  );

  // --- Opening instances.
  /** Open `key`; false when a dirty draft of another instance refused it. */
  const openInstance = (key) => {
    const before = draft.key;
    if (draft.open(key) !== key) {
      return false;
    }
    if (before !== key) {
      confirm.setStatus(null);
      problems.reset();
      focus.reset();
      setVanished(null);
      setReloaded(null);
      setReturning(false);
      setSaved(null);
      lastStepRef.current = null;
    }
    return true;
  };

  useLayoutEffect(() => {
    if (place === "opening") {
      openInstance(routeKey);
    } else if (place === "open" && step === null) {
      navigate(sceneRoute(routeKey, lastStepRef.current ?? firstStep(routeKey)), { replace: true });
    }
  });

  if (step !== null) {
    lastStepRef.current = step;
  }

  const resumeRoute = () => sceneRoute(draft.key, lastStepRef.current ?? firstStep(draft.key));

  /** Discard the dirty draft (asked through useConfirm), then `then()`. */
  const askToDiscard = (event, then) =>
    confirm.open(event, {
      key: `discard:${draft.key}`,
      title: "Discard your unsaved draft?",
      confirmLabel: "Discard draft",
      body: <p>{`Your unsaved changes to ${describeKey(draft.key)} will be lost.`}</p>,
      run: async () => ({ state: "done", message: `Discarded the draft for ${describeKey(draft.key)}.` }),
      after: () => {
        draft.discard();
        then?.();
      },
    });

  /** Show `target` (a step route) and move focus to its step heading. */
  const enter = (target, options) => {
    navigate(target, options);
    focus.focusStep();
  };

  const startNew = () => {
    if (openInstance(NEW_KEY)) {
      enter(sceneRoute(NEW_KEY, firstStep(NEW_KEY)));
    }
  };

  const startEdit = (sceneId, event) => {
    const key = `edit/${sceneId}`;
    if (draft.key === key) {
      enter(resumeRoute());
    } else if (openInstance(key)) {
      enter(sceneRoute(key, firstStep(key)));
    } else {
      askToDiscard(event, () => enter(sceneRoute(key, firstStep(key))));
    }
  };

  const showNow = (sceneId) => {
    rememberScene(sceneId);
    navigate({ section: "now" });
  };
  const schedule = (sceneId) => {
    rememberScene(sceneId);
    navigate({ section: "schedule" });
  };

  // --- Draft edits the views ask for.
  const toggleTarget = (frameId) =>
    draft.patch((current) => {
      const chosen = current.targets.includes(frameId);
      const selections = { ...current.selections };
      delete selections[frameId]; // a frame no longer targeted keeps no choice
      return {
        targets: chosen ? current.targets.filter((id) => id !== frameId) : [...current.targets, frameId],
        selections: chosen ? selections : current.selections,
      };
    });

  const selectMedia = (frameId, assetId) =>
    draft.patch((current) => {
      const selections = { ...current.selections };
      if (assetId === "") {
        delete selections[frameId];
      } else {
        selections[frameId] = assetId;
      }
      return { selections };
    });

  /** Route a problem (a summary entry) or a Change link to its field. */
  const openField = (field) => {
    if (stepOfField(SCENE_FIELD_STEP, field) !== "review") {
      setReturning(true);
    }
    focus.openField(field);
  };

  // --- Moving through the steps.
  const onContinue = () => {
    const own = problemsOf(problemList, SCENE_FIELD_STEP, step);
    if (!problems.check(own)) {
      focus.openField(own[0].field);
      return;
    }
    const problemSteps = new Set(problemList.map((problem) => stepOfField(SCENE_FIELD_STEP, problem.field)));
    const next = nextStep(steps, step, { returning, problemSteps });
    if (next === "review") {
      setReturning(false);
    }
    goToStep(next);
    focus.focusStep();
  };

  const onBack = () => {
    const previous = previousStep(steps, step);
    if (previous === null) {
      navigate({ section: "scenes" }, { replace: true });
      return;
    }
    goToStep(previous);
    focus.focusStep();
  };

  // --- Edit: a newer stored revision than the draft's base.
  const stored = editingId === null ? undefined : definitions[editingId];
  const storedRevision = stored === undefined ? null : normalizeScene(stored).revision;
  const stale =
    storedRevision !== null && draft.baseRevision !== null && storedRevision > draft.baseRevision;

  const reload = () => {
    const fresh = seedScene(definitions)(draft.key);
    if (fresh === null) {
      return;
    }
    const changed = changedSceneFields(value, fresh);
    draft.reseed();
    problems.reset();
    setReloaded(
      `Reloaded revision ${fresh.revision}. ` +
        (changed.length === 0
          ? "None of the values here changed."
          : `Changed: ${changed.join(", ")}.`),
    );
  };

  // --- Save.
  const finish = (sceneId) => {
    draft.discard();
    problems.reset();
    focus.reset();
    setVanished(null);
    setReloaded(null);
    setReturning(false);
    lastStepRef.current = null;
    setSaved(sceneId);
    rememberScene(sceneId);
    navigate({ section: "scenes" }, { replace: true });
    focus.focusWhenShown(() => savedRef.current);
  };

  const onSave = async () => {
    if (saving || stale) {
      return;
    }
    if (!problems.check(problemList)) {
      const first = problemList[0];
      if (stepOfField(SCENE_FIELD_STEP, first.field) === "review") {
        focus.openField(first.field);
      } else {
        focus.focusWhenShown(() => summaryRef.current);
      }
      return;
    }
    const sceneId = editingId ?? draftId(value);
    const save = buildSave(value.mode, {
      sceneId,
      sourceRef: value.sourceRef,
      targetIds: value.targets,
      cycleSeconds: Number(value.cycleSeconds),
      loop: value.loop,
      selections: value.selections,
      revision: editingId === null ? 1 : draft.baseRevision + 1,
    });
    if (editingId !== null) {
      confirm.open(
        { currentTarget: saveRef.current },
        replaceRequest(editingId, draft.baseRevision, save, candidates.reload, () => finish(sceneId)),
      );
      return;
    }
    setSaving(true);
    confirm.setStatus(null);
    try {
      // ONE request; useMutate() then does its one Plane A refresh, so the new card is
      // listed when the flow returns to the cards.
      const result = await mutate(() => apiWrite(save.path, { method: "PUT", body: save.body }));
      if (result.ok) {
        confirm.setStatus(`Saved Scene ${sceneId}.`);
        finish(sceneId);
      } else {
        confirm.setStatus(refusal("Could not save Scene", result, candidates.reload));
      }
    } catch {
      confirm.setStatus("Could not save Scene: the request did not complete.");
    } finally {
      setSaving(false);
    }
  };

  // --- Views.
  const stepProps = { value, patch: draft.patch, problems };
  const advanced = (stepId) => ({
    open: focus.advancedOpen(stepId),
    onToggle: () => focus.toggleAdvanced(stepId),
  });
  const views = {
    kind: () => <KindStep {...stepProps} />,
    photos: () => (
      <PhotosStep {...stepProps} sources={sources} onNewSource={() => navigate({ section: "sources" })} />
    ),
    frames: () => <FramesStep {...stepProps} snapshot={snapshot} onToggle={toggleTarget} />,
    media: () => <MediaStep {...stepProps} candidates={candidates} onSelect={selectMedia} />,
    playback: () => <PlaybackStep {...stepProps} advanced={advanced("playback")} />,
    review: () => (
      <>
        {stale && (
          <div className="notice notice--warn" role="status">
            <p>{`This Scene was changed (revision ${storedRevision}) since you opened it.`}</p>
            <p>Reload it to review the stored version; Replace waits until you do.</p>
            <button type="button" onClick={reload}>
              Reload
            </button>
          </div>
        )}
        {reloaded !== null && (
          <p className="scene-flow__reloaded" role="status">
            {reloaded}
          </p>
        )}
        <ReviewStep
          {...stepProps}
          snapshot={snapshot}
          editingId={editingId}
          candidates={candidates}
          advanced={advanced("review")}
          onChange={openField}
        />
      </>
    ),
  };

  const stepLabel = steps.find((candidate) => candidate.id === step)?.label ?? "";

  return (
    <div className="scene-flow" ref={focus.rootRef}>
      <div className="scene-flow__saved" ref={savedRef} tabIndex={-1}>
        {confirm.confirmation("scene-flow__status-line")}
        {place === "list" && saved !== null && !draft.dirty && (
          <div className="record__actions" role="group" aria-label={`Next for Scene ${saved}`}>
            <button type="button" onClick={() => showNow(saved)}>
              Show now
            </button>
            <button type="button" onClick={() => schedule(saved)}>
              Schedule it
            </button>
          </div>
        )}
      </div>

      {place === "list" && (
        <>
          <div className="scene-flow__toolbar">
            {draft.dirty ? (
              <>
                <p className="scene-flow__draft-note">
                  {`Unsaved draft for ${describeKey(draft.key)}.`}
                </p>
                <button type="button" className="button--primary" onClick={() => enter(resumeRoute())}>
                  Resume draft <span className="scene-flow__draft-word">(Draft)</span>
                </button>
                <button type="button" onClick={(event) => askToDiscard(event)}>
                  Discard draft
                </button>
              </>
            ) : (
              <button type="button" className="button--primary" onClick={startNew}>
                New Scene
              </button>
            )}
          </div>
          <SceneList snapshot={snapshot} onEdit={startEdit} onShowNow={showNow} onSchedule={schedule} />
        </>
      )}

      {place === "missing" && (
        <div className="notice">
          <p>{`Scene ${routeId}: This Scene no longer exists.`}</p>
          <a href="#/scenes">Back to Scenes</a>
        </div>
      )}

      {place === "unauthorable" && (
        <div className="notice">
          <p>{`Scene ${routeId} can't be edited here: ${UNAUTHORABLE_REASON}`}</p>
          <a href="#/scenes">Back to Scenes</a>
        </div>
      )}

      {place === "blocked" && (
        <div className="notice notice--warn">
          <p>{`Unsaved draft for ${describeKey(draft.key)}: Resume or Discard`}</p>
          <p>{`Discarding it opens ${describeKey(routeKey)}.`}</p>
          <div className="record__actions">
            <button
              type="button"
              className="button--primary"
              onClick={() => enter(resumeRoute(), { replace: true })}
            >
              Resume
            </button>
            <button
              type="button"
              onClick={() => {
                draft.discard();
                focus.focusStep();
              }}
            >
              Discard
            </button>
          </div>
        </div>
      )}

      {step !== null && (
        <>
          <h2 className="scene-flow__title">
            {editingId === null ? "New Scene" : `Edit Scene ${editingId}`}
          </h2>
          <Stepper
            steps={steps}
            current={step}
            onStep={(stepId) => {
              goToStep(stepId);
              focus.focusStep();
            }}
          />
          <StepForm
            label="Author a Scene"
            heading={HEADINGS[step] ?? stepLabel}
            onSubmit={step === "review" ? onSave : onContinue}
            onBack={onBack}
            submitLabel={
              step !== "review" ? "Continue" : editingId === null ? "Save Scene" : "Replace Scene"
            }
            submitDisabled={step === "review" && (saving || stale)}
            submitRef={saveRef}
          >
            {vanished !== null && (
              <p className="scene-flow__vanished notice notice--warn" role="status">
                {vanished}
              </p>
            )}
            <ProblemSummary
              ref={summaryRef}
              summary={problems.summary}
              label="Scene problems"
              onOpen={openField}
            />
            {views[step]()}
          </StepForm>
        </>
      )}
    </div>
  );
}

// Refusals in plain words (slice 3 §13): media_repository.py `author_candidates_in`
// answers the first two (authored route) with 409; runtime.py `set_scene` the last
// (both routes) when another save of this id landed first.
const SAVE_REFUSALS = {
  source_not_fresh:
    "The Source's last refresh failed; authored choices can be saved once it succeeds.",
  authored_asset_not_member: "That item is no longer in the Source; choose again.",
  scene_revision_conflict: "A Scene with this id was saved meanwhile; nothing was replaced.",
};

/**
 * A refused save in words: a known refusal as its sentence (and, when an item left
 * the Source, the choosers read their candidates again), otherwise the served code.
 */
function refusal(lead, result, reload) {
  const words = SAVE_REFUSALS[result.error];
  if (words === undefined) {
    return `${lead}: ${result.error ?? `HTTP ${result.status}`}.`;
  }
  if (result.error === "authored_asset_not_member") {
    reload();
  }
  return `${lead}. ${words}`;
}

/**
 * Replace a stored Scene (slice 3 §13), captured when the dialog opens: the body and
 * the revision the draft was based on. Central refuses it with 409
 * `scene_revision_conflict` when the stored Scene moved past that revision between
 * polls, which ends in the terminal "changed". `after` runs once it is done.
 */
function replaceRequest(sceneId, revision, { path, body }, reload, after) {
  return {
    key: `replace:${sceneId}:${revision}`,
    title: `Replace Scene ${sceneId}?`,
    confirmLabel: "Confirm replace",
    body: (
      <p>
        {`Saves it as revision ${revision + 1}. Runs already going keep the version they ` +
          "started with; Programs that start later use the new one."}
      </p>
    ),
    run: async () => {
      const result = await apiWrite(path, { method: "PUT", body });
      if (result.ok) {
        return { state: "done", message: `Replaced Scene ${sceneId}: now revision ${revision + 1}.` };
      }
      if (result.error === "scene_revision_conflict") {
        return { state: "changed", message: CHANGED_MESSAGE };
      }
      if (result.status >= 500) {
        return { state: "unknown", message: UNKNOWN_MESSAGE };
      }
      return { state: "refused", message: refusal("Not replaced", result, reload) };
    },
    after,
  };
}
