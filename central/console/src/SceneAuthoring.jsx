import React, { useCallback, useMemo, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { useMutate } from "./useMutate.js";

/**
 * Scene authoring shell (Bead 14a — SR-scenes-live).
 *
 * A Scene is a per-target COMPOSITION of contributions (design J4). This shell
 * authors and saves a LIVE-SOURCE scene: a Scene whose media for each target
 * Frame is driven by a live Source, targeting one or more EXPLICIT Frames, on a
 * fixed seconds-per-cycle interval. The saved Scene then appears in the Scenes
 * list by its `scene_id` (tiles carry the id, never a name — design J4).
 *
 * A live-source scene saves in ONE request to the plain scene route
 * `PUT /v1/operator/scenes/{scene_id}` with the Scene as the body: each target
 * Frame becomes a media Contribution `{target:"frame:<id>", role:"<id>",
 * source_refs:[<sourceRef>], kind:"media", retain_on_expiry:true}` (mirroring
 * the legacy operator.js live branch, ~operator.js:416). The write goes through
 * the shared `apiWrite` helper wrapped in `useMutate()` (primitive #7), so Plane
 * A — and therefore the Scenes list below — refreshes exactly once after the
 * save.
 *
 * SEAM for Bead 14b (SR-scenes-authored): the authoring MODE is the split
 * boundary. This component drives its payload+route through `buildSave(mode,…)`,
 * dispatched on `mode`; Bead 14a registers only the "live" branch. Bead 14b adds
 * an "authored" mode — its per-frame candidate choosers (a clearly-separable
 * subtree rendered where {@link ModeControls} branches) and its single-request
 * `PUT …/scenes/{id}/authored` payload (scene + source_ref + asset_ids) — WITHOUT
 * reshaping this component: it adds a mode option, a chooser subtree, and an
 * "authored" case to `buildSave`. The profile hard-filter on candidates is 14b,
 * NOT here. This bead deliberately builds ONLY the live branch.
 *
 * @param {{snapshot: object|null}} props
 */
export function SceneAuthoring({ snapshot }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const sources = snapshot?.media?.sources ?? [];
  // Existing scenes come from the runtime definitions map, keyed by scene_id
  // (central/app.py:669 -> runtime.export_state()["scenes"]). Tiles show the id.
  const definitions = snapshot?.runtime?.definitions ?? {};
  const scenes = useMemo(() => Object.values(definitions), [definitions]);

  const mutate = useMutate();

  // Plane B: component-local authoring draft. `mode` is the 14a/14b split seam;
  // 14a only offers "live", so there is no user-facing mode toggle yet (a lone
  // "live" toggle would be a broken affordance) — 14b introduces the toggle when
  // it adds the "authored" mode alongside.
  const [mode] = useState("live");
  const [sceneId, setSceneId] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [targets, setTargets] = useState(/** @type {Set<string>} */ (new Set()));
  const [cycleSeconds, setCycleSeconds] = useState(30);
  const [status, setStatus] = useState(/** @type {string|null} */ (null));
  const [saving, setSaving] = useState(false);

  const toggleTarget = useCallback((frameId) => {
    setTargets((prev) => {
      const next = new Set(prev);
      if (next.has(frameId)) {
        next.delete(frameId);
      } else {
        next.add(frameId);
      }
      return next;
    });
  }, []);

  const targetIds = useMemo(() => [...targets], [targets]);
  const canSave =
    !saving &&
    sceneId.trim() !== "" &&
    sourceRef !== "" &&
    targetIds.length > 0 &&
    Number(cycleSeconds) > 0;

  const onSave = useCallback(
    async (event) => {
      event.preventDefault();
      if (!canSave) {
        return;
      }
      const { path, body } = buildSave(mode, {
        sceneId: sceneId.trim(),
        sourceRef,
        targetIds,
        cycleSeconds: Number(cycleSeconds),
      });
      setSaving(true);
      setStatus(null);
      try {
        // ONE request: the scene is saved in a single PUT; useMutate() then does
        // its one Plane A refresh so the Scenes list reflects the new scene.
        const result = await mutate(() =>
          apiWrite(path, { method: "PUT", body }),
        );
        setStatus(
          result.ok
            ? `Saved Scene ${body.scene_id}.`
            : `Could not save Scene: ${result.error ?? result.status}.`,
        );
      } catch {
        setStatus("Could not save Scene: the request did not complete.");
      } finally {
        setSaving(false);
      }
    },
    [canSave, mode, sceneId, sourceRef, targetIds, cycleSeconds, mutate],
  );

  return (
    <div className="scene-authoring">
      <form
        className="scene-authoring__form"
        role="form"
        aria-label="Author a Scene"
        onSubmit={onSave}
      >
        <label className="scene-authoring__field">
          Scene ID
          <input
            type="text"
            className="scene-authoring__scene-id"
            aria-label="Scene ID"
            value={sceneId}
            onChange={(event) => setSceneId(event.target.value)}
          />
        </label>

        <ModeControls
          mode={mode}
          sources={sources}
          sourceRef={sourceRef}
          onSource={setSourceRef}
          frames={frames}
          targets={targets}
          onToggleTarget={toggleTarget}
          cycleSeconds={cycleSeconds}
          onCycleSeconds={setCycleSeconds}
        />

        <button
          type="submit"
          className="scene-authoring__save"
          disabled={!canSave}
        >
          Save Scene
        </button>
      </form>

      {status !== null ? (
        <p className="scene-authoring__status" role="status">
          {status}
        </p>
      ) : null}

      {/* The Scenes list: every saved Scene by its scene_id (design J4 — the id
          is the tile, never a name). The just-saved scene appears here after the
          useMutate() refresh. */}
      {scenes.length === 0 ? (
        <p className="scene-authoring__empty">No Scenes yet.</p>
      ) : (
        <ul className="scene-authoring__scenes" role="list">
          {scenes.map((scene) => (
            <li
              key={scene.scene_id}
              className="scene-authoring__scene"
              aria-label={`Scene ${scene.scene_id}`}
            >
              {scene.scene_id}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * The per-mode authoring controls. 14a renders the LIVE-source controls (pick a
 * Source, pick target Frames, set seconds-per-cycle). SEAM: 14b adds an
 * `mode === "authored"` branch here rendering its per-frame candidate-chooser
 * subtree, without touching the live branch.
 */
function ModeControls({
  mode,
  sources,
  sourceRef,
  onSource,
  frames,
  targets,
  onToggleTarget,
  cycleSeconds,
  onCycleSeconds,
}) {
  // 14a: the only mode is "live". 14b registers "authored" alongside.
  if (mode !== "live") {
    return null;
  }
  return (
    <fieldset className="scene-authoring__live" aria-label="Live source">
      <label className="scene-authoring__field">
        Source
        <select
          className="scene-authoring__source"
          aria-label="Source"
          value={sourceRef}
          onChange={(event) => onSource(event.target.value)}
        >
          <option value="">Choose a Source</option>
          {sources.map((source) => (
            <option key={source.source_ref} value={source.source_ref}>
              {source.source_ref}
            </option>
          ))}
        </select>
      </label>

      <fieldset className="scene-authoring__targets" aria-label="Target frames">
        <legend>Target frames</legend>
        {frames.length === 0 ? (
          <p className="scene-authoring__empty">No Frames to target.</p>
        ) : (
          frames.map((frame) => (
            <label key={frame.id} className="scene-authoring__target">
              <input
                type="checkbox"
                aria-label={`Target frame ${frame.id}`}
                checked={targets.has(frame.id)}
                onChange={() => onToggleTarget(frame.id)}
              />
              {frame.id}
            </label>
          ))
        )}
      </fieldset>

      <label className="scene-authoring__field">
        Seconds per cycle
        <input
          type="number"
          min="1"
          className="scene-authoring__cycle"
          aria-label="Seconds per cycle"
          value={cycleSeconds}
          onChange={(event) => onCycleSeconds(event.target.value)}
        />
      </label>
    </fieldset>
  );
}

/**
 * Build the save {path, body} for the given authoring mode (the 14a/14b seam).
 * 14a implements only "live": the plain scene route with the Scene as the body.
 *
 * @param {"live"} mode
 * @param {{sceneId: string, sourceRef: string, targetIds: string[], cycleSeconds: number}} draft
 * @returns {{path: string, body: object}}
 */
export function buildSave(mode, { sceneId, sourceRef, targetIds, cycleSeconds }) {
  if (mode !== "live") {
    // SEAM: Bead 14b adds the "authored" case here, returning the authored route
    // `PUT …/scenes/{id}/authored` and its {scene, source_ref, asset_ids} body.
    throw new Error(`unsupported scene authoring mode: ${mode}`);
  }
  // A live-source Scene: one media Contribution per target Frame, all driven by
  // the chosen Source. `target` is the verified string "frame:<id>" (design
  // §1b); role carries the frame id; retain_on_expiry keeps the last still.
  const contributions = targetIds.map((frameId) => ({
    target: `frame:${frameId}`,
    role: frameId,
    kind: "media",
    source_refs: [sourceRef],
    retain_on_expiry: true,
  }));
  const scene = {
    scene_id: sceneId,
    revision: 1,
    cycle_seconds: cycleSeconds,
    loop: false,
    contributions,
  };
  return {
    path: `/v1/operator/scenes/${encodeURIComponent(sceneId)}`,
    body: scene,
  };
}
