import React, { useCallback, useEffect, useMemo, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { draftId, sceneProblems } from "./authoring.js";
import { CycleInput } from "./CycleInput.jsx";
import { Field, IdentityFields, ProblemSummary, useProblems } from "./Field.jsx";
import { toTarget } from "./join.js";
import { cycleWording } from "./showState.js";
import { SourcePicker } from "./SourcePicker.jsx";
import { TargetPicker } from "./TargetPicker.jsx";
import { useMutate } from "./useMutate.js";

/**
 * Scene authoring (Beads 14a/14b; pass 2 slice 3 §4–§6).
 *
 * A Scene is a per-target COMPOSITION of contributions (design J4): media for
 * one or more EXPLICIT Frames on a seconds-per-cycle interval, from a live
 * Source ("live") or one hand-picked asset per Frame ("authored"). The operator
 * names the Scene; the id is derived from the name (authoring.js) and shown
 * before saving. Problems are reasons beside the fields and a summary frozen at
 * submit (Field.jsx); only the in-flight save disables the button. A successful
 * save clears the form.
 *
 * Each mode saves in ONE request, built by `buildSave(mode, …)`: "live" to
 * `PUT /v1/operator/scenes/{scene_id}` with the Scene as the body, "authored" to
 * `PUT …/scenes/{id}/authored` (scene + source_ref + asset_ids). The write goes
 * through `apiWrite` inside `useMutate()` (primitive #7), so Plane A — and the
 * Scenes list below — refreshes exactly once after the save.
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
  const existingIds = useMemo(() => new Set(Object.keys(definitions)), [definitions]);

  const mutate = useMutate();

  // Plane B: component-local authoring draft. `mode` is the 14a/14b split seam;
  // both modes exist, so a user-facing toggle (live <-> authored) selects
  // between a live-source Scene and a per-Frame authored Scene.
  const [mode, setMode] = useState("live");
  const [name, setName] = useState("");
  const [idOverride, setIdOverride] = useState(/** @type {string|null} */ (null));
  const [sourceRef, setSourceRef] = useState("");
  const [targets, setTargets] = useState(/** @type {Set<string>} */ (new Set()));
  const [cycleSeconds, setCycleSeconds] = useState(/** @type {string|number} */ (30));
  // New Scenes keep playing until their Program ends (slice 3 Question 1).
  const [loop, setLoop] = useState(true);
  // Authored mode only: the chosen asset per target Frame, keyed by frame id.
  const [selections, setSelections] = useState(
    /** @type {Record<string, string>} */ ({}),
  );
  const [status, setStatus] = useState(/** @type {string|null} */ (null));
  const [saving, setSaving] = useState(false);
  // A targeted frame that a poll no longer lists is dropped and announced (§6).
  const [vanished, setVanished] = useState(/** @type {string|null} */ (null));

  const frameKey = frames.map((frame) => frame.id).join(" ");
  useEffect(() => {
    const listed = new Set(frameKey === "" ? [] : frameKey.split(" "));
    const gone = [...targets].filter((frameId) => !listed.has(frameId));
    if (gone.length === 0) {
      return;
    }
    setTargets((prev) => new Set([...prev].filter((frameId) => listed.has(frameId))));
    setSelections((prev) =>
      Object.fromEntries(Object.entries(prev).filter(([frameId]) => listed.has(frameId))),
    );
    setVanished(
      `${gone.join(", ")} ${gone.length === 1 ? "was" : "were"} deleted and removed from this Scene.`,
    );
  }, [frameKey, targets]);

  const targetIds = useMemo(() => [...targets], [targets]);
  const draft = { name, idOverride, mode, sourceRef, targets: targetIds, cycleSeconds, selections };
  const problems = useProblems(sceneProblems(draft, existingIds));

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

  // A successful save clears the form, so the saved Scene never reads as a
  // collision with itself (§5).
  const clear = () => {
    setName("");
    setIdOverride(null);
    setSourceRef("");
    setTargets(new Set());
    setCycleSeconds(30);
    setLoop(true);
    setSelections({});
    setVanished(null);
    problems.reset();
  };

  const onSave = async (event) => {
    event.preventDefault();
    if (saving || !problems.check()) {
      return;
    }
    const sceneId = draftId(draft);
    const { path, body } = buildSave(mode, {
      sceneId,
      sourceRef,
      targetIds,
      cycleSeconds: Number(cycleSeconds),
      loop,
      selections,
    });
    setSaving(true);
    setStatus(null);
    try {
      // ONE request: the scene is saved in a single PUT; useMutate() then does
      // its one Plane A refresh so the Scenes list reflects the new scene.
      const result = await mutate(() => apiWrite(path, { method: "PUT", body }));
      if (result.ok) {
        clear();
        setStatus(`Saved Scene ${sceneId}.`);
      } else {
        setStatus(`Could not save Scene: ${result.error ?? result.status}.`);
      }
    } catch {
      setStatus("Could not save Scene: the request did not complete.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="scene-authoring">
      <form
        className="scene-authoring__form"
        role="form"
        aria-label="Author a Scene"
        noValidate
        onSubmit={onSave}
      >
        <ProblemSummary summary={problems.summary} label="Scene problems" />
        <IdentityFields
          kind="Scene"
          name={name}
          idOverride={idOverride}
          onName={setName}
          onIdOverride={setIdOverride}
          problems={problems}
        />

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

        <SourcePicker
          id={problems.idFor("source")}
          reason={problems.reasonFor("source")}
          sources={sources}
          value={sourceRef}
          onChange={(next) => {
            setSourceRef(next);
            // A per-Frame choice belongs to one Source's catalog.
            setSelections({});
            problems.touch("source");
          }}
        />

        <TargetPicker
          id={problems.idFor("targets")}
          reason={problems.reasonFor("targets")}
          snapshot={snapshot}
          targets={targets}
          onToggle={(frameId) => {
            toggleTarget(frameId);
            problems.touch("targets");
          }}
        />
        {vanished !== null && (
          <p className="scene-authoring__vanished" role="status">
            {vanished}
          </p>
        )}

        {mode === "authored" && (
          <MediaChoosers
            sourceRef={sourceRef}
            targetIds={targetIds}
            selections={selections}
            onSelect={onSelect}
            problems={problems}
          />
        )}

        <CycleInput
          problems={problems}
          seconds={cycleSeconds}
          onSeconds={setCycleSeconds}
          loop={loop}
          onLoop={setLoop}
        />

        <button type="submit" className="scene-authoring__save" disabled={saving}>
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
              <span className="scene-authoring__scene-id">{scene.scene_id}</span>
              {cycleWording(scene) !== null && (
                <span className="scene-authoring__scene-cycle">{` · ${cycleWording(scene)}`}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
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
 * The authored-mode choosers (Bead 14b): ONE asset per target Frame from that
 * Frame's candidate list. The candidates come from
 * `GET /v1/operator/sources/{ref}/candidates?frame_id=<frame>`, which central
 * HARD-FILTERS by the Frame's profile server-side — an asset ineligible for a
 * Frame's profile is never returned, so it can never be offered here. The whole
 * selection is later saved in ONE `PUT …/scenes/{id}/authored`.
 */
function MediaChoosers({ sourceRef, targetIds, selections, onSelect, problems }) {
  // Candidate lists keyed by frame id, each already hard-filtered by that
  // Frame's profile on the server.
  const [byFrame, setByFrame] = useState(
    /** @type {Record<string, Array<object>>} */ ({}),
  );
  const [loadError, setLoadError] = useState(/** @type {string|null} */ (null));
  const targetKey = targetIds.join(" ");

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
                  className="scene-authoring__choice"
                  value={selections[frameId] ?? ""}
                  onChange={(event) => {
                    onSelect(frameId, event.target.value);
                    problems.touch(field);
                  }}
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
              )}
            </Field>
          );
        })
      )}
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
 * @param {{sceneId: string, sourceRef: string, targetIds: string[], cycleSeconds: number, loop: boolean, selections?: Record<string,string>}} draft
 * @returns {{path: string, body: object}}
 */
export function buildSave(
  mode,
  { sceneId, sourceRef, targetIds, cycleSeconds, loop, selections = {} },
) {
  if (mode === "authored") {
    // An authored Scene: one media Contribution per target Frame, each carrying
    // the operator's chosen asset ref (never a live source_ref). The asset_ids
    // list is the de-duplicated set of chosen refs the authored route persists.
    const contributions = targetIds.map((frameId) => ({
      target: toTarget(frameId),
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
      loop,
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
    target: toTarget(frameId),
    role: frameId,
    kind: "media",
    source_refs: [sourceRef],
    retain_on_expiry: true,
  }));
  const scene = {
    scene_id: sceneId,
    revision: 1,
    cycle_seconds: cycleSeconds,
    loop,
    contributions,
  };
  return {
    path: `/v1/operator/scenes/${encodeURIComponent(sceneId)}`,
    body: scene,
  };
}
