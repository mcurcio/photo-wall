import React from "react";

import { editableDraft, normalizeScene, UNAUTHORABLE_REASON } from "./authoring.js";
import { SummaryCard } from "./flow/SummaryCard.jsx";
import { frameOf, LIVE_PHASES } from "./join.js";
import { cycleWording } from "./showState.js";
import { FrameChips } from "./TargetPicker.jsx";

/**
 * The stored Scenes as cards (pass 2 slice 3 §13; flow design §7): each a summary
 * card named `Scene X` with what feeds it (live Sources, or the number of hand-picked
 * items), its target frames with their health, its cycle and loop wording, its
 * revision and the Programs that name it; a "Running now" chip while a Run of it is
 * live. Every fact is read from the served runtime payload (`definitions`,
 * `programs`, `current.runs`).
 *
 * Actions: Edit, offered only when the console can save the Scene back without loss
 * (authoring.js `editableDraft`; otherwise withheld with the reason); Show now; and
 * Schedule it. Delete is not offered (slice 3 Question 3).
 *
 * @param {{snapshot: object|null,
 *          onEdit: (sceneId: string, event: React.MouseEvent) => void,
 *          onShowNow: (sceneId: string) => void,
 *          onSchedule: (sceneId: string) => void}} props
 */
export function SceneList({ snapshot, onEdit, onShowNow, onSchedule }) {
  const runtime = snapshot?.runtime;
  const scenes = Object.values(runtime?.definitions ?? {});
  if (scenes.length === 0) {
    return <p className="scene-list__empty">No Scenes yet.</p>;
  }
  const programs = Object.values(runtime?.programs ?? {});
  const live = (runtime?.current?.runs ?? []).filter((run) => LIVE_PHASES.has(run.phase));
  return (
    <ul className="card-grid scene-list" role="list">
      {scenes.map((scene) => (
        <li key={scene.scene_id} className="card-grid__item">
          <SceneCard
            scene={scene}
            snapshot={snapshot}
            usedBy={programs.filter((program) => program.scene_id === scene.scene_id)}
            running={live.some((run) => run.scene_id === scene.scene_id)}
            onEdit={onEdit}
            onShowNow={onShowNow}
            onSchedule={onSchedule}
          />
        </li>
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

function SceneCard({ scene, snapshot, usedBy, running, onEdit, onShowNow, onSchedule }) {
  const id = scene.scene_id;
  const filled = normalizeScene(scene);
  const once = cycleWording(scene);
  const editable = editableDraft(scene) !== null;
  const frames = filled.contributions
    .map((entry) => frameOf(entry.target))
    .filter((frameId) => frameId !== null);
  return (
    <SummaryCard
      title={`Scene ${id}`}
      chip={running ? { tone: "ok", text: "Running now" } : null}
      lines={[
        { label: "Media", value: feedWording(filled.contributions) },
        {
          label: "Frames",
          value: frames.length > 0 ? <FrameChips snapshot={snapshot} frameIds={frames} /> : "none",
        },
        {
          label: "Cycle",
          value:
            once ??
            `${Number(filled.cycle_seconds)} s per cycle, keeps playing until its Program ends ` +
              "or, when started by hand, until you Finish or Cancel it",
        },
        { label: "Revision", value: `revision ${filled.revision}` },
        {
          label: "Used by",
          value:
            usedBy.length > 0
              ? `Programs ${usedBy.map((program) => program.program_id).join(", ")}`
              : "no Program",
        },
      ]}
      actions={
        <>
          {editable ? (
            <button type="button" aria-label={`Edit Scene ${id}`} onClick={(event) => onEdit(id, event)}>
              Edit
            </button>
          ) : (
            <p className="field__hint scene-list__withheld">{`Edit unavailable: ${UNAUTHORABLE_REASON}`}</p>
          )}
          <button type="button" aria-label={`Show Scene ${id} now`} onClick={() => onShowNow(id)}>
            Show now
          </button>
          <button type="button" aria-label={`Schedule Scene ${id}`} onClick={() => onSchedule(id)}>
            Schedule it
          </button>
        </>
      }
    />
  );
}
