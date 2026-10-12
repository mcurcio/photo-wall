import React, { useId } from "react";

import { draftId, idFromName, PLAYBACK_LABELS } from "./authoring.js";
import { CycleField, LoopField } from "./CycleInput.jsx";
import { Field, IdField, idNeeded, NameField } from "./Field.jsx";
import { SourceCheckProblem } from "./domain/source-check-problem.tsx";
import { Advanced } from "./flow/Advanced.jsx";
import { CheckAnswers, NotChosen } from "./flow/CheckAnswers.jsx";
import { candidateLabels } from "./mediaHealth.js";
import { SCENE_ANSWER_LABELS } from "./sceneFlowModel.js";
import {
  AdvancedRows, cropWords, daysWords, EndingRows, endingWords, onOff, ORDER_LABELS, secondsWords, SelectionRows,
  TimingRows,
} from "./SceneSettings.jsx";
import { SourcePicker } from "./SourcePicker.jsx";
import { sourceName } from "./sourceNames.js";
import { FrameChips, TargetPicker } from "./TargetPicker.jsx";
import { useDraftPool } from "./useDraftPool.js";
import { frameChoiceTiles, PhotoFitBoard } from "./domain/photo-fit-board.tsx";

/**
 * The draft's Fit board (follow-ups C1), composed for its step (design review round 2: each
 * step previews its own question): read by SceneFlow.jsx (`fit`: its read, judged words and the
 * raise a Frame with nothing that fits is offered). `lead`: `wall` on Photos (how these photos
 * fill the Scene's Frames together), `crop` on Playback (what the crop limit, right above it,
 * lets each Frame take), the strip on Review.
 */
function SceneFitBoard({ fit, size, lead = "wall" }) {
  if (!fit) return null;
  const crop = lead === "crop";
  const title = crop ? "What the crop limit lets each Frame take" : "How this Scene fills its Frames";
  return (
    <PhotoFitBoard read={fit.read} size={size} lead={lead} judged={crop ? fit.framesJudged : fit.judged} rescue={fit.rescue}
      heading={size === "strip" ? undefined : title} title={title} />
  );
}
import { sourceCheckMessage } from "./useSourceCheck.js";

/**
 * The Scene flow's step views (flow design §7 J4): views over the draft that
 * SceneFlow.jsx owns. They hold no draft state and run no draft effect (the
 * container runs the prunes, whichever step shows); each renders its fields through
 * the shared pickers and Field.jsx, so labels and accessible names are the ones the
 * single form had (rule 3): "Choose <Source>", "Target frame <id>", "Media for frame <id>",
 * "Each photo shows for", "Keep repeating", "Scene name", "Id".
 *
 * Every view takes `{value, patch, problems}`: the draft value, the draft's patch,
 * and the flow's `useProblems` (reasons beside fields; field ids). Views with an
 * Advanced disclosure also take `advanced: {open, onToggle}`.
 *
 * @typedef {import("./authoring.js").SceneDraft} SceneDraft
 * @typedef {ReturnType<typeof import("./Field.jsx").useProblems>} Problems
 * @typedef {{value: SceneDraft, patch: (partial: Partial<SceneDraft>) => void,
 *            problems: Problems}} StepProps
 */

/** The two kinds of Scene, as the Kind step and Review word them. */
export const KIND_LABELS = Object.freeze({
  live: "Live from a Source",
  authored: "Hand-picked per frame",
});

const KIND_HINTS = {
  live: "Each frame shows the Source's media as it changes.",
  authored: "You choose one item from the Source for each frame.",
};

/**
 * Step 1, Kind: live or hand-picked. The first question, because it decides whether
 * "Media per frame" is asked.
 *
 * @param {StepProps} props
 */
export function KindStep({ value, patch, problems }) {
  const name = useId();
  return (
    <fieldset id={problems.idFor("mode")} className="choices" aria-label="Scene kind">
      <legend className="choices__legend">What should this Scene show?</legend>
      {["live", "authored"].map((mode) => (
        <div key={mode} className="choices__option">
          <label className="choices__label">
            <input
              type="radio"
              name={name}
              checked={value.mode === mode}
              aria-describedby={`${name}-${mode}-hint`}
              onChange={() => patch({ mode })}
            />
            {KIND_LABELS[mode]}
          </label>
          <p id={`${name}-${mode}-hint`} className="field__hint choices__hint">
            {KIND_HINTS[mode]}
          </p>
        </div>
      ))}
    </fieldset>
  );
}

/**
 * Step 2, Photos (Scene editor › What, #36): the Photo sources the Scene plays, at least one,
 * as one list of cards (SourcePicker.jsx), each chosen by its checkbox (any of them: the Scene
 * plays the photos of all of them, C3). The last card, "New Photo source", opens the Photo
 * source editor and returns here with it chosen (`onNewSource`, a hand-off). A hand-picked Scene
 * picks from one Source (radios). Right under the cards, the Fit board of the chosen Sources on
 * the Scene's Frames (chosen on the step before), then one line of the chosen Sources' actions
 * (design fix round 3: the Frames lead; each card already says its check's state).
 *
 * @param {StepProps & {sources: Array<{source_ref: string}>, onNewSource: () => void}} props
 */
export function PhotosStep({ value, patch, problems, sources, onNewSource, sourceReadiness, fit }) {
  const set = (sourceRefs) => {
    patch({ sourceRefs });
    problems.touch("source");
  };
  const several = value.mode !== "authored";
  return (
    <>
      <p className="field__hint">
        A Photo source asks Immich for photos; Photo Wall shows them and never changes anything there.
        {several ? " Choose one or more: the Scene plays the photos of every one chosen." : " Choose the one to pick from."}
      </p>
      <SourcePicker
        id={problems.idFor("source")}
        reason={problems.reasonFor("source")}
        sources={sources}
        value={value.sourceRefs}
        single={!several}
        onChange={set}
        onNewSource={onNewSource}
      />
      {value.sourceRefs.length > 0 && <SceneFitBoard fit={fit} size="full" />}
      <ChosenSources {...sourceReadiness}
        onRemove={(ref) => set(value.sourceRefs.filter((chosen) => chosen !== ref))} />
    </>
  );
}

/**
 * Step 2, Frames: the target frames (required), grouped by wall with their health, each a tile
 * at its Frame output's shape: once the Scene has photos (`fit`, its read of every Frame), filled
 * with a photo it would show and how many fit it; before, empty at its shape. A chosen card is
 * drawn as chosen, so no separate line repeats the choice.
 *
 * @param {StepProps & {snapshot: object|null, onToggle: (frameId: string) => void,
 *          fit?: {read: import("./domain/photo-fit-board.tsx").FitRead}|null}} props
 */
export function FramesStep({ value, problems, snapshot, onToggle, fit = null }) {
  return (
    <>
      <p className="field__hint">
        {fit === null
          ? "Each Frame is drawn at its shape. Choose its photos next to see what each would show."
          : "Each Frame is drawn with a photo it would show, and how many of this Scene's photos fit it."}
      </p>
      <TargetPicker
        id={problems.idFor("targets")}
        reason={problems.reasonFor("targets")}
        snapshot={snapshot}
        tileFor={frameChoiceTiles(fit === null ? null : fit.read, snapshot, value.targets.length)}
        targets={new Set(value.targets)}
        onToggle={(frameId) => {
          onToggle(frameId);
          problems.touch("targets");
        }}
      />
    </>
  );
}

/**
 * Step 3b, Media per frame (hand-picked only): ONE item per target frame from that
 * frame's candidates (the container's `useCandidates`, profile-filtered by Central),
 * each labelled with the planner's `standing` (mediaHealth.js `candidateLabels`). A
 * failed read says so and offers Retry (`candidates.reload`); successful reads can be
 * explicitly refreshed after an unknown Source-refresh outcome.
 *
 * @param {StepProps & {candidates: ReturnType<typeof import("./useCandidates.js").useCandidates>,
 *          onSelect: (frameId: string, assetId: string) => void}} props
 */
export function MediaStep({ value, problems, candidates, onSelect }) {
  const { byFrame, loading, error: loadError } = candidates;
  return (
    <fieldset className="scene-flow__choosers" aria-label="Per-frame media choices">
      <legend>Per-frame media choices</legend>
      {value.targets.length === 0 ? (
        <p className="scene-flow__empty">Choose a Source and target Frames to pick media.</p>
      ) : (
        value.targets.map((frameId) => {
          const list = byFrame[frameId] ?? [];
          const labels = candidateLabels(list);
          const field = `media:${frameId}`;
          return (
            <Field
              key={frameId}
              id={problems.idFor(field)}
              label={`Media for frame ${frameId}`}
              reason={problems.reasonFor(field)}
            >
              {(props) => (
                <select
                  {...props}
                  value={value.selections[frameId] ?? ""}
                  onChange={(event) => {
                    onSelect(frameId, event.target.value);
                    problems.touch(field);
                  }}
                >
                  <option value="">
                    {loading
                      ? "Loading compatible media…"
                      : list.length === 0
                        ? "No compatible media"
                        : "Choose compatible media"}
                  </option>
                  {list.map((candidate, index) => (
                    <option key={candidate.asset_id} value={candidate.asset_id}>
                      {labels[index]}
                    </option>
                  ))}
                </select>
              )}
            </Field>
          );
        })
      )}
      {loadError !== null && (
        <div className="scene-flow__status" role="status">
          <p>{loadError}</p>
          <button type="button" onClick={candidates.reload}>
            Retry
          </button>
        </div>
      )}
      {loadError === null && !loading && value.targets.length > 0 && (
        <button type="button" onClick={candidates.reload}>
          Reload compatible media
        </button>
      )}
    </fieldset>
  );
}

/**
 * Step 4, Playback: "Seconds per cycle" (default 30) and "Fade between photos"; how the
 * Scene ends; "Order", "Don't repeat within" and "Crop limit" (roadmap 5a); under Advanced, "Keep repeating" (default on), "Keep
 * the last photo up" and "Keep these Frames together" (SceneSettings.jsx).
 *
 * @param {StepProps & {advanced: {open: boolean, onToggle: () => void}}} props
 */
export function PlaybackStep({ value, patch, problems, advanced, fit, houseCropLimit = null, onCropPreview }) {
  const summary = [
    `${PLAYBACK_LABELS.loop}: ${onOff(value.loop)}`,
    `${PLAYBACK_LABELS.keepLastPhoto}: ${onOff(value.keepLastPhoto)}`,
    `${PLAYBACK_LABELS.keepTogether}: ${onOff(value.keepTogether)}`,
  ].join(" · ");
  return (
    <>
      <CycleField
        problems={problems}
        seconds={value.cycleSeconds}
        onSeconds={(cycleSeconds) => patch({ cycleSeconds })}
      />
      <TimingRows value={value} patch={patch} problems={problems} />
      <EndingRows value={value} patch={patch} problems={problems} />
      <SelectionRows value={value} patch={patch} problems={problems} houseCropLimit={houseCropLimit}
        onCropPreview={onCropPreview} />
      {value.sourceRefs.length > 0 && <SceneFitBoard fit={fit} size="compact" lead="crop" />}
      <Advanced summary={summary} open={advanced.open} onToggle={advanced.onToggle}>
        <LoopField problems={problems} loop={value.loop} onLoop={(loop) => patch({ loop })} />
        <AdvancedRows value={value} patch={patch} problems={problems} />
      </Advanced>
    </>
  );
}

/**
 * Step 5, Review (follow-ups C2, after the designer review): for a new Scene its "Scene name"
 * first, with its id under Advanced only (derived from the name until the operator types one,
 * slice 3 §5; an edit keeps its stored id); then every value, the advanced ones included,
 * grouped by the step that asks it, with one "Change <step>" per group (`onChange(field)`
 * opens that step at its first field); then the compact Fit board of the Scene's photos on its
 * Frames, the last moment to see a Frame that nothing fits, with "Raise the crop limit".
 *
 * @param {StepProps & {snapshot: object|null, editingId: string|null,
 *          candidates: ReturnType<typeof import("./useCandidates.js").useCandidates>,
 *          advanced: {open: boolean, onToggle: () => void},
 *          onChange: (field: string) => void}} props
 */
export function ReviewStep({
  value,
  patch,
  problems,
  snapshot,
  editingId,
  candidates,
  advanced,
  onChange,
  fit,
}) {
  const authored = value.mode === "authored";
  const missing = <NotChosen />;
  const groups = [
    { label: "Kind", field: "mode", rows: [{ label: SCENE_ANSWER_LABELS.mode, value: KIND_LABELS[value.mode] }] },
    {
      label: FRAMES_LABEL,
      field: "targets",
      rows: [{
        label: SCENE_ANSWER_LABELS.targets,
        value: value.targets.length === 0 ? missing : <FrameChips snapshot={snapshot} frameIds={value.targets} />,
      }],
    },
    {
      label: "Photos",
      field: "source",
      rows: [{
        label: SCENE_ANSWER_LABELS.source,
        value: value.sourceRefs.length === 0 ? missing : value.sourceRefs.map(sourceName).join(", "),
      }],
    },
  ];
  if (authored) {
    groups.push({
      label: SCENE_ANSWER_LABELS.media,
      field: value.targets.length > 0 ? `media:${value.targets[0]}` : "targets",
      rows: [{
        label: SCENE_ANSWER_LABELS.media,
        value: value.targets.length === 0 ? missing : <MediaAnswers value={value} candidates={candidates} />,
      }],
    });
  }
  groups.push({
    label: "Playback",
    field: "cycle",
    rows: [
      { label: SCENE_ANSWER_LABELS.cycle, value: secondsWords(value.cycleSeconds) },
      { label: SCENE_ANSWER_LABELS.loop, value: value.loop ? onOff(true) : "Off: it plays once through" },
      { label: SCENE_ANSWER_LABELS.fade, value: secondsWords(value.fadeSeconds) },
      { label: SCENE_ANSWER_LABELS.ending, value: endingWords(value) },
      { label: SCENE_ANSWER_LABELS.keepLastPhoto, value: onOff(value.keepLastPhoto) },
      { label: SCENE_ANSWER_LABELS.keepTogether, value: onOff(value.keepTogether) },
      { label: SCENE_ANSWER_LABELS.order, value: ORDER_LABELS[value.order] },
      { label: SCENE_ANSWER_LABELS.noRepeatDays, value: daysWords(value.noRepeatDays) },
      { label: SCENE_ANSWER_LABELS.cropLimit, value: cropWords(value.cropLimit) },
    ],
  });
  const derived = idFromName(value.name);
  // The id is said once: "Saved as <id> · Change" under the name while it is derived; once it
  // is typed (or must be), the Id field under Advanced, whose summary names it only while closed.
  const idShown = value.idOverride !== null || idNeeded(value.name);
  const idOpen = advanced.open || idNeeded(value.name);
  return (
    <>
      {editingId !== null && (
        <p className="scene-flow__editing">
          {"Editing "}
          <code>{editingId}</code>
          {`. Its name stays the same.`}
        </p>
      )}
      {editingId === null && (
        <>
          <NameField
            kind="Scene"
            name={value.name}
            idShown={idShown}
            onName={(name) => patch({ name })}
            onChangeId={() => {
              patch({ idOverride: derived });
              onChange("id");
            }}
            problems={problems}
          />
          {idShown && (
            <Advanced
              summary={idOpen ? null : `Id: ${draftId(value) || "none yet"}`}
              open={advanced.open}
              lockedOpen={idNeeded(value.name)}
              onToggle={advanced.onToggle}
            >
              <IdField
                value={value.idOverride ?? derived ?? ""}
                onIdOverride={(idOverride) => patch({ idOverride })}
                problems={problems}
              />
            </Advanced>
          )}
        </>
      )}
      <section aria-label="Your answers">
      {groups.map((group) => (
        <section key={group.label} className="review__group" aria-label={`${group.label} answers`}>
          <div className="review__group-head">
            <h4 className="review__group-title">{group.label}</h4>
            <button type="button" className="review__change-button" aria-label={`Change ${group.label}`}
              onClick={() => onChange(group.field)}>
              Change
            </button>
          </div>
          <CheckAnswers rows={group.rows} onChange={onChange} label={`${group.label}: your answers`} />
        </section>
      ))}
      </section>
      {value.sourceRefs.length > 0 && <SceneFitBoard fit={fit} size="strip" />}
    </>
  );
}

/** The Frames step's label, as Review's group names it. */
const FRAMES_LABEL = "Frames";

/**
 * The chosen Photo sources' actions on one line (design fix round 3): each Source's name, a note
 * only when its photos are not ready (pool, read here), Check now's receipt, Check now and
 * Remove; then Manage in Sources (the Scene draft stays open in this tab meanwhile). Each card
 * above already says its check's state, so the line does not repeat it; a Source whose check
 * failed also gets the error template, with its one fix, right under the line
 * (SourceCheckProblem: a failure is a block, never a line of body text).
 */
function ChosenSources({ chosen, check, onManage, onRemove }) {
  if (chosen.length === 0) return null;
  const failed = chosen.filter(({ source }) => source?.check?.state === "failed");
  return (
    <section className="chosen-sources" aria-label="Source media status">
      <ul className="chosen-sources__list">
        {chosen.map(({ ref, source }) => (
          <ChosenSource key={ref} sourceRef={ref} source={source} check={check} onRemove={onRemove} />
        ))}
      </ul>
      <button type="button" className="chosen-sources__manage" onClick={onManage}>Manage in Sources</button>
      {failed.length > 0 && (
        <div className="chosen-sources__problems">
          {failed.map(({ ref, source }) => (
            <SourceCheckProblem key={ref} name={sourceName(source)} check={source.check} />
          ))}
        </div>
      )}
    </section>
  );
}

function ChosenSource({ sourceRef, source, check, onRemove }) {
  const name = sourceName(source ?? sourceRef);
  const receipt = source === null ? null : sourceCheckMessage(check.feedback[sourceRef], source.check);
  return (
    <li aria-label={name} className="chosen-sources__item">
      <strong className="chosen-sources__name">{name}</strong>
      {source === null
        ? <span className="chosen-sources__note">No longer current: choose a current Source</span>
        : <PoolReadiness sourceRef={sourceRef} />}
      {receipt !== null && <span className="chosen-sources__note" role="status">{receipt}</span>}
      {source !== null && (
        <button type="button" aria-label={`Check ${name} now`} disabled={check.pending !== null}
          onClick={() => void check.run(sourceRef, source.check ?? null)}>
          Check now
        </button>
      )}
      <button type="button" aria-label={`Remove ${name}`} onClick={() => onRemove(sourceRef)}>Remove</button>
    </li>
  );
}

/** Whether a chosen Source's photos are ready, from its pool; nothing when they are. */
function PoolReadiness({ sourceRef }) {
  const { view } = useDraftPool({ draftRef: null, sourceRef });
  if (view === null) return null;
  const empty = view.in_pool.photos + view.in_pool.videos === 0;
  const state = view.check?.state;
  if (state === "checking" || state === "never_checked") {
    return <span className="chosen-sources__note">Still being read: what is found so far plays.</span>;
  }
  return empty ? <span className="chosen-sources__note">This Source has no photos.</span> : null;
}

/** Each target frame's chosen item, by its chooser label once the candidates are read. */
function MediaAnswers({ value, candidates }) {
  return (
    <ul className="review__media">
      {value.targets.map((frameId) => {
        const assetId = value.selections[frameId];
        const list = candidates.byFrame[frameId] ?? [];
        const index = list.findIndex((candidate) => candidate.asset_id === assetId);
        const label = index === -1 ? assetId : candidateLabels(list)[index];
        return <li key={frameId}>{`${frameId}: ${assetId ? label : "not chosen"}`}</li>;
      })}
    </ul>
  );
}
