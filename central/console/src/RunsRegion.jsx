import React, { useCallback, useId, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { UNKNOWN_MESSAGE } from "./equipmentApi.js";
import { frameHealth } from "./health.js";
import { explainPrecedence } from "./join.js";
import { PrecedenceExplanation } from "./NowShowingFacet.jsx";
import { runRows } from "./showState.js";
import { useMutate } from "./useMutate.js";

/**
 * The Runs region (Bead 16; pass 2 slice 3 §9–§11), the "Now" column.
 *
 *  1. ACTIVATE NOW — POST `/v1/operator/activations`; the server answers
 *     SYNCHRONOUSLY with an `Admission` ({status, reason}), shown at the moment
 *     exactly as returned.
 *  2. RUNS — the served Runs (runtime.current.runs, live plus those ended in the
 *     last day) as rows from showState.js `runRows`: live roots with their
 *     children nested, then a closed "Recently ended" list. Finish asks for a
 *     natural end; Cancel stops now and goes through ConfirmAction.
 *  3. WHY — Central's plan for a chosen frame (join.js `explainPrecedence`),
 *     the same explanation the Now-showing facet renders.
 *
 * Every write wraps the shared `useMutate()` hook (primitive #7), so Plane A
 * refreshes exactly once after it.
 *
 * @param {{snapshot: object|null}} props
 */
export function RunsRegion({ snapshot }) {
  const regionRef = useRef(/** @type {HTMLElement|null} */ (null));
  // After a cancel, or when its opener is gone, the Runs region takes focus.
  const focusRegion = () => regionRef.current?.focus();
  const { open, confirmation } = useConfirm(focusRegion, focusRegion);
  const mutate = useMutate();
  const rows = runRows(snapshot);
  const ended = rows.completed.length + rows.cancelled.length;

  const finishRun = useCallback(
    (runId) =>
      mutate(() =>
        apiWrite(`/v1/operator/runs/${encodeURIComponent(runId)}/finish`, {
          method: "POST",
        }),
      ),
    [mutate],
  );

  const actions = {
    onFinish: finishRun,
    onCancel: (event, row) => open(event, cancelRequest(row)),
  };

  return (
    <section
      ref={regionRef}
      tabIndex={-1}
      className="showrunner__region"
      role="region"
      aria-label="Runs"
    >
      <h2 className="showrunner__region-title">Runs</h2>
      <ActivateForm snapshot={snapshot} />

      {rows.live.length === 0 ? (
        <p className="run-control__empty">No live Runs.</p>
      ) : (
        <ul className="run-control__runs" role="list">
          {rows.live.map((row) => (
            <RunRow key={row.run.run_id} row={row} snapshot={snapshot} actions={actions} />
          ))}
        </ul>
      )}

      {ended > 0 && (
        <details className="run-control__ended">
          <summary>{`Recently ended (${ended})`}</summary>
          <p className="run-control__note">Runs that ended in the last day.</p>
          <EndedRuns title="Completed" rows={rows.completed} snapshot={snapshot} />
          <EndedRuns title="Cancelled" rows={rows.cancelled} snapshot={snapshot} />
        </details>
      )}

      <WhyPanel snapshot={snapshot} />
      {confirmation("run-control__status-line")}
    </section>
  );
}

/**
 * Cancel one Run (§9): captured when the dialog opens. Cancel stops now,
 * skipping the outro, and its children stop too (central/runtime.py `_cancel`).
 */
function cancelRequest(row) {
  const { run } = row;
  const where = row.frames.length > 0 ? row.frames.join(", ") : "its targets";
  return {
    key: `cancel:${run.run_id}`,
    title: `Cancel the Run of ${run.scene_id}?`,
    confirmLabel: "Confirm cancel",
    body: <p>{`Stops now on ${where}, skipping its outro; its child Scenes stop too.`}</p>,
    run: async () => {
      const result = await apiWrite(`/v1/operator/runs/${encodeURIComponent(run.run_id)}/cancel`, {
        method: "POST",
      });
      if (result.ok) {
        return { state: "done", message: `Run of ${run.scene_id} cancelled.` };
      }
      if (result.status >= 500) {
        return { state: "unknown", message: UNKNOWN_MESSAGE };
      }
      return { state: "refused", message: `Not cancelled: ${result.error ?? result.status}.` };
    },
  };
}

/**
 * One Run as a record (§9): `Scene X` and its revision, origin, age, state,
 * one-cycle wording, priority, protection and its frames with their health.
 * Roots carry Finish and Cancel; their children are nested beneath.
 */
function RunRow({ row, snapshot, actions = null }) {
  const statusId = useId();
  const { run } = row;
  return (
    <li className="run-control__run" aria-label={`Run ${run.run_id}`}>
      <p className="run-control__run-title">
        <span className="run-control__run-scene">{`Scene ${run.scene_id}`}</span>
        {` · revision ${run.scene_revision}`}
      </p>
      <dl className="record">
        <dt>Origin</dt>
        <dd>{row.origin}</dd>
        <dt>Started</dt>
        <dd>{row.started}</dd>
        <dt>State</dt>
        <dd id={statusId}>{row.status}</dd>
        {row.cycle !== null && (
          <>
            <dt>Cycle</dt>
            <dd>{row.cycle}</dd>
          </>
        )}
        <dt>Priority</dt>
        <dd>{`priority ${run.priority}`}</dd>
        {row.protects.length > 0 && (
          <>
            <dt>Protection</dt>
            <dd>{`protects ${row.protects.join(", ")}`}</dd>
          </>
        )}
        <dt>Frames</dt>
        <dd>
          <span className="run-control__frames">
            {row.frames.map((frameId) => {
              const health = frameHealth(snapshot, frameId);
              return (
                <span key={frameId} className={`run-control__frame health--${health?.severity ?? "todo"}`}>
                  {health === null ? `${frameId}: not in the inventory` : `${frameId}: ${health.tileLabel}`}
                </span>
              );
            })}
          </span>
        </dd>
      </dl>
      {actions !== null && (
        <div className="record__actions">
          <button
            type="button"
            className="run-control__finish"
            aria-label={`Finish run ${run.run_id}`}
            aria-describedby={row.finishing ? statusId : undefined}
            disabled={row.finishing}
            onClick={() => actions.onFinish(run.run_id)}
          >
            Finish
          </button>
          <button
            type="button"
            className="run-control__cancel"
            aria-label={`Cancel run ${run.run_id}`}
            onClick={(event) => actions.onCancel(event, row)}
          >
            Cancel
          </button>
        </div>
      )}
      {row.children.length > 0 && (
        <ul className="run-control__children" aria-label="Child Scenes">
          {row.children.map((child) => (
            <RunRow key={child.run.run_id} row={child} snapshot={snapshot} />
          ))}
        </ul>
      )}
    </li>
  );
}

function EndedRuns({ title, rows, snapshot }) {
  if (rows.length === 0) {
    return null;
  }
  return (
    <>
      <h3 className="run-control__ended-title">{title}</h3>
      <ul className="run-control__runs" aria-label={`${title} Runs`}>
        {rows.map((row) => (
          <RunRow key={row.run.run_id} row={row} snapshot={snapshot} />
        ))}
      </ul>
    </>
  );
}

/** Central's plan for a chosen frame (§10). */
function WhyPanel({ snapshot }) {
  const frames = snapshot?.inventory?.frames ?? [];
  // The Frame whose "why" is shown; none until the operator picks.
  const [whyFrame, setWhyFrame] = useState("");
  return (
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
      {whyFrame !== "" && (
        <PrecedenceExplanation
          explanation={explainPrecedence(snapshot?.runtime, whyFrame)}
          listLabel="Contribution precedence"
          listClass="run-control__why-list"
          emptyClass="run-control__empty"
        />
      )}
    </div>
  );
}

/**
 * Activate a Scene now: POST `/v1/operator/activations`, and show the
 * synchronous Admission at the moment, exactly as returned.
 */
function ActivateForm({ snapshot }) {
  const definitions = snapshot?.runtime?.definitions ?? {};
  const scenes = useMemo(() => Object.values(definitions), [definitions]);
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

  return (
    <>
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
    </>
  );
}
