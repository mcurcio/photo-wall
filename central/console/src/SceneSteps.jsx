import React, { useId } from "react";

import { draftId, idFromName, PLAYBACK_LABELS } from "./authoring.js";
import { CycleField, LoopField } from "./CycleInput.jsx";
import { Field, IdField, idNeeded, NameField } from "./Field.jsx";
import { Advanced } from "./flow/Advanced.jsx";
import { CheckAnswers, NotChosen } from "./flow/CheckAnswers.jsx";
import { candidateLabels, sourceState } from "./mediaHealth.js";
import { SCENE_ANSWER_LABELS } from "./sceneFlowModel.js";
import { AdvancedRows, EndingRows, endingWords, onOff, secondsWords, TimingRows } from "./SceneSettings.jsx";
import { SourcePicker } from "./SourcePicker.jsx";
import { sourceName } from "./sourceNames.js";
import { FrameChips, TargetPicker } from "./TargetPicker.jsx";
import { sourceRefreshMessage } from "./useSourceRefresh.js";

/**
 * The Scene flow's step views (flow design §7 J4): views over the draft that
 * SceneFlow.jsx owns. They hold no draft state and run no draft effect (the
 * container runs the prunes, whichever step shows); each renders its fields through
 * the shared pickers and Field.jsx, so labels and accessible names are the ones the
 * single form had (rule 3): "Source", "Target frame <id>", "Media for frame <id>",
 * "Seconds per cycle", "Keep playing until the Program ends", "Scene name", "Id".
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
 * Step 2, Photos: the Source (required), or a new selection from the photo library
 * (`onNewSource`: the Source flow, run inline; it returns here).
 *
 * @param {StepProps & {sources: Array<{source_ref: string}>, onNewSource: () => void}} props
 */
export function PhotosStep({ value, patch, problems, sources, onNewSource, sourceReadiness }) {
  return (
    <>
      <p className="field__hint">
        A Source is a saved selection from your photo library; Photo Wall shows its media and
        never changes anything there.
      </p>
      <SourcePicker
        id={problems.idFor("source")}
        reason={problems.reasonFor("source")}
        sources={sources}
        value={value.sourceRef}
        onChange={(sourceRef) => {
          patch({ sourceRef });
          problems.touch("source");
        }}
      />
      <SourceReadiness {...sourceReadiness} />
      <div className="record__actions">
        <button type="button" onClick={onNewSource}>
          New selection from your photo library
        </button>
      </div>
    </>
  );
}

/**
 * Step 3, Frames: the target frames (required), grouped by wall with their health.
 *
 * @param {StepProps & {snapshot: object|null, onToggle: (frameId: string) => void}} props
 */
export function FramesStep({ value, problems, snapshot, onToggle }) {
  return (
    <>
      <TargetPicker
        id={problems.idFor("targets")}
        reason={problems.reasonFor("targets")}
        snapshot={snapshot}
        targets={new Set(value.targets)}
        onToggle={(frameId) => {
          onToggle(frameId);
          problems.touch("targets");
        }}
      />
      {value.targets.length > 0 && (
        <p className="scene-flow__chosen">
          <span className="scene-flow__chosen-label">Chosen: </span>
          <FrameChips snapshot={snapshot} frameIds={value.targets} />
        </p>
      )}
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
 * Scene ends; under Advanced, "Keep playing until the Program ends" (default on), "Keep
 * the last photo up" and "Keep these Frames together" (SceneSettings.jsx).
 *
 * @param {StepProps & {advanced: {open: boolean, onToggle: () => void}}} props
 */
export function PlaybackStep({ value, patch, problems, advanced }) {
  const summary = [
    `Loop: ${onOff(value.loop)}`,
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
      <Advanced summary={summary} open={advanced.open} onToggle={advanced.onToggle}>
        <LoopField problems={problems} loop={value.loop} onLoop={(loop) => patch({ loop })} />
        <AdvancedRows value={value} patch={patch} problems={problems} />
      </Advanced>
    </>
  );
}

/**
 * Step 5, Review: a check-answers list of every value, the advanced ones included,
 * each with a "Change" button (`onChange(field)` routes to the field's step); then,
 * for a new Scene, "Scene name" and, under Advanced, its "Id" (derived from the name
 * until the operator types one, slice 3 §5). An edit keeps its stored id.
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
  sourceReadiness,
}) {
  const authored = value.mode === "authored";
  const missing = <NotChosen />;
  const rows = [
    { label: SCENE_ANSWER_LABELS.mode, field: "mode", value: KIND_LABELS[value.mode] },
    {
      label: SCENE_ANSWER_LABELS.source,
      field: "source",
      value: value.sourceRef === "" ? missing : sourceName(value.sourceRef),
    },
    {
      label: SCENE_ANSWER_LABELS.targets,
      field: "targets",
      value:
        value.targets.length === 0 ? missing : <FrameChips snapshot={snapshot} frameIds={value.targets} />,
    },
  ];
  if (authored) {
    rows.push({
      label: SCENE_ANSWER_LABELS.media,
      field: value.targets.length > 0 ? `media:${value.targets[0]}` : "targets",
      value: value.targets.length === 0 ? missing : <MediaAnswers value={value} candidates={candidates} />,
    });
  }
  rows.push(
    { label: SCENE_ANSWER_LABELS.cycle, field: "cycle", value: String(value.cycleSeconds) },
    {
      label: SCENE_ANSWER_LABELS.loop,
      field: "loop",
      value: value.loop ? "Yes" : "No, it plays one cycle",
    },
    { label: SCENE_ANSWER_LABELS.fade, field: "fade", value: secondsWords(value.fadeSeconds) },
    { label: SCENE_ANSWER_LABELS.ending, field: "ending", value: endingWords(value) },
    { label: SCENE_ANSWER_LABELS.keepLastPhoto, field: "keepLastPhoto", value: onOff(value.keepLastPhoto) },
    { label: SCENE_ANSWER_LABELS.keepTogether, field: "keepTogether", value: onOff(value.keepTogether) },
  );
  const derived = idFromName(value.name);
  if (editingId === null) {
    rows.push({ label: "Id", field: "id", value: draftId(value) || missing });
  }
  return (
    <>
      {editingId !== null && (
        <p className="scene-flow__editing">
          {"Editing "}
          <code>{editingId}</code>
          {`. Its name stays the same.`}
        </p>
      )}
      <CheckAnswers rows={rows} onChange={onChange} />
      <SourceReadiness {...sourceReadiness} />
      {editingId === null && (
        <>
          <NameField
            kind="Scene"
            name={value.name}
            idShown={value.idOverride !== null || idNeeded(value.name)}
            onName={(name) => patch({ name })}
            onChangeId={() => {
              patch({ idOverride: derived });
              onChange("id");
            }}
            problems={problems}
          />
          <Advanced
            summary={`Id: ${draftId(value) || "none yet"}`}
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
        </>
      )}
    </>
  );
}

/** Current Source health and its effect on the Scene, shared by Photos and Review. */
function SourceReadiness({ source, historicalRef, now, feedback, refreshing, onRefresh, onManage }) {
  if (source === null && !historicalRef) return null;
  const state = source === null ? null : sourceState(source, now, false);
  const consequence = state === null
    ? "Status is unavailable for this saved reference. Choose a current Source to check its media status."
    : state.state === "ok"
      ? "A recent refresh found media; each Frame still needs compatible, prepared content."
      : state.state === "empty"
        ? "The last refresh found no valid items, so this Scene may have nothing to show."
        : state.state === "failing"
          ? "The last refresh failed, so this Scene may have no current media."
          : state.state === "overdue"
            ? "The catalog may be stale, so this Scene may not show the latest media."
            : "No catalog has been loaded yet, so this Scene may have nothing to show.";
  const message = sourceRefreshMessage(feedback, source, state);
  return (
    <section className={`notice scene-flow__source-readiness${state !== null && state.severity !== "ok" ? " notice--warn" : ""}`} aria-label="Source media status">
      <p><strong>Source status:</strong> {state?.label ?? "No longer current"}</p>
      <p>{consequence}</p>
      {message !== null && <p role="status">{message}</p>}
      {state !== null && state.state !== "ok" && (
        <button type="button" disabled={refreshing} onClick={onRefresh}>
          {refreshing ? "Requesting refresh…" : "Refresh Source"}
        </button>
      )}
      {(state === null || state.state !== "ok") && (
        <div className="scene-flow__source-manage">
          <p>Your Scene draft stays open in this tab while you manage Sources.</p>
          <button type="button" onClick={onManage}>Manage in Sources</button>
        </div>
      )}
    </section>
  );
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
