import React, { useCallback, useEffect, useMemo, useState } from "react";

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
  // both modes now exist, so a user-facing toggle (live <-> authored) selects
  // between a live-source Scene and a per-Frame authored Scene.
  const [mode, setMode] = useState("live");
  const [sceneId, setSceneId] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [targets, setTargets] = useState(/** @type {Set<string>} */ (new Set()));
  const [cycleSeconds, setCycleSeconds] = useState(30);
  // Authored mode only: the chosen asset per target Frame, keyed by frame id.
  const [selections, setSelections] = useState(
    /** @type {Record<string, string>} */ ({}),
  );
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
    // A Frame that is no longer targeted keeps no per-Frame choice.
    setSelections((prev) => {
      if (!(frameId in prev)) {
        return prev;
      }
      const next = { ...prev };
      delete next[frameId];
      return next;
    });
  }, []);

  const onSelect = useCallback((frameId, assetId) => {
    setSelections((prev) => {
      if (assetId === "") {
        if (!(frameId in prev)) {
          return prev;
        }
        const next = { ...prev };
        delete next[frameId];
        return next;
      }
      return { ...prev, [frameId]: assetId };
    });
  }, []);

  // Per-Frame choices are only valid within one Source's catalog, so clear them
  // whenever the Source changes.
  const clearSelections = useCallback(() => setSelections({}), []);

  const targetIds = useMemo(() => [...targets], [targets]);
  const canSave =
    !saving &&
    sceneId.trim() !== "" &&
    sourceRef !== "" &&
    targetIds.length > 0 &&
    Number(cycleSeconds) > 0 &&
    // Authored mode additionally requires a chosen asset for every target Frame.
    (mode !== "authored" || targetIds.every((frameId) => selections[frameId]));

  const onSave = useCallback(
    async (event) => {
      event.preventDefault();
      if (!canSave) {
        return;
      }
      const trimmedId = sceneId.trim();
      const { path, body } = buildSave(mode, {
        sceneId: trimmedId,
        sourceRef,
        targetIds,
        cycleSeconds: Number(cycleSeconds),
        selections,
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
            ? `Saved Scene ${trimmedId}.`
            : `Could not save Scene: ${result.error ?? result.status}.`,
        );
      } catch {
        setStatus("Could not save Scene: the request did not complete.");
      } finally {
        setSaving(false);
      }
    },
    [canSave, mode, sceneId, sourceRef, targetIds, cycleSeconds, selections, mutate],
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

        <fieldset
          className="scene-authoring__mode"
          aria-label="Authoring mode"
        >
          <legend>Authoring mode</legend>
          <label className="scene-authoring__mode-option">
            <input
              type="radio"
              name="scene-authoring-mode"
              aria-label="Live source"
              checked={mode === "live"}
              onChange={() => setMode("live")}
            />
            Live source
          </label>
          <label className="scene-authoring__mode-option">
            <input
              type="radio"
              name="scene-authoring-mode"
              aria-label="Authored per-frame"
              checked={mode === "authored"}
              onChange={() => setMode("authored")}
            />
            Authored per-frame
          </label>
        </fieldset>

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
          targetIds={targetIds}
          selections={selections}
          onSelect={onSelect}
          onClearSelections={clearSelections}
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
 * Source, pick target Frames, set seconds-per-cycle). 14b adds the
 * `mode === "authored"` branch: a per-frame candidate-chooser subtree
 * ({@link AuthoredControls}), rendered without touching the live branch.
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
  targetIds,
  selections,
  onSelect,
  onClearSelections,
}) {
  if (mode === "authored") {
    return (
      <AuthoredControls
        sources={sources}
        sourceRef={sourceRef}
        onSource={onSource}
        frames={frames}
        targets={targets}
        onToggleTarget={onToggleTarget}
        cycleSeconds={cycleSeconds}
        onCycleSeconds={onCycleSeconds}
        targetIds={targetIds}
        selections={selections}
        onSelect={onSelect}
        onClearSelections={onClearSelections}
      />
    );
  }
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
 * A human label for one candidate option — kind and original geometry, so the
 * operator can tell photos/videos apart. The option VALUE is the asset id.
 */
function candidateLabel(candidate) {
  const kind = candidate.kind === "video" ? "Video" : "Photo";
  return `${kind} ${candidate.original_width}×${candidate.original_height}`;
}

/**
 * The authored-mode controls (Bead 14b): pick a Source and target Frames, then
 * choose ONE asset per Frame from that Frame's candidate list. The candidates
 * come from `GET /v1/operator/sources/{ref}/candidates?frame_id=<frame>`, which
 * central HARD-FILTERS by the Frame's profile server-side — an asset ineligible
 * for a Frame's profile is never returned, so it can never be offered here. The
 * whole selection is later saved in ONE `PUT …/scenes/{id}/authored`.
 */
function AuthoredControls({
  sources,
  sourceRef,
  onSource,
  frames,
  targets,
  onToggleTarget,
  cycleSeconds,
  onCycleSeconds,
  targetIds,
  selections,
  onSelect,
  onClearSelections,
}) {
  // Candidate lists keyed by frame id, each already hard-filtered by that
  // Frame's profile on the server.
  const [byFrame, setByFrame] = useState(
    /** @type {Record<string, Array<object>>} */ ({}),
  );
  const [loadError, setLoadError] = useState(/** @type {string|null} */ (null));
  const targetKey = targetIds.join(" ");

  // A per-Frame choice belongs to one Source's catalog; changing the Source
  // invalidates every prior choice.
  useEffect(() => {
    onClearSelections();
  }, [sourceRef, onClearSelections]);

  useEffect(() => {
    if (sourceRef === "" || targetKey === "") {
      setByFrame({});
      setLoadError(null);
      return undefined;
    }
    let ignore = false;
    setLoadError(null);
    const frameIds = targetKey.split(" ");
    (async () => {
      try {
        const entries = await Promise.all(
          frameIds.map(async (frameId) => {
            // frame_id makes central drop every asset ineligible for THIS Frame's
            // profile — the profile hard-filter (design J4). Dropping it would
            // offer incompatible assets, which is exactly what the mutation probe
            // attacks.
            const result = await apiWrite(
              `/v1/operator/sources/${encodeURIComponent(sourceRef)}/candidates?frame_id=${encodeURIComponent(frameId)}`,
              { method: "GET" },
            );
            if (!result.ok) {
              throw new Error(result.error ?? String(result.status));
            }
            return [frameId, result.data?.candidates ?? []];
          }),
        );
        if (!ignore) {
          setByFrame(Object.fromEntries(entries));
        }
      } catch {
        if (!ignore) {
          setByFrame({});
          setLoadError("Could not load candidate media for these Frames.");
        }
      }
    })();
    return () => {
      ignore = true;
    };
  }, [sourceRef, targetKey]);

  return (
    <fieldset className="scene-authoring__authored" aria-label="Authored controls">
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

      <fieldset
        className="scene-authoring__choosers"
        aria-label="Per-frame media choices"
      >
        <legend>Per-frame media choices</legend>
        {targetIds.length === 0 ? (
          <p className="scene-authoring__empty">
            Choose a Source and target Frames to pick media.
          </p>
        ) : (
          targetIds.map((frameId) => {
            const candidates = byFrame[frameId] ?? [];
            return (
              <label key={frameId} className="scene-authoring__chooser">
                {`Media for frame ${frameId}`}
                <select
                  className="scene-authoring__choice"
                  aria-label={`Media for frame ${frameId}`}
                  value={selections[frameId] ?? ""}
                  onChange={(event) => onSelect(frameId, event.target.value)}
                >
                  <option value="">
                    {candidates.length === 0
                      ? "No compatible media"
                      : "Choose compatible media"}
                  </option>
                  {candidates.map((candidate) => (
                    <option key={candidate.asset_id} value={candidate.asset_id}>
                      {candidateLabel(candidate)}
                    </option>
                  ))}
                </select>
              </label>
            );
          })
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

      {loadError !== null ? (
        <p className="scene-authoring__status" role="status">
          {loadError}
        </p>
      ) : null}
    </fieldset>
  );
}

/**
 * Build the save {path, body} for the given authoring mode (the 14a/14b seam).
 * "live": the plain scene route with the Scene as the body. "authored": the
 * authored route with a {scene, source_ref, asset_ids} body — one media
 * Contribution per target Frame carrying that Frame's chosen asset ref.
 *
 * @param {"live"|"authored"} mode
 * @param {{sceneId: string, sourceRef: string, targetIds: string[], cycleSeconds: number, selections?: Record<string,string>}} draft
 * @returns {{path: string, body: object}}
 */
export function buildSave(
  mode,
  { sceneId, sourceRef, targetIds, cycleSeconds, selections = {} },
) {
  if (mode === "authored") {
    // An authored Scene: one media Contribution per target Frame, each carrying
    // the operator's chosen asset ref (never a live source_ref). The asset_ids
    // list is the de-duplicated set of chosen refs the authored route persists.
    const contributions = targetIds.map((frameId) => ({
      target: `frame:${frameId}`,
      role: frameId,
      kind: "media",
      asset_refs: [selections[frameId]],
      retain_on_expiry: true,
    }));
    const assetIds = [...new Set(targetIds.map((frameId) => selections[frameId]))];
    const scene = {
      scene_id: sceneId,
      revision: 1,
      cycle_seconds: cycleSeconds,
      loop: false,
      contributions,
    };
    return {
      path: `/v1/operator/scenes/${encodeURIComponent(sceneId)}/authored`,
      body: { scene, source_ref: sourceRef, asset_ids: assetIds },
    };
  }
  if (mode !== "live") {
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
