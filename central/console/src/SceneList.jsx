import React from "react";

import { apiWrite } from "./apiWrite.js";
import { editableDraft, normalizeScene, UNAUTHORABLE_REASON } from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { SummaryCard } from "./flow/SummaryCard.jsx";
import { frameOf, LIVE_PHASES } from "./join.js";
import { cycleWording } from "./showState.js";
import { FrameChips } from "./TargetPicker.jsx";
import { sourceName } from "./sourceNames.js";

/**
 * The stored Scenes as cards (pass 2 slice 3 §13; flow design §7): each a summary
 * card named `Scene X` with what feeds it (live Sources, or the number of hand-picked
 * items), its target frames with their health, its cycle and loop wording, and the
 * Programs that name it; a "Running now" chip while a Run of it is
 * live. Every fact is read from the served runtime payload (`definitions`,
 * `programs`, `current.runs`).
 *
 * Actions: Edit, offered only when the console can save the Scene back without loss
 * (authoring.js `editableDraft`; otherwise withheld with the reason); Show now,
 * Schedule it, and guarded Delete. Edit is disabled while
 * `editDisabled` (the Scene flow's write is in flight: its draft is held).
 *
 * @param {{snapshot: object|null, editDisabled?: boolean,
 *          onEdit: (sceneId: string, event: React.MouseEvent) => void,
 *          onShowNow: (sceneId: string) => void,
 *          onSchedule: (sceneId: string) => void}} props
 */
export function SceneList({ snapshot, editDisabled = false, onEdit, onShowNow, onSchedule, onDeleted, editingId = null, editingDirty = false }) {
  const runtime = snapshot?.runtime;
  const scenes = Object.values(runtime?.definitions ?? {});
  const programs = Object.values(runtime?.programs ?? {});
  const live = (runtime?.current?.runs ?? []).filter((run) => LIVE_PHASES.has(run.phase));
  const { open, confirmation } = useConfirm(null, (_result, request) => onDeleted?.(request.sceneId));
  if (scenes.length === 0) {
    return <>
      <p className="scene-list__empty">No Scenes yet.</p>
      {confirmation("scene-list__status")}
    </>;
  }
  const remove = (event, scene) => {
    const id = scene.scene_id;
    const programsForScene = programs.filter((program) => program.scene_id === id);
    const runsForScene = live.filter((run) => run.scene_id === id);
    const containsScene = (candidate) => candidate.children?.some?.((child) =>
      child.scene?.scene_id === id || containsScene(child.scene)) ?? false;
    const parents = scenes.filter((candidate) => candidate.scene_id !== id && containsScene(candidate));
    const editingThisDirty = editingId === id && editingDirty;
    const dependentNames = [
      ...programsForScene.map((item) => `Program ${item.program_id}`),
      ...runsForScene.map((item) => `active Run ${item.run_id}`),
      ...parents.map((item) => `stored Scene ${item.scene_id}`),
    ];
    const impacts = dependentNames.length ? ` Current references: ${dependentNames.join(", ")}.` : " The server will check for other references before deleting it.";
    open(event, {
      key: `delete-scene:${id}:${scene.revision}`,
      sceneId: id,
      title: `Delete Scene ${id}?`,
      confirmLabel: "Confirm delete",
      body: <>
        <p>{`This removes Scene ${id} from future choices. It never stops a Run.${editingThisDirty ? ` Your unsaved edits to Scene ${id} will be discarded.` : ""}`}</p>
        <p>{`Deletion is refused while stored work refers to it.${impacts}`}</p>
      </>,
      run: async () => {
        const result = await apiWrite(`/v1/operator/scenes/${encodeURIComponent(id)}?expected_revision=${scene.revision}`, { method: "DELETE" });
        if (result.ok) return { state: "done", message: `Scene ${id} deleted.` };
        if (result.error === "scene_revision_conflict" || result.error === "scene_missing") return { state: "changed", message: result.error === "scene_missing" ? `Scene ${id} no longer exists; refresh the list.` : `Scene ${id} changed since this card was loaded. Review the refreshed Scene before retrying.` };
        if (result.error === "scene_in_use") {
          const ids = result.data ?? {};
          const names = [
            ...(ids.program_ids ?? []).map((value) => `Program ${value}`),
            ...(ids.run_ids ?? []).map((value) => `Run ${value}`),
            ...(ids.queued_activation_ids ?? []).map((value) => `queued activation ${value}`),
            ...(ids.scene_ids ?? []).map((value) => `stored Scene ${value}`),
          ];
          return { state: "refused", message: names.length ? `Still referenced by ${names.join(", ")}. Remove or edit that work before deleting Scene ${id}.` : `Scene ${id} is still in use. Remove its references before deleting it.` };
        }
        return { state: "refused", message: `Could not delete Scene ${id}: ${result.error ?? result.status}.` };
      },
    });
  };
  return (
    <>
      <ul className="card-grid scene-list" role="list">
        {scenes.map((scene) => (
          <li key={scene.scene_id} className="card-grid__item">
            <SceneCard
              scene={scene}
              snapshot={snapshot}
              usedBy={programs.filter((program) => program.scene_id === scene.scene_id)}
              running={live.some((run) => run.scene_id === scene.scene_id)}
              editDisabled={editDisabled}
              onEdit={onEdit}
              onShowNow={onShowNow}
              onSchedule={onSchedule}
              onDelete={remove}
            />
          </li>
        ))}
      </ul>
      {confirmation("scene-list__status")}
    </>
  );
}

/** What feeds a Scene: its live Sources, or how many items were hand-picked. */
function feedWording(contributions) {
  const assets = new Set(contributions.flatMap((entry) => entry.asset_refs));
  const sources = [...new Set(contributions.flatMap((entry) => entry.source_refs))];
  if (assets.size > 0) {
    return `authored: ${assets.size} chosen ${assets.size === 1 ? "item" : "items"}`;
  }
  return sources.length > 0 ? `live from ${sources.map(sourceName).join(", ")}` : "no media";
}

function SceneCard({ scene, snapshot, usedBy, running, editDisabled, onEdit, onShowNow, onSchedule, onDelete }) {
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
            <button
              type="button"
              aria-label={`Edit Scene ${id}`}
              disabled={editDisabled}
              onClick={(event) => onEdit(id, event)}
            >
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
          <button type="button" aria-label={`Delete Scene ${id}`} onClick={(event) => onDelete(event, scene)}>
            Delete
          </button>
        </>
      }
    />
  );
}
