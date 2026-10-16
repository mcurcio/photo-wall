import React, { useEffect, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import {
  buildSave,
  draftId,
  inRange,
  NEW_SCENE_DRAFT,
  normalizeScene,
  sceneEditDraft,
  sceneProblems,
  SETTING_RANGES,
  UNAUTHORABLE_REASON,
} from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { UNKNOWN_MESSAGE } from "./sendOutcome.js";
import { useProblems } from "./Field.jsx";
import { FlowFrame } from "./flow/FlowFrame.jsx";
import { editedId, editKey } from "./flow/instance.js";
import { inStepOrder } from "./flow/steps.js";
import { useFlowDraft } from "./flow/useFlowDraft.js";
import { useHandOffFrom } from "./flow/useHandOff.js";
import { useFlowInstance, useFlowRefs } from "./flow/useFlowInstance.js";
import { NOT_CONFIRMED, useFlowWrite } from "./flow/useFlowWrite.js";
import {
  changedSceneFields,
  earlierNotice,
  SCENE_ADVANCED_FIELDS,
  SCENE_FIELD_STEP,
  SCENE_KEYS,
  sceneSteps,
  seedScene,
} from "./sceneFlowModel.js";
import { SceneList } from "./SceneList.jsx";
import { SaveBar } from "./patterns/save-bar.tsx";
import { SCENE, SCHEDULE, scene as sceneWords } from "./showWords.js";
import { FramesStep, KindStep, MediaStep, PhotosStep, PlaybackStep, ReviewStep } from "./SceneSteps.jsx";
import { useCandidates } from "./useCandidates.js";
import { useHouseCropLimit } from "./domain/photo-fit.ts";
import { raiseAction, useFitRead } from "./domain/photo-fit-board.tsx";
import { useSourceCheck } from "./useSourceCheck.js";

const EMPTY = {};
// The button's words, as Schedules have it (ProgramsRegion.jsx).
const SAVE_AND_APPLY_NOW = "Save and apply now";

// Each step's heading: the one question it asks.
const HEADINGS = {
  kind: "What kind of Scene?",
  photos: "Which photos?",
  frames: "Which Frames?",
  media: "Which item on each frame?",
  playback: "How long each item shows",
  review: "Check your Scene",
};

/**
 * The Scenes section (flow design §7 J4, "Make a Scene"): the Scene cards and the
 * Scene flow, Kind → Frames → Photos → Media per frame (hand-picked only) → Playback
 * → Review. Frames come before Photos (follow-ups C1), so the Photos and Playback steps show
 * the Fit board of the chosen Sources on the chosen Frames, and Review its compact form.
 *
 * THE FIT BOARD reads one question for the draft (domain/photo-fit-board.tsx): its Sources,
 * its Frames (every Frame on the Frames step, whose tiles each say what that Frame would get, and
 * until some are chosen) and its crop limit (the house's while it has none). Each step previews
 * its own question from that read: Frames, what each Frame would get; Photos, how the photos fill
 * the chosen Frames together; Playback, what the crop limit lets each Frame take; Review, the
 * line and strip. While Playback's Crop limit is dragged, each value is asked by shape
 * (`liveCrop`); letting go keeps it. A Frame with nothing that fits is offered "Raise the crop
 * limit to N %".
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
 * flow inline (flow/handOff.js): it begins a hand-off to "sources" for this draft (its
 * `id`); the Source flow's Save returns here with the new Source chosen, and its Back or
 * Discard returns here with the draft unchanged. Either way Photos shows again with
 * focus on "Source". Once this draft closes or another replaces it, the hand-off is
 * settled with nothing (`useHandOffFrom`), so a Source saved later stays a Source and
 * never lands in another draft.
 *
 * A Save Central did not answer (no answer, or a 5xx) "may have been saved": the kit
 * remembers its id, which the name check then leaves out while the draft is unchanged,
 * and Save again (an idempotent PUT of the same body) confirms it.
 *
 * SAVE writes ONE request (authoring.js `buildSave`) through the kit's write
 * (flow/useFlowWrite.js: one Plane A refresh, so the new card is listed), ends the flow
 * (`finish`), remembers the Scene for the next flows (`rememberScene`, the shell's
 * `recentScene`) and offers "Show now" and "Schedule it".
 *
 * SAVE AND APPLY NOW (an edit's Review, beside Replace, as Schedules have it): the same body
 * `POST …/apply-now`, confirmed like Replace; what plays the Scene changes to the saved version
 * at once. A plain Replace applies from the Scene's next start (requirements › live media); when
 * Central's answer (`SceneOnWall.earlier_version`) says a Schedule or Show now still plays the
 * earlier version, the list names them (`earlierVersionNames`) and offers Save and apply now for
 * the saved body (an unchanged Scene: Central replaces only what is earlier).
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

  // The selected Frame is a seed for a newly opened Scene only. A route from the
  // Wall must never patch an existing draft, even when it names the same "new" key.
  const draft = useFlowDraft(seedScene(definitions, route?.initialTarget));
  const { patch } = draft;
  const value = draft.value ?? NEW_SCENE_DRAFT;
  const editingId = editedId(draft.key);
  const steps = sceneSteps(value.mode);

  const [vanished, setVanished] = useState(/** @type {string|null} */ (null));
  const [reloaded, setReloaded] = useState(/** @type {string|null} */ (null));
  // The Scene just saved, for the next actions (Show now, Schedule it).
  const [saved, setSaved] = useState(/** @type {string|null} */ (null));
  // After a save: what still plays the earlier version, and the save that applies it.
  const [earlier, setEarlier] = useState(
    /** @type {{sceneId: string, message: string, save: {path: string, body: object}}|null} */ (null),
  );
  const applyNowRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  // Check now on a chosen Source: its receipts belong to this draft.
  const sourceCheck = useSourceCheck(JSON.stringify([draft.id]));
  const saveRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const reloadRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const refs = useFlowRefs();

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

  // A hand-picked Scene picks from its first Source; its candidates are read again when that
  // Source's check moves on (a new check, or one completed). Central judges each photo's fit at
  // the draft's crop limit (roadmap 5a, run ledger K2), so a Frame offers exactly what the Scene
  // will show there; a limit outside its range is Playback's problem, and Central's default
  // judges meanwhile.
  const firstSource = sources.find((source) => source.source_ref === value.sourceRefs[0]) ?? null;
  const candidates = useCandidates(
    draft.key !== null && value.mode === "authored" ? value.sourceRefs[0] ?? "" : "",
    value.targets,
    checkStamp(firstSource?.check),
    inRange(value.cropLimit, SETTING_RANGES.cropLimit) ? Number(value.cropLimit) : undefined,
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
  // One confirmation (ConfirmAction) for this section: Replace, and discarding a
  // draft. A request's `after` runs once it is done. When a Replace ends without
  // replacing and Replace is withheld (a newer revision), focus moves on to Reload.
  const confirm = useConfirm(
    () => (reloadRef.current ?? saveRef.current)?.focus(),
    (result, request) => request.after?.(result),
  );

  const write = useFlowWrite({ draft, confirm, failure: "Could not save Scene" });
  const sourceReadiness = {
    chosen: value.sourceRefs.map((ref) => ({
      ref, source: sources.find((source) => source.source_ref === ref) ?? null,
    })),
    check: sourceCheck,
    onManage: () => navigate({ section: "sources" }),
  };

  // A Save Central did not answer may have stored its id: while the draft is the one it
  // sent, that id is not another Scene's (flow/useFlowWrite.js NOT CONFIRMED).
  const takenIds = useMemo(
    () =>
      write.attempt === null
        ? existingIds
        : new Set([...existingIds].filter((id) => !write.attempt.ids.includes(id))),
    [existingIds, write.attempt],
  );
  const problemList = useMemo(
    () =>
      inStepOrder(
        sceneProblems({ ...value, loadingMedia: candidates.loading, noMedia }, takenIds, {
          editing: editingId !== null,
        }),
        SCENE_FIELD_STEP,
        steps,
      ),
    [value, candidates.loading, noMedia, takenIds, editingId, steps],
  );
  const problems = useProblems(problemList);

  // --- The instance, its route and its steps (the flow kit).
  const flow = useFlowInstance({
    section: "scenes",
    draft,
    route,
    navigate,
    markDraft,
    keys: SCENE_KEYS,
    refs,
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
      setEarlier(null);
    },
  });
  const { place, step, focus } = flow;

  // --- How the draft's photos fit its Frames (THE FIT BOARD above), read while a step shows it.
  const houseCropLimit = useHouseCropLimit();
  const [liveCrop, setLiveCrop] = useState(/** @type {number|null} */ (null));
  const ownCrop = value.cropLimit !== null && value.cropLimit !== undefined
    && inRange(value.cropLimit, SETTING_RANGES.cropLimit) ? Number(value.cropLimit) : null;
  const judgedCrop = liveCrop ?? ownCrop;
  const boardShown = place === "open" && ["frames", "photos", "playback", "review"].includes(step ?? "");
  // The Frames step judges every Frame (each tile says what it would get, chosen or not); the
  // later steps judge the chosen ones.
  const everyFrame = step === "frames";
  const sourceKey = value.sourceRefs.join("\n");
  const targetKey = value.targets.join("\n");
  const fitQuestion = useMemo(() => (!boardShown || sourceKey === "" ? null : {
    selection: { sources: sourceKey.split("\n") },
    frames: everyFrame || targetKey === "" ? null : targetKey.split("\n"),
    crop_limit: judgedCrop,
  }), [boardShown, everyFrame, sourceKey, targetKey, judgedCrop]);
  const fitRead = useFitRead(fitQuestion, { live: liveCrop !== null });
  const fit = {
    read: fitRead,
    judged: { at: "scene", framesChosen: targetKey !== "", ownCrop: judgedCrop !== null },
    // Under the crop limit (Playback), whose value is right above: only which Frames.
    framesJudged: { at: "scene-crop", framesChosen: targetKey !== "" },
    rescue: (frame) => raiseAction(frame, (limit) => patch({ cropLimit: limit })),
  };

  const showNow = (sceneId) => {
    rememberScene(sceneId);
    navigate({ section: "now", flow: "show", step: "scene" }); // the Show-now flow, prefilled
  };
  const schedule = (sceneId) => {
    rememberScene(sceneId);
    navigate({ section: "schedule", flow: "new", step: "scene" }); // prefilled with it
  };

  // --- A new Source, made inline (NEW SOURCE above). The return reads this render's
  // draft and focus, whenever it comes back.
  const beginHandOff = useHandOffFrom(handOffs, "scenes", draft.id);
  const returnRef = useRef(null);
  returnRef.current = (owner, result, { show }) => {
    if (draft.id !== owner) {
      return false; // the draft it was begun for is gone
    }
    if (result !== null) {
      // The new Source joins the chosen ones (a hand-picked Scene has one, so it replaces it).
      draft.patch((current) => ({
        sourceRefs: current.mode === "authored" ? [result.sourceRef]
          : current.sourceRefs.includes(result.sourceRef) ? current.sourceRefs
            : [...current.sourceRefs, result.sourceRef],
      }));
    }
    if (show) {
      focus.openField("source");
    }
    return true;
  };
  const newSource = () => {
    const owner = draft.id;
    beginHandOff({
      to: "sources",
      label: "your Scene",
      onReturn: (result, options) => returnRef.current(owner, result, options),
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
      "Reloaded the latest saved Scene. " +
        (changed.length === 0
          ? "None of its stored values changed."
          : `Changed: ${changed.join(", ")}.`) +
        lost,
    );
  };

  // --- Save.
  const finish = (sceneId, end = flow.finish) => {
    end();
    setVanished(null);
    setReloaded(null);
    setSaved(sceneId);
    setEarlier(null);
    rememberScene(sceneId);
  };

  /** After a save (`applied`: Save and apply now): name what still plays the earlier version
   * (none: nothing said). */
  const sayEarlier = (sceneId, save, data, applied) => {
    const notice = earlierNotice(sceneId, data?.earlier_version, snapshot?.runtime, applied);
    setEarlier(notice === null ? null : { sceneId, message: notice.message, save });
  };

  const buildSceneSave = (sceneId) =>
    buildSave(value.mode, {
      sceneId,
      sourceRefs: value.sourceRefs,
      targetIds: value.targets,
      cycleSeconds: Number(value.cycleSeconds),
      loop: value.loop,
      selections: value.selections,
      transitionStyle: value.transitionStyle,
      fadeSeconds: value.fadeSeconds,
      ending: value.ending,
      endingSeconds: value.endingSeconds,
      keepLastPhoto: value.keepLastPhoto,
      keepTogether: value.keepTogether,
      order: value.order,
      noRepeatDays: value.noRepeatDays,
      cropLimit: value.cropLimit,
      revision: editingId === null ? 1 : draft.baseRevision + 1,
    });

  // An edit: Replace (from the Scene's next start), or Save and apply now (at once).
  const replace = (applyNow, opener) => {
    if (stale || !flow.checkAll()) {
      return;
    }
    const save = buildSceneSave(editingId);
    confirm.open(
      { currentTarget: opener },
      replaceRequest(
        editingId,
        draft.baseRevision,
        save,
        candidates.reload,
        write.bind((result) => {
          finish(editingId);
          sayEarlier(editingId, save, result?.data, applyNow);
        }),
        applyNow,
      ),
    );
  };

  // The notice's Save and apply now: the save Replace just stored, applied now.
  const applySavedNow = (event) => {
    if (earlier === null) {
      return;
    }
    const { sceneId, save } = earlier;
    confirm.open(event, replaceRequest(sceneId, save.body.scene?.revision ?? save.body.revision, save,
      candidates.reload, (result) => sayEarlier(sceneId, save, result?.data, true), true));
  };

  const onSave = () => {
    if (stale) {
      return;
    }
    if (editingId !== null) {
      replace(false, saveRef.current);
      return;
    }
    write.send(flow, async (sent) => {
      const sceneId = draftId(value);
      const save = buildSceneSave(sceneId);
      // ONE request, then one Plane A refresh, so the new card is listed when the flow
      // returns to the cards.
      const result = await sent.request(() => apiWrite(save.path, { method: "PUT", body: save.body }));
      if (result === null || result.status >= 500) {
        // It may have been stored: saving the same draft again confirms it.
        sent.attempted([sceneId]);
        sent.say(`Scene ${sceneId} ${NOT_CONFIRMED} Save again to confirm.`);
      } else if (result.ok) {
        sent.say(`Saved Scene ${sceneId}.`);
        finish(sceneId, sent.finish);
      } else {
        sent.say(refusal("Could not save Scene", result, candidates.reload));
      }
    });
  };

  // --- Views.
  const stepProps = { value, patch: draft.patch, problems };
  const { advanced } = flow;
  const views = {
    kind: () => <KindStep {...stepProps} />,
    photos: () => (
      <PhotosStep {...stepProps} sources={sources} onNewSource={newSource} sourceReadiness={sourceReadiness} fit={fit} />
    ),
    frames: () => (
      <FramesStep {...stepProps} snapshot={snapshot} onToggle={toggleTarget} fit={sourceKey === "" ? null : fit} />
    ),
    media: () => <MediaStep {...stepProps} candidates={candidates} onSelect={selectMedia} />,
    playback: () => (
      <PlaybackStep {...stepProps} advanced={advanced("playback")} fit={fit} houseCropLimit={houseCropLimit}
        onCropPreview={setLiveCrop} />
    ),
    review: () => (
      <>
        {stale && (
          <div className="notice notice--warn" role="status">
            <p>This Scene changed since you opened it.</p>
            <p>Reload it to review the latest saved Scene; Replace waits until you do.</p>
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
          fit={fit}
        />
        {editingId !== null && (
          <SaveBar label={SCENE.applyNowBar} message={SCENE.applyNowNote}>
            <button ref={applyNowRef} type="button" disabled={write.busy || stale}
              onClick={() => replace(true, applyNowRef.current)}>
              {SAVE_AND_APPLY_NOW}
            </button>
          </SaveBar>
        )}
      </>
    ),
  };

  const stepLabel = steps.find((candidate) => candidate.id === step)?.label ?? "";

  return (
    <FlowFrame
      flow={flow}
      draft={draft}
      keys={SCENE_KEYS}
      noun="Scene"
      sectionLabel="Scenes"
      unavailableReason={UNAUTHORABLE_REASON}
      confirm={confirm}
      problems={problems}
      writeRef={saveRef}
      next={
        place === "list" &&
        saved !== null &&
        !draft.dirty && (
          <>
            {earlier !== null && earlier.sceneId === saved && (
              <div className="notice notice--warn flow__said" role="group"
                aria-label={sceneWords("earlierGroup", { id: saved })}>
                <p role="status">{earlier.message}</p>
                <button type="button" onClick={applySavedNow}>
                  {SAVE_AND_APPLY_NOW}
                </button>
              </div>
            )}
            <div className="record__actions flow__said" role="group" aria-label={sceneWords("next", { id: saved })}>
              <button type="button" onClick={() => showNow(saved)}>
                {SCENE.showNow}
              </button>
              <button type="button" onClick={() => schedule(saved)}>
                {SCENE.scheduleIt}
              </button>
            </div>
          </>
        )
      }
      newLabel="New Scene"
      cards={
        <SceneList
          snapshot={snapshot}
          editDisabled={write.busy}
          editingId={editingId}
          editingDirty={draft.dirty}
          onEdit={(sceneId, event) => flow.start(editKey(sceneId), event)}
          onShowNow={showNow}
          onSchedule={schedule}
          onDeleted={(sceneId) => {
            if (saved === sceneId) setSaved(null);
            if (editingId === sceneId) {
              draft.discard();
              flow.leave();
            }
          }}
        />
      }
      title={editingId === null ? "New Scene" : `Edit Scene ${editingId}`}
      steps={steps}
      formLabel="Author a Scene"
      heading={HEADINGS[step] ?? stepLabel}
      busy={write.busy}
      onWrite={onSave}
      writeLabel={editingId === null ? "Save Scene" : "Replace Scene"}
      writeDisabled={write.busy || stale}
      problemsLabel="Scene problems"
      notice={
        <>
          {vanished !== null && (
            <p className="scene-flow__vanished notice notice--warn" role="status">
              {vanished}
            </p>
          )}
          {place === "open" && route?.initialTarget !== undefined &&
            !value.targets.includes(route.initialTarget) &&
            !draft.seeded?.targets.includes(route.initialTarget) && (
              <div className="notice notice--warn" role="status">
                <p>
                  {`Your open Scene draft was kept. Frame ${route.initialTarget} was not added.`}
                </p>
                <button type="button" onClick={() => flow.openField("targets")}>Choose Frames</button>
              </div>
            )}
        </>
      }
    >
      {step !== null && views[step]()}
    </FlowFrame>
  );
}

// A Replace refused because another editor saved first: nothing was replaced, and the
// next refresh shows the newer revision, so Review withholds Replace and offers Reload.
const REPLACE_CHANGED =
  "This Scene was changed since you opened it; nothing was replaced. Review now offers Reload.";

// Refusals in plain words (slice 3 §13): media_repository.py `author_candidates_in`
// answers the first (authored route) with 409; runtime.py `set_scene` the others (both
// routes): another save of this id landed first, or the Transition leaves less than 5 s of each
// photo free (`Scene.check_transition`, which the editor checks first).
const SAVE_REFUSALS = {
  authored_asset_not_member: "That item is no longer in the Source; choose again.",
  scene_revision_conflict: "A Scene with this id was saved meanwhile; nothing was replaced.",
  changed: "A Scene with this id was saved meanwhile; nothing was replaced.",
  transition_too_long: "Fade between photos must leave 5 s of each photo without a fade.",
};

// Central's answers that another save of this Scene landed first.
const CHANGED = new Set(["scene_revision_conflict", "changed"]);

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
 * polls, which ends in the terminal "changed". `after` runs once it is done, with the result
 * (its `data` Central's answer, a `SceneOnWall`). `applyNow`: Save and apply now, the same body
 * posted to `…/apply-now`.
 */
function replaceRequest(sceneId, revision, { path, body }, reload, after, applyNow = false) {
  return {
    key: `${applyNow ? "apply-now" : "replace"}:${sceneId}:${revision}`,
    title: applyNow ? sceneWords("applyNowTitle", { id: sceneId }) : `Replace Scene ${sceneId}?`,
    confirmLabel: applyNow ? SCENE.applyNowConfirm : "Confirm replace",
    // Save and apply now destroys nothing (what plays changes with a fade): the primary tone.
    tone: applyNow ? "primary" : "danger",
    body: (
      <p>
        {applyNow ? SCENE.applyNowBody : SCHEDULE.replaceNote}
      </p>
    ),
    run: async () => {
      const result = applyNow
        ? await apiWrite(`${path}/apply-now`, { method: "POST", body })
        : await apiWrite(path, { method: "PUT", body });
      if (result.ok) {
        const kept = (result.data?.earlier_version?.playing ?? []).length > 0;
        return {
          state: "done",
          message: !applyNow ? `Scene ${sceneId} saved.`
            : sceneWords(kept ? "savedNowKept" : "savedNow", { id: sceneId }),
          data: result.data,
        };
      }
      if (CHANGED.has(result.error)) {
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

/** A number that moves when a Source's check moves on: its start, or its completion. */
function checkStamp(check) {
  return Number(check?.at ?? check?.started_at ?? 0);
}
