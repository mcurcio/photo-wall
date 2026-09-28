import React, { useCallback, useMemo, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { rankedContributions } from "./join.js";
import { useMutate } from "./useMutate.js";

/**
 * The Runs region (moved out of Showrunner.jsx, pass 2 slice 3 §3).
 *
 * @param {{snapshot: object|null}} props
 */
export function RunsRegion({ snapshot }) {
  return (
    <section className="showrunner__region" role="region" aria-label="Runs">
      <h2 className="showrunner__region-title">Runs</h2>
      <RunControl snapshot={snapshot} />
    </section>
  );
}

/**
 * The Runs region body (Bead 16 — SR-runs).
 *
 * Three surfaces, all honest to the server's real shapes (design J4, §5/§6):
 *
 *  1. ACTIVATE NOW — a form that POSTs `/v1/operator/activations` with the
 *     stored `ActivationRequest` body ({scene_id, activation_id, priority}). The
 *     server answers SYNCHRONOUSLY with an `Admission` ({status, reason}); we
 *     render that outcome AT THE MOMENT, exactly as returned — admitted, queued,
 *     ignored, rejected or expired — never softened, never invented. This is the
 *     ONLY place an activation outcome is shown, because `Admission` is on no
 *     operator GET (design §5/§6: `/runtime` carries no `admissions` field).
 *
 *  2. CURRENT RUNS — the live Runs from `runtime.current.runs`, each with Finish
 *     and Cancel (`POST …/runs/{id}/finish|cancel`). These are the runs the
 *     projection actually holds right now; there is NO history list, and in
 *     particular NO "expired: missed_window" row — that reason is a server-side
 *     Admission on no GET, so surfacing it would be inventing state the operator
 *     surface cannot truthfully know (design §5/§6, R2).
 *
 *  3. WHY — for a selected Frame, its `contributions` ranked by the total
 *     precedence order (priority, root_order, admission_order) via the shared
 *     read `rankedContributions` (primitive #4, join.js) — the same ranking the
 *     Now-showing facet renders, in one place.
 *
 * Activate/finish/cancel all wrap the shared `useMutate()` hook (primitive #7)
 * so Plane A — and therefore the Runs list and the "why" panel — refresh exactly
 * once after each write.
 *
 * @param {{snapshot: object|null}} props
 */
function RunControl({ snapshot }) {
  const definitions = snapshot?.runtime?.definitions ?? {};
  const scenes = useMemo(() => Object.values(definitions), [definitions]);
  // The LIVE Runs the projection holds right now: `runtime.current.runs` carries
  // ALL runs (including completed/cancelled ones, whose phase has advanced), so
  // filter to the live phases — body/outro — the SAME "live" definition the
  // delete guard uses (runtime.py:233; RunView.phase, runtime.py:178). Only a
  // live Run is Finish/Cancel-able, and a cancelled/completed Run drops off here.
  // This is the ONLY Run list — no history, so no missed-window row can appear.
  const runs = useMemo(() => {
    const all = snapshot?.runtime?.current?.runs ?? [];
    return all.filter((run) => run.phase === "body" || run.phase === "outro");
  }, [snapshot]);
  const frames = snapshot?.inventory?.frames ?? [];

  const mutate = useMutate();

  // Plane B: the activation draft + the SYNCHRONOUS outcome of the last activate.
  const [sceneId, setSceneId] = useState("");
  const [activationId, setActivationId] = useState("");
  const [priority, setPriority] = useState(0);
  // The synchronous Admission of the last activation, shown at the moment and
  // held in component state ONLY (never read back from a GET). Null until the
  // operator activates something — so no activation outcome is shown on load.
  const [outcome, setOutcome] = useState(
    /** @type {{status: string, reason: string|null}|null} */ (null),
  );
  const [activating, setActivating] = useState(false);

  // The Frame whose "why" is shown; defaults to none until the operator picks.
  const [whyFrame, setWhyFrame] = useState("");

  const activateValid =
    !activating && sceneId !== "" && activationId.trim() !== "";

  const activate = useCallback(async () => {
    const id = activationId.trim();
    setActivating(true);
    setOutcome(null);
    try {
      const result = await mutate(() =>
        apiWrite("/v1/operator/activations", {
          method: "POST",
          body: {
            scene_id: sceneId,
            activation_id: id,
            priority: Number(priority),
          },
        }),
      );
      // Show the SYNCHRONOUS server outcome truthfully. On a 2xx the body is the
      // Admission {activation_id, status, run_id, reason}; on a non-2xx we report
      // the transport failure honestly rather than claim an activation status.
      if (result.ok && result.data && typeof result.data.status === "string") {
        setOutcome({
          status: result.data.status,
          reason: result.data.reason ?? null,
        });
      } else {
        setOutcome({
          status: "not accepted",
          reason: result.error ?? `HTTP ${result.status}`,
        });
      }
    } catch {
      setOutcome({ status: "not accepted", reason: "the request did not complete" });
    } finally {
      setActivating(false);
    }
  }, [sceneId, activationId, priority, mutate]);

  const finishRun = useCallback(
    (runId) =>
      mutate(() =>
        apiWrite(`/v1/operator/runs/${encodeURIComponent(runId)}/finish`, {
          method: "POST",
        }),
      ),
    [mutate],
  );

  const cancelRun = useCallback(
    (runId) =>
      mutate(() =>
        apiWrite(`/v1/operator/runs/${encodeURIComponent(runId)}/cancel`, {
          method: "POST",
        }),
      ),
    [mutate],
  );

  const why = whyFrame === "" ? [] : rankedContributions(snapshot?.runtime, whyFrame);

  return (
    <div className="run-control">
      <form
        className="run-control__activate"
        role="form"
        aria-label="Activate a Scene"
        onSubmit={(event) => {
          event.preventDefault();
          if (activateValid) {
            activate();
          }
        }}
      >
        <label className="run-control__field">
          Scene to activate
          <select
            className="run-control__scene"
            aria-label="Scene to activate"
            value={sceneId}
            onChange={(event) => setSceneId(event.target.value)}
          >
            <option value="">Choose a Scene</option>
            {scenes.map((scene) => (
              <option key={scene.scene_id} value={scene.scene_id}>
                {scene.scene_id}
              </option>
            ))}
          </select>
        </label>

        <label className="run-control__field">
          Activation ID
          <input
            type="text"
            className="run-control__activation-id"
            aria-label="Activation ID"
            value={activationId}
            onChange={(event) => setActivationId(event.target.value)}
          />
        </label>

        <label className="run-control__field">
          Priority
          <input
            type="number"
            className="run-control__priority"
            aria-label="Activation priority"
            value={priority}
            onChange={(event) => setPriority(event.target.value)}
          />
        </label>

        <button
          type="submit"
          className="run-control__activate-button"
          disabled={!activateValid}
        >
          Activate now
        </button>
      </form>

      {/* The SYNCHRONOUS activation outcome — shown at the moment, exactly as the
          server returned it, and only after an activation (null until then). This
          is the sole activation-outcome surface; the calendar/Runs area renders no
          history and no missed_window row (design §5/§6). */}
      {outcome !== null ? (
        <p className="run-control__outcome" role="status" aria-label="Activation outcome">
          {outcome.reason
            ? `Activation ${outcome.status}: ${outcome.reason}`
            : `Activation ${outcome.status}`}
        </p>
      ) : null}

      {/* Current live Runs (runtime.current.runs) — Finish/Cancel each. */}
      {runs.length === 0 ? (
        <p className="run-control__empty">No live Runs.</p>
      ) : (
        <ul className="run-control__runs" role="list">
          {runs.map((run) => (
            <li
              key={run.run_id}
              className="run-control__run"
              aria-label={`Run ${run.run_id}`}
            >
              <span className="run-control__run-scene">{`Scene ${run.scene_id}`}</span>
              <span className="run-control__run-phase">{`Phase ${run.phase}`}</span>
              <button
                type="button"
                className="run-control__finish"
                aria-label={`Finish run ${run.run_id}`}
                onClick={() => finishRun(run.run_id)}
              >
                Finish
              </button>
              <button
                type="button"
                className="run-control__cancel"
                aria-label={`Cancel run ${run.run_id}`}
                onClick={() => cancelRun(run.run_id)}
              >
                Cancel
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* The "why" panel: pick a Frame, see its contributions ranked by the total
          precedence order (priority, root_order, admission_order) via the shared
          primitive #4 read — deterministic, winner first. */}
      <div className="run-control__why" role="group" aria-label="Why">
        <label className="run-control__field">
          Frame for why
          <select
            className="run-control__why-frame"
            aria-label="Frame for why"
            value={whyFrame}
            onChange={(event) => setWhyFrame(event.target.value)}
          >
            <option value="">Choose a Frame</option>
            {frames.map((frame) => (
              <option key={frame.id} value={frame.id}>
                {frame.id}
              </option>
            ))}
          </select>
        </label>
        {whyFrame === "" ? null : why.length === 0 ? (
          <p className="run-control__empty">No contributions target this frame.</p>
        ) : (
          <ol className="run-control__why-list" aria-label="Contribution precedence">
            {why.map((intent, index) => (
              <li
                key={`${intent.run_id}:${index}`}
                className="run-control__why-row"
              >
                {`${intent.scene_id} — priority ${intent.priority}, ` +
                  `root order ${intent.root_order}, admission ${intent.admission_order}`}
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
