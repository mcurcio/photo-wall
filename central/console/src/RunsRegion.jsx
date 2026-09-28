import React, { useCallback, useId, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { activationProblems, newActivationKey } from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { UNKNOWN_MESSAGE } from "./equipmentApi.js";
import { Field, ProblemSummary, useProblems } from "./Field.jsx";
import { frameHealth } from "./health.js";
import { explainPrecedence } from "./join.js";
import { PrecedenceExplanation } from "./NowShowingFacet.jsx";
import { ScenePicker } from "./ScenePicker.jsx";
import { frameOf, protectorOf, runRows } from "./showState.js";
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
 * Activate a Scene now (§11): POST `/v1/operator/activations` and state the
 * synchronous Admission at the moment, from served facts only. The activation
 * id is never shown: it is minted when the draft changes or after a definite
 * outcome, and REUSED on a retry after "outcome unknown" (a 5xx, a timeout or a
 * thrown request — the write may have committed), so a retry cannot start the
 * Scene twice. A 4xx is a definite refusal.
 */
function ActivateForm({ snapshot }) {
  const definitions = snapshot?.runtime?.definitions ?? {};
  const mutate = useMutate();
  const hintId = useId();
  const repeatName = useId();

  // Plane B: the activation draft and its key.
  const [sceneId, setSceneId] = useState("");
  const [priority, setPriority] = useState(/** @type {string|number} */ (0));
  const [repeat, setRepeat] = useState(/** @type {"ignore"|"restart"} */ ("ignore"));
  const [key, setKey] = useState(newActivationKey);
  // The last outcome, shown at the moment and held here only; null on load.
  const [outcome, setOutcome] = useState(/** @type {string|null} */ (null));
  const [activating, setActivating] = useState(false);
  const problems = useProblems(activationProblems({ sceneId, priority }));

  // Any change to the draft is a new activation.
  const edit = (setter, field) => (value) => {
    setter(value);
    setKey(newActivationKey());
    if (field !== null) {
      problems.touch(field);
    }
  };

  const activate = async () => {
    const asked = { sceneId, priority: Number(priority) };
    setActivating(true);
    setOutcome(null);
    let result = null;
    try {
      result = await mutate(() =>
        apiWrite("/v1/operator/activations", {
          method: "POST",
          body: { scene_id: sceneId, activation_id: key, priority: asked.priority, repeat },
        }),
      );
    } catch {
      result = null;
    }
    setActivating(false);
    if (result === null || result.status >= 500) {
      setOutcome("Outcome unknown. Try again; it will not start twice.");
      return;
    }
    setKey(newActivationKey());
    setOutcome(
      result.ok
        ? admissionSentence(snapshot, asked, result.data)
        : `Not started: ${result.error ?? `HTTP ${result.status}`}.`,
    );
  };

  return (
    <>
      <form
        className="run-control__activate"
        role="form"
        aria-label="Activate a Scene"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (!activating && problems.check()) {
            activate();
          }
        }}
      >
        <ProblemSummary summary={problems.summary} label="Activation problems" />
        <ScenePicker
          id={problems.idFor("scene")}
          label="Scene to activate"
          reason={problems.reasonFor("scene")}
          definitions={definitions}
          value={sceneId}
          onChange={edit(setSceneId, "scene")}
        />
        <Field
          id={problems.idFor("priority")}
          label="Activation priority"
          reason={problems.reasonFor("priority")}
        >
          {(props) => (
            <input
              {...props}
              type="number"
              step="1"
              value={priority}
              onChange={(event) => edit(setPriority, "priority")(event.target.value)}
            />
          )}
        </Field>
        <fieldset className="run-control__repeat" aria-label="If it is already running">
          <legend>If it is already running</legend>
          <label className="run-control__repeat-option">
            <input
              type="radio"
              name={repeatName}
              checked={repeat === "ignore"}
              onChange={() => edit(setRepeat, null)("ignore")}
            />
            Leave it running
          </label>
          <label className="run-control__repeat-option">
            <input
              type="radio"
              name={repeatName}
              aria-describedby={hintId}
              checked={repeat === "restart"}
              onChange={() => edit(setRepeat, null)("restart")}
            />
            Restart it
          </label>
          {repeat === "restart" && (
            <p id={hintId} className="field__hint">
              Ends the current Run and starts a new one now. A restarted Run has no Program
              end; it plays until finished.
            </p>
          )}
        </fieldset>
        <button type="submit" className="run-control__activate-button" disabled={activating}>
          Activate now
        </button>
      </form>

      {/* The SYNCHRONOUS activation outcome — shown at the moment, only after
          an activation (null until then). */}
      {outcome !== null && (
        <p className="run-control__outcome" role="status" aria-label="Activation outcome">
          {outcome}
        </p>
      )}
    </>
  );
}

/**
 * An Admission in words (§11), from served facts only: a refusal names the
 * protecting Run only when the snapshot serves it live now.
 *
 * @param {object|null} snapshot the snapshot the operator activated from
 * @param {{sceneId: string, priority: number}} asked
 * @param {{status: string, reason: string|null}} admission
 * @returns {string}
 */
function admissionSentence(snapshot, asked, admission) {
  const scene = snapshot?.runtime?.definitions?.[asked.sceneId];
  const now = snapshot?.runtime?.current?.now;
  switch (admission?.status) {
    case "admitted":
      return `Started: Central admitted a Run of ${asked.sceneId}.`;
    case "ignored":
      return `Not started: ${asked.sceneId} is already running, left as is.`;
    case "queued":
      return `Queued: ${asked.sceneId} starts when its running Run ends.`;
    case "expired":
      return "Not started: it expired before it could start.";
    case "rejected":
      break;
    default:
      return "Outcome unknown. Try again; it will not start twice.";
  }
  if (admission.reason === "protected_frames") {
    const { run, frames } = protectorOf(snapshot, scene, asked.priority, admission.reason, now);
    const list = frames.map(frameOf).join(", ");
    return run !== null
      ? `Not started: ${list} is protected by the Run of ${run.scene_id}.`
      : `Not started: ${list} is protected by another Run.`;
  }
  if (admission.reason === "protection_not_visible") {
    const { run, frames } = protectorOf(snapshot, scene, asked.priority, admission.reason, now);
    const list = frames.map(frameOf).join(", ");
    return run !== null
      ? `Not started: this Scene protects ${list}, but ${run.scene_id}'s Run ` +
          `(priority ${run.priority}) covers it; use priority at least ${run.priority}.`
      : `Not started: this Scene protects ${list}, but a higher-priority Run covers it.`;
  }
  if (admission.reason === "queue_full") {
    return "Not started: 16 activations are already waiting.";
  }
  return `Not started: ${admission.reason ?? "refused"}.`;
}
