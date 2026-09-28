import React from "react";

import { editableDraft, normalizeScene, UNAUTHORABLE_REASON } from "./authoring.js";
import { frameOf, LIVE_PHASES } from "./join.js";
import { cycleWording } from "./showState.js";
import { FrameChips } from "./TargetPicker.jsx";

/**
 * The stored Scenes (pass 2 slice 3 §13), each a disclosure named `Scene X`:
 * what feeds it (live Sources, or the number of hand-picked items), its target
 * frames with their health, its cycle and loop wording, its revision, the
 * Programs that name it and whether a Run of it is live. Every fact is read
 * from the served runtime payload (`definitions`, `programs`, `current.runs`).
 *
 * Edit is offered only when the console can save the Scene back without loss
 * (authoring.js `editableDraft`); otherwise it is withheld with the reason.
 * Delete is not offered (slice 3 Question 3).
 *
 * @param {{snapshot: object|null,
 *          onEdit: (scene: object, draft: ReturnType<typeof editableDraft>) => void}} props
 */
export function SceneList({ snapshot, onEdit }) {
  const runtime = snapshot?.runtime;
  const scenes = Object.values(runtime?.definitions ?? {});
  if (scenes.length === 0) {
    return <p className="scene-authoring__empty">No Scenes yet.</p>;
  }
  const programs = Object.values(runtime?.programs ?? {});
  const live = (runtime?.current?.runs ?? []).filter((run) => LIVE_PHASES.has(run.phase));
  return (
    <ul className="scene-authoring__scenes" role="list">
      {scenes.map((scene) => (
        <SceneRow
          key={scene.scene_id}
          scene={scene}
          snapshot={snapshot}
          usedBy={programs.filter((program) => program.scene_id === scene.scene_id)}
          running={live.some((run) => run.scene_id === scene.scene_id)}
          onEdit={onEdit}
        />
      ))}
    </ul>
  );
}

/** What feeds a Scene: its live Sources, or how many items were hand-picked. */
function feedWording(contributions) {
  const assets = new Set(contributions.flatMap((entry) => entry.asset_refs));
  const sources = [...new Set(contributions.flatMap((entry) => entry.source_refs))];
  if (assets.size > 0) {
    return `authored: ${assets.size} chosen ${assets.size === 1 ? "item" : "items"}`;
  }
  return sources.length > 0 ? `live from ${sources.join(", ")}` : "no media";
}

function SceneRow({ scene, snapshot, usedBy, running, onEdit }) {
  const filled = normalizeScene(scene);
  const once = cycleWording(scene);
  const draft = editableDraft(scene);
  const frames = filled.contributions
    .map((entry) => frameOf(entry.target))
    .filter((frameId) => frameId !== null);
  return (
    <li className="scene-authoring__scene" aria-label={`Scene ${scene.scene_id}`}>
      <details className="scene-list__details">
        <summary className="scene-list__summary">
          <span className="scene-authoring__scene-id">{`Scene ${scene.scene_id}`}</span>
          {once !== null && <span className="scene-authoring__scene-cycle">{` · ${once}`}</span>}
          {running && <span className="scene-list__running">{" · Running now"}</span>}
        </summary>
        <dl className="record">
          <dt>Media</dt>
          <dd>{feedWording(filled.contributions)}</dd>
          <dt>Frames</dt>
          <dd>
            {frames.length > 0 ? <FrameChips snapshot={snapshot} frameIds={frames} /> : "none"}
          </dd>
          <dt>Cycle</dt>
          <dd>
            {once ??
              `${Number(filled.cycle_seconds)} s per cycle, keeps playing until its Program ends ` +
                "or, when started by hand, until you Finish or Cancel it"}
          </dd>
          <dt>Revision</dt>
          <dd>{`revision ${filled.revision}`}</dd>
          <dt>Used by</dt>
          <dd>
            {usedBy.length > 0
              ? `Programs ${usedBy.map((program) => program.program_id).join(", ")}`
              : "no Program"}
          </dd>
          <dt>Now</dt>
          <dd>{running ? "Running now" : "Not running"}</dd>
        </dl>
        {draft !== null ? (
          <div className="record__actions">
            <button
              type="button"
              aria-label={`Edit Scene ${scene.scene_id}`}
              onClick={() => onEdit(scene, draft)}
            >
              Edit
            </button>
          </div>
        ) : (
          <p className="field__hint scene-list__withheld">{`Edit unavailable: ${UNAUTHORABLE_REASON}`}</p>
        )}
      </details>
    </li>
  );
}
