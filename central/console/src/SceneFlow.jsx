import React, { useEffect, useMemo, useRef, useState } from "react";

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
import { UNKNOWN_MESSAGE } from "./equipmentApi.js";
import { ProblemSummary, useProblems } from "./Field.jsx";
import { editedId, editKey, NEW_KEY } from "./flow/instance.js";
import { DraftBar, InstanceNotice } from "./flow/InstanceNotice.jsx";
import { StepForm } from "./flow/StepForm.jsx";
import { Stepper } from "./flow/Stepper.jsx";
import { inStepOrder, stepOfField } from "./flow/steps.js";
import { useFlowDraft } from "./flow/useFlowDraft.js";
import { useFlowInstance } from "./flow/useFlowInstance.js";
import {
  changedSceneFields,
  SCENE_ADVANCED_FIELDS,
  SCENE_FIELD_STEP,
  SCENE_KEYS,
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
 * ROUTES AND STEPS are the flow kit's (flow/useFlowInstance.js): `#/scenes` shows the
 * cards; `#/scenes/new/<step>` and `#/scenes/<id>/edit/<step>` show a step, a new
 * Scene opening at Kind and an edit at Review (sceneFlowModel.js `SCENE_KEYS`). Another
 * instance never replaces a dirty draft silently. An edit route whose Scene the loaded
 * snapshot does not list says "This Scene no longer exists"; one the console cannot
 * author says why. Problems route through `SCENE_FIELD_STEP`: a summary entry opens its
 * step (and its Advanced, for "loop" and "id") and focuses the field.
 *
 * EDIT opens at Review, seeded from the stored Scene, with `baseRevision`. When a poll
 * shows another stored revision, Review says so, withholds Replace and offers Reload
 * (reseed; the values storage changed are named). Central's 409 `scene_revision_conflict`
 * remains the backstop for a change between polls. Replace goes through ConfirmAction
 * (slice 3 §13).
 *
 * NEW SOURCE. The Photos step's "New selection from your photo library" runs the Source
 * flow inline (flow/handOff.js): it begins a hand-off to "sources" for this draft; the
 * Source flow's Save returns here with the new Source chosen, and its Back or Discard
 * returns here with the draft unchanged. Either way Photos shows again with focus on
 * "Source"; a hand-off that returns after this draft closed changes nothing.
 *
 * SAVE writes ONE request (authoring.js `buildSave`) inside `useMutate()`, ends the flow
 * (`finish`), remembers the Scene for the next flows (`rememberScene`, the shell's
 * `recentSceneId`) and offers "Show now" and "Schedule it".
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          rememberScene: (sceneId: string) => void,
 *          markDraft: (section: string, dirty: boolean) => void,
 *          handOffs: ReturnType<typeof import("./flow/useHandOff.js").useHandOff>}} props
 */
export function SceneFlow({ snapshot, route, navigate, rememberScene, markDraft, handOffs }) {
  const definitions = snapshot?.runtime?.definitions ?? EMPTY;
  const existingIds = useMemo(() => new Set(Object.keys(definitions)), [definitions]);
  const sources = snapshot?.media?.sources ?? [];
  const loaded = snapshot != null;

  const draft = useFlowDraft(seedScene(definitions));
  const { patch } = draft;
  const value = draft.value ?? NEW_SCENE_DRAFT;
  const editingId = editedId(draft.key);
  const steps = sceneSteps(value.mode);
  const mutate = useMutate();

  const [vanished, setVanished] = useState(/** @type {string|null} */ (null));
  const [reloaded, setReloaded] = useState(/** @type {string|null} */ (null));
  const [saving, setSaving] = useState(false);
  // The Scene just saved, for the next actions (Show now, Schedule it).
  const [saved, setSaved] = useState(/** @type {string|null} */ (null));
  const saveRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const reloadRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const newRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const summaryRef = useRef(/** @type {HTMLDivElement|null} */ (null));
  const savedRef = useRef(/** @type {HTMLDivElement|null} */ (null));

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

  // --- Problems, in step order. A hand-picked frame whose candidates were read empty
  // has nothing to choose (its problem is the frame choice's).
  const noMedia = useMemo(
    () =>
      candidates.ready
        ? value.targets.filter((frameId) => (candidates.byFrame[frameId] ?? []).length === 0)
        : [],
    [candidates.ready, candidates.byFrame, value.targets],
  );
  const problemList = useMemo(
    () =>
      inStepOrder(
        sceneProblems({ ...value, loadingMedia: candidates.loading, noMedia }, existingIds, {
          editing: editingId !== null,
        }),
        SCENE_FIELD_STEP,
        steps,
      ),
    [value, candidates.loading, noMedia, existingIds, editingId, steps],
  );
  const problems = useProblems(problemList);

  // One confirmation (ConfirmAction) for this section: Replace, and discarding a
  // draft. A request's `after` runs once it is done. When a Replace ends without
  // replacing and Replace is withheld (a newer revision), focus moves on to Reload.
  const confirm = useConfirm(
    () => (reloadRef.current ?? saveRef.current)?.focus(),
    (_result, request) => request.after?.(),
  );

  // --- The instance, its route and its steps (the flow kit).
  const flow = useFlowInstance({
    section: "scenes",
    draft,
    route,
    navigate,
    markDraft,
    keys: SCENE_KEYS,
    availability: (key) => {
      const id = editedId(key);
      if (id === null) {
        return "ok";
      }
      if (definitions[id] === undefined) {
        return "missing";
      }
      return sceneEditDraft(definitions[id]) === null ? "unavailable" : "ok";
    },
    steps,
    fieldStep: SCENE_FIELD_STEP,
    advancedFields: SCENE_ADVANCED_FIELDS,
    problemList,
    problems,
    confirm,
    onOpened: () => {
      setVanished(null);
      setReloaded(null);
      setSaved(null);
    },
  });
  const { place, step, focus } = flow;

  const showNow = (sceneId) => {
    rememberScene(sceneId);
    navigate({ section: "now" });
  };
  const schedule = (sceneId) => {
    rememberScene(sceneId);
    navigate({ section: "schedule" });
  };

  // --- A new Source, made inline (NEW SOURCE above). The return reads this render's
  // draft and focus, whenever it comes back.
  const returnRef = useRef(null);
  returnRef.current = (key, result, { show }) => {
    if (draft.key !== key) {
      return false; // the draft it was begun for is gone
    }
    if (result !== null) {
      draft.patch({ sourceRef: result.sourceRef });
    }
    if (show) {
      focus.openField("source");
    }
    return true;
  };
  const newSource = () => {
    const key = draft.key;
    handOffs.begin({
      from: "scenes",
      to: "sources",
      label: "your Scene",
      onReturn: (result, options) => returnRef.current(key, result, options),
    });
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

  // --- Edit: a stored revision other than the draft's base.
  const stored = editingId === null ? undefined : definitions[editingId];
  const storedRevision = stored === undefined ? null : normalizeScene(stored).revision;
  // A newer one, or a lower one (a Scene deleted and made again, or restored).
  const stale =
    storedRevision !== null && draft.baseRevision !== null && storedRevision !== draft.baseRevision;

  /** Reseed from storage, naming what storage changed and what the operator lost. */
  const reload = () => {
    const fresh = seedScene(definitions)(draft.key);
    if (fresh === null) {
      return;
    }
    const changed = changedSceneFields(draft.seeded, fresh);
    const lost = draft.dirty
      ? ` Your unsaved changes to ${SCENE_KEYS.describe(draft.key)} were replaced.`
      : "";
    draft.reseed();
    problems.reset();
    setReloaded(
      `Reloaded revision ${fresh.revision}. ` +
        (changed.length === 0
          ? "None of its stored values changed."
          : `Changed: ${changed.join(", ")}.`) +
        lost,
    );
  };

  // --- Save.
  const finish = (sceneId) => {
    flow.finish(() => savedRef.current);
    setVanished(null);
    setReloaded(null);
    setSaved(sceneId);
    rememberScene(sceneId);
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
      <PhotosStep {...stepProps} sources={sources} onNewSource={newSource} />
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
            <button ref={reloadRef} type="button" onClick={reload}>
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
          onChange={flow.openField}
        />
      </>
    ),
  };

  const stepLabel = steps.find((candidate) => candidate.id === step)?.label ?? "";
  const draftName = SCENE_KEYS.describe(draft.key);

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
          <DraftBar
            dirty={draft.dirty}
            draftName={draftName}
            newLabel="New Scene"
            newRef={newRef}
            onNew={() => flow.start(NEW_KEY)}
            onResume={() => flow.resume()}
            onDiscard={(event) =>
              confirm.open(
                event,
                flow.discardRequest(() => focus.focusWhenShown(() => newRef.current)),
              )
            }
          />
          <SceneList
            snapshot={snapshot}
            onEdit={(sceneId, event) => flow.start(editKey(sceneId), event)}
            onShowNow={showNow}
            onSchedule={schedule}
          />
        </>
      )}

      <InstanceNotice
        place={place}
        draftName={draftName}
        targetName={flow.routeKey === null ? "" : SCENE_KEYS.describe(flow.routeKey)}
        noun="Scene"
        unavailableReason={UNAUTHORABLE_REASON}
        sectionHref="#/scenes"
        sectionLabel="Scenes"
        onResume={() => flow.resume({ replace: true })}
        onDiscard={flow.discardForRoute}
      />

      {step !== null && (
        <>
          <h2 className="scene-flow__title">
            {editingId === null ? "New Scene" : `Edit Scene ${editingId}`}
          </h2>
          <Stepper
            steps={steps}
            current={step}
            onStep={saving ? undefined : flow.showStep}
            answered={flow.answered}
          />
          <StepForm
            label="Author a Scene"
            heading={HEADINGS[step] ?? stepLabel}
            onSubmit={step === "review" ? onSave : flow.onContinue}
            onBack={flow.onBack}
            submitLabel={
              step !== "review" ? "Continue" : editingId === null ? "Save Scene" : "Replace Scene"
            }
            submitDisabled={step === "review" && (saving || stale)}
            busy={saving}
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
              onOpen={flow.openField}
            />
            {views[step]()}
          </StepForm>
        </>
      )}
    </div>
  );
}

// A Replace refused because another editor saved first: nothing was replaced, and the
// next refresh shows the newer revision, so Review withholds Replace and offers Reload.
const REPLACE_CHANGED =
  "This Scene was changed since you opened it; nothing was replaced. Review now offers Reload.";

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
        return { state: "changed", message: REPLACE_CHANGED };
      }
      if (result.status >= 500) {
        return { state: "unknown", message: UNKNOWN_MESSAGE };
      }
      return { state: "refused", message: refusal("Not replaced", result, reload) };
    },
    after,
  };
}
