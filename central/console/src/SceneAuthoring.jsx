import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { buildSave, draftId, sceneProblems } from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { CycleInput } from "./CycleInput.jsx";
import { CHANGED_MESSAGE, UNKNOWN_MESSAGE } from "./equipmentApi.js";
import { Field, IdentityFields, ProblemSummary, useProblems } from "./Field.jsx";
import { SceneList } from "./SceneList.jsx";
import { SourcePicker } from "./SourcePicker.jsx";
import { TargetPicker } from "./TargetPicker.jsx";
import { useMutate } from "./useMutate.js";

/**
 * Scene authoring (Beads 14a/14b; pass 2 slice 3 §4–§6, §13).
 *
 * A Scene is a per-target COMPOSITION of contributions (design J4): media for
 * one or more EXPLICIT Frames on a seconds-per-cycle interval, from a live
 * Source ("live") or one hand-picked asset per Frame ("authored"). The operator
 * names the Scene; the id is derived from the name (authoring.js) and shown
 * before saving. Problems are reasons beside the fields and a summary frozen at
 * submit (Field.jsx); only the in-flight save disables the button. A successful
 * save clears the form.
 *
 * Each mode saves in ONE request, built by authoring.js `buildSave(mode, …)`:
 * "live" to `PUT /v1/operator/scenes/{scene_id}` with the Scene as the body,
 * "authored" to `PUT …/scenes/{id}/authored` (scene + source_ref + asset_ids).
 * The write goes through `apiWrite` inside `useMutate()` (primitive #7), so
 * Plane A — and the Scenes list below — refreshes exactly once after the save.
 *
 * EDIT (§13) loads a stored Scene the console can author losslessly into the
 * same form. It shows the stored id, never a derived one, and Replace goes
 * through ConfirmAction with the stored revision + 1. Central keeps no
 * precondition on the PUT (slice 3 Question 4), so the dialog reads the stored
 * revision first: if it moved since Edit was opened, nothing is sent and the
 * dialog ends "Changed since you opened this".
 *
 * @param {{snapshot: object|null}} props
 */
export function SceneAuthoring({ snapshot }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const sources = snapshot?.media?.sources ?? [];
  // Existing scenes come from the runtime definitions map, keyed by scene_id
  // (central/app.py `runtime_state` -> runtime.export_state()["scenes"]).
  const definitions = snapshot?.runtime?.definitions ?? {};
  const existingIds = useMemo(() => new Set(Object.keys(definitions)), [definitions]);

  const mutate = useMutate();
  const saveRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const editingRef = useRef(/** @type {HTMLParagraphElement|null} */ (null));

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
  // The stored Scene being edited (§13): its stored id and revision; null
  // while authoring a new Scene.
  const [editing, setEditing] = useState(
    /** @type {{sceneId: string, revision: number}|null} */ (null),
  );
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
  const candidates = useCandidates(mode === "authored" ? sourceRef : "", targetIds);

  // A per-Frame choice belongs to one Source's catalog: once a Frame's
  // candidates are read, a choice that is not among them is dropped. An edited
  // authored Scene keeps its stored items only while they are still candidates.
  useEffect(() => {
    if (!candidates.ready) {
      return;
    }
    setSelections((prev) => {
      const kept = Object.entries(prev).filter(([frameId, assetId]) =>
        (candidates.byFrame[frameId] ?? []).some((candidate) => candidate.asset_id === assetId),
      );
      return kept.length === Object.keys(prev).length ? prev : Object.fromEntries(kept);
    });
  }, [candidates.ready, candidates.byFrame]);

  const draft = { name, idOverride, mode, sourceRef, targets: targetIds, cycleSeconds, selections };
  const problems = useProblems(sceneProblems(draft, existingIds, { editing: editing !== null }));

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
  // collision with itself (§5); a replaced Scene leaves edit mode.
  const clear = () => {
    setName("");
    setIdOverride(null);
    setSourceRef("");
    setTargets(new Set());
    setCycleSeconds(30);
    setLoop(true);
    setSelections({});
    setVanished(null);
    setEditing(null);
    problems.reset();
  };

  const confirm = useConfirm(() => saveRef.current?.focus(), clear);

  // Edit (§13): the stored Scene into the form, under its stored id.
  const startEdit = (scene, stored) => {
    setEditing({ sceneId: scene.scene_id, revision: scene.revision });
    setMode(stored.mode);
    setName("");
    setIdOverride(null);
    setSourceRef(stored.sourceRef);
    setTargets(new Set(stored.targetIds));
    setCycleSeconds(stored.cycleSeconds);
    setLoop(stored.loop);
    setSelections(stored.selections);
    setVanished(null);
    confirm.setStatus(null);
    problems.reset();
    requestAnimationFrame(() => editingRef.current?.focus());
  };

  const onSave = async (event) => {
    event.preventDefault();
    if (saving || !problems.check()) {
      return;
    }
    const sceneId = editing?.sceneId ?? draftId(draft);
    const save = buildSave(mode, {
      sceneId,
      sourceRef,
      targetIds,
      cycleSeconds: Number(cycleSeconds),
      loop,
      selections,
      revision: editing === null ? 1 : editing.revision + 1,
    });
    if (editing !== null) {
      confirm.open({ currentTarget: saveRef.current }, replaceRequest(editing, save, candidates.reload));
      return;
    }
    setSaving(true);
    confirm.setStatus(null);
    try {
      // ONE request: the scene is saved in a single PUT; useMutate() then does
      // its one Plane A refresh so the Scenes list reflects the new scene.
      const result = await mutate(() => apiWrite(save.path, { method: "PUT", body: save.body }));
      if (result.ok) {
        clear();
        confirm.setStatus(`Saved Scene ${sceneId}.`);
      } else {
        confirm.setStatus(refusal("Could not save Scene", result, candidates.reload));
      }
    } catch {
      confirm.setStatus("Could not save Scene: the request did not complete.");
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
        {editing === null ? (
          <IdentityFields
            kind="Scene"
            name={name}
            idOverride={idOverride}
            onName={setName}
            onIdOverride={setIdOverride}
            problems={problems}
          />
        ) : (
          <p ref={editingRef} tabIndex={-1} className="scene-authoring__editing">
            {"Editing "}
            <code>{editing.sceneId}</code>
            {` · revision ${editing.revision}. Its id stays; Replace saves revision ${editing.revision + 1}.`}
          </p>
        )}

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
            candidates={candidates}
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

        <div className="record__actions">
          <button ref={saveRef} type="submit" className="scene-authoring__save" disabled={saving}>
            {editing === null ? "Save Scene" : "Replace Scene"}
          </button>
          {editing !== null && (
            <button type="button" onClick={clear}>
              Stop editing
            </button>
          )}
        </div>
      </form>

      {confirm.confirmation("scene-authoring__status-line")}

      <SceneList snapshot={snapshot} onEdit={startEdit} />
    </div>
  );
}

// The authored route's refusals in plain words (§13); media_repository.py
// `author_candidates_in` answers them with 409.
const SAVE_REFUSALS = {
  source_not_fresh:
    "The Source's last refresh failed; authored choices can be saved once it succeeds.",
  authored_asset_not_member: "That item is no longer in the Source; choose again.",
};

/**
 * A refused save in words: a known refusal as its sentence (and, when an item
 * left the Source, the choosers read their candidates again), otherwise the
 * served error code.
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
 * Replace a stored Scene (§13), captured when the dialog opens: the body and
 * the revision it was opened at. The stored revision is read first, so a Scene
 * that changed in Plane A since Edit ends in the terminal "changed" and nothing
 * is sent (Central keeps no precondition, Question 4: a save racing this read
 * still replaces silently).
 */
function replaceRequest({ sceneId, revision }, { path, body }, reload) {
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
      let stored;
      try {
        stored = await apiWrite("/v1/operator/runtime", { method: "GET" });
      } catch {
        stored = null;
      }
      if (!stored?.ok) {
        return {
          state: "refused",
          message: "Could not read the stored Scene, so nothing was sent. Try again.",
        };
      }
      if (stored.data?.definitions?.[sceneId]?.revision !== revision) {
        return { state: "changed", message: CHANGED_MESSAGE };
      }
      const result = await apiWrite(path, { method: "PUT", body });
      if (result.ok) {
        return { state: "done", message: `Replaced Scene ${sceneId}: now revision ${revision + 1}.` };
      }
      if (result.status >= 500) {
        return { state: "unknown", message: UNKNOWN_MESSAGE };
      }
      return { state: "refused", message: refusal("Not replaced", result, reload) };
    },
  };
}

/**
 * Each target Frame's candidates from one Source (Bead 14b), read from
 * `GET /v1/operator/sources/{ref}/candidates?frame_id=<frame>`, which central
 * HARD-FILTERS by the Frame's profile server-side — an asset ineligible for a
 * Frame's profile is never returned, so it can never be offered here. `ready`
 * once the lists for exactly this Source and these Frames are read; `reload`
 * reads them again.
 *
 * @param {string} sourceRef "" reads nothing
 * @param {string[]} targetIds
 */
function useCandidates(sourceRef, targetIds) {
  const [nonce, setNonce] = useState(0);
  const targetKey = targetIds.join(" ");
  const key = sourceRef === "" || targetKey === "" ? "" : `${nonce}\n${sourceRef}\n${targetKey}`;
  const [loaded, setLoaded] = useState(
    /** @type {{key: string, byFrame: Record<string, Array<object>>, error: string|null}} */ ({
      key: "",
      byFrame: {},
      error: null,
    }),
  );

  useEffect(() => {
    if (key === "") {
      return undefined;
    }
    let ignore = false;
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
          setLoaded({ key, byFrame: Object.fromEntries(entries), error: null });
        }
      } catch {
        if (!ignore) {
          setLoaded({ key, byFrame: {}, error: "Could not load candidate media for these Frames." });
        }
      }
    })();
    return () => {
      ignore = true;
    };
    // `key` carries sourceRef, targetKey and the reload nonce.
  }, [key]);

  const current = key !== "" && loaded.key === key;
  return {
    byFrame: current ? loaded.byFrame : EMPTY,
    loading: key !== "" && !current,
    error: current ? loaded.error : null,
    ready: current && loaded.error === null,
    reload: () => setNonce((value) => value + 1),
  };
}

const EMPTY = {};

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
 * Frame's candidate list (`useCandidates`, profile-filtered by Central). The
 * whole selection is later saved in ONE `PUT …/scenes/{id}/authored`.
 */
function MediaChoosers({ candidates, targetIds, selections, onSelect, problems }) {
  const { byFrame, error: loadError } = candidates;
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
          const list = byFrame[frameId] ?? [];
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
                    {list.length === 0
                      ? "No compatible media"
                      : "Choose compatible media"}
                  </option>
                  {list.map((candidate) => (
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
