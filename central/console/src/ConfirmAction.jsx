import React, { useEffect, useId, useRef, useState } from "react";

import {
  ALREADY_MESSAGE,
  CHANGED_MESSAGE,
  retirePlayer,
  unbind,
  unbindSequence,
  UNKNOWN_MESSAGE,
} from "./equipmentApi.js";
import { deleteFrame } from "./framesApi.js";
import { isBound, outputLabel, outputStates, playerHandle } from "./health.js";
import { liveRunsFor } from "./join.js";
import { useMutate } from "./useMutate.js";

/**
 * One confirmation pattern (slice 2 §7) for the destructive verbs: retire,
 * unbind, unbind all, and delete frame.
 *
 * A native `<dialog>` opened with `showModal()`. Everything it shows and sends
 * is CAPTURED when it opens — the target, the generation, the bound Outputs and
 * Frames, the live Runs, the sibling Output's Frame, the handle — by one of the
 * request builders below; the live snapshot is never read at confirm time. The
 * write runs inside `useMutate()` (one Plane A refresh after it), and its result
 * is independent of that refresh.
 *
 * States: idle -> in flight -> done (the dialog closes; the owner shows a
 * role=status line and moves focus to the named successor) | refused (back to
 * idle, the reason as role=alert) | changed, already done, outcome unknown, or
 * an "Unbind all" summary (terminal: the result stays, only Close remains, and
 * nothing is ever resent).
 *
 * IN FLIGHT, Esc and Cancel are blocked. Preventing `cancel` is not enough —
 * under Chromium's close-watcher rule a repeated Esc without user activation
 * closes the dialog anyway — so the dialog also carries `closedby="none"` while
 * in flight, and a `close` that still arrives re-opens it with its state kept.
 *
 * Each surface owns ONE of these at its top level through {@link useConfirm},
 * keyed by target and never inside a list row, so a poll that regroups a
 * Player cannot unmount it.
 *
 * @typedef {"done"|"refused"|"changed"|"already"|"unknown"|"summary"} ConfirmState
 * @typedef {{frameId: string, outcome: string, label: string}} FrameResult
 * @typedef {{state: ConfirmState, message: string|null, results?: FrameResult[]}} ConfirmResult
 * @typedef {{key: string, title: string, body: React.ReactNode, confirmLabel: string,
 *            handle?: string|null, run: () => Promise<ConfirmResult>}} ConfirmRequest
 *
 * @param {{request: ConfirmRequest,
 *          onClose: (result: ConfirmResult|null) => void}} props
 *   `onClose(null)` after Cancel or Esc (focus returns to the opener);
 *   otherwise the result the dialog ended with.
 */
export function ConfirmAction({ request, onClose }) {
  const mutate = useMutate();
  const titleId = useId();
  const dialogRef = useRef(/** @type {HTMLDialogElement|null} */ (null));
  const inputRef = useRef(/** @type {HTMLInputElement|null} */ (null));
  const cancelRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const closeRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  // Read synchronously by the native event listeners.
  const phaseRef = useRef("idle");
  const resultRef = useRef(/** @type {ConfirmResult|null} */ (null));
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [phase, setPhaseState] = useState(/** @type {"idle"|"in-flight"|"terminal"|"closing"} */ ("idle"));
  const [alert, setAlert] = useState(/** @type {string|null} */ (null));
  const [terminal, setTerminal] = useState(/** @type {ConfirmResult|null} */ (null));
  const [typed, setTyped] = useState("");

  const setPhase = (next) => {
    phaseRef.current = next;
    setPhaseState(next);
  };

  useEffect(() => {
    const dialog = dialogRef.current;
    const onCancel = (event) => {
      if (phaseRef.current === "in-flight") {
        event.preventDefault();
      }
    };
    const onCloseEvent = () => {
      if (phaseRef.current === "in-flight") {
        // A close request got past the guard: re-open, keeping the state.
        dialog.showModal();
        return;
      }
      onCloseRef.current(resultRef.current);
    };
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("close", onCloseEvent);
    dialog.showModal();
    (inputRef.current ?? cancelRef.current)?.focus();
    return () => {
      dialog.removeEventListener("cancel", onCancel);
      dialog.removeEventListener("close", onCloseEvent);
    };
  }, []);

  useEffect(() => {
    dialogRef.current?.setAttribute("closedby", phase === "in-flight" ? "none" : "closerequest");
    if (phase === "terminal") {
      closeRef.current?.focus();
    } else if (phase === "closing") {
      // Closed only once the render carrying the post-write snapshot has
      // committed, so the owner's focus successor sees the fresh surface.
      dialogRef.current?.close();
    }
  }, [phase]);

  const handle = request.handle ?? null;
  const typedOk = handle === null || typed.trim().toLowerCase() === handle.toLowerCase();

  const confirm = async () => {
    if (phaseRef.current !== "idle" || !typedOk) {
      return;
    }
    setAlert(null);
    setPhase("in-flight");
    let result;
    try {
      result = await mutate(request.run);
    } catch {
      result = { state: "unknown", message: UNKNOWN_MESSAGE };
    }
    if (result.state === "refused") {
      setAlert(result.message);
      setPhase("idle");
      return;
    }
    resultRef.current = result;
    if (result.state === "done") {
      setPhase("closing");
      return;
    }
    setTerminal(result);
    setPhase("terminal");
  };

  const close = () => {
    if (phaseRef.current !== "in-flight") {
      dialogRef.current.close();
    }
  };

  return (
    <dialog ref={dialogRef} className="confirm" aria-labelledby={titleId}>
      <h2 id={titleId} className="confirm__title">
        {request.title}
      </h2>
      <div className="confirm__body">{request.body}</div>
      {terminal === null ? (
        <>
          {handle !== null && (
            <label className="confirm__typed">
              {`Type ${handle} to confirm`}
              <input
                ref={inputRef}
                type="text"
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoCapitalize="off"
                autoCorrect="off"
                autoComplete="off"
                spellCheck={false}
                disabled={phase !== "idle"}
              />
            </label>
          )}
          {alert !== null && (
            <p className="confirm__alert" role="alert">
              {alert}
            </p>
          )}
          <div className="confirm__actions">
            <button
              type="button"
              className="confirm__confirm"
              onClick={confirm}
              disabled={!typedOk || phase !== "idle"}
            >
              {request.confirmLabel}
            </button>
            <button
              ref={cancelRef}
              type="button"
              onClick={close}
              disabled={phase === "in-flight"}
            >
              Cancel
            </button>
          </div>
          {phase === "in-flight" && (
            <p className="confirm__progress" role="status">
              Sending…
            </p>
          )}
        </>
      ) : (
        <>
          <p className="confirm__outcome" role="status">
            {terminal.message}
          </p>
          {terminal.results !== undefined && (
            <ul className="confirm__results" aria-label="Result for each frame">
              {terminal.results.map((entry) => (
                <li key={entry.frameId}>{`${entry.frameId}: ${entry.label}`}</li>
              ))}
            </ul>
          )}
          <div className="confirm__actions">
            <button ref={closeRef} type="button" onClick={close}>
              Close
            </button>
          </div>
        </>
      )}
    </dialog>
  );
}

/**
 * The owner's side of the one confirmation pattern: the open request, the
 * opener, the done status line, and where focus goes when the dialog closes.
 * Every surface that owns a ConfirmAction uses this, so the close policy is
 * written once: done -> the status line states the result and `onDone` runs
 * (the owner's successor); anything else -> focus returns to the opener, or,
 * when a poll removed it, `successor` moves it on.
 *
 * `confirmation(statusClass)` renders the role=status line and the dialog; the
 * owner places it once at its top level, never inside a list row.
 *
 * @param {(() => void)|null} successor moves focus when the opener is gone
 * @param {((result: ConfirmResult, request: ConfirmRequest) => void)|null} [onDone]
 * @returns {{open: (event: {currentTarget: Element}|null, request: ConfirmRequest) => void,
 *            setStatus: (status: string|null) => void,
 *            confirmation: (statusClass: string) => React.ReactNode}}
 */
export function useConfirm(successor, onDone = null) {
  const [request, setRequest] = useState(/** @type {ConfirmRequest|null} */ (null));
  const [status, setStatus] = useState(/** @type {string|null} */ (null));
  const openerRef = useRef(/** @type {HTMLElement|null} */ (null));

  const open = (event, next) => {
    openerRef.current = event?.currentTarget ?? null;
    setStatus(null);
    setRequest(next);
  };

  const onClosed = (result) => {
    const closed = request;
    setRequest(null);
    if (result?.state === "done") {
      setStatus(result.message);
      onDone?.(result, closed);
    } else if (openerRef.current?.isConnected) {
      openerRef.current.focus();
    } else {
      successor?.();
    }
  };

  const confirmation = (statusClass) => (
    <>
      <p className={statusClass} role="status">
        {status}
      </p>
      {request !== null && <ConfirmAction key={request.key} request={request} onClose={onClosed} />}
    </>
  );

  return { open, setStatus, confirmation };
}

// --- The verbs (slice 2 §7 table): what each says, captured when it opens.

/**
 * An equipmentApi result as the dialog's result; `done` carries the status line.
 *
 * @param {import("./equipmentApi.js").EquipmentResult} result
 * @param {string} doneMessage
 * @returns {ConfirmResult}
 */
function fromEquipment(result, doneMessage) {
  switch (result.outcome) {
    case "done":
      return { state: "done", message: doneMessage };
    case "changed":
      return { state: "changed", message: CHANGED_MESSAGE };
    case "already":
      return { state: "already", message: ALREADY_MESSAGE };
    default:
      return { state: result.outcome, message: result.message };
  }
}

/** The live Runs a Frame participates in, as a list (nothing when there are none). */
function RunList({ runs, lead }) {
  if (runs.length === 0) {
    return null;
  }
  return (
    <>
      <p>{lead}</p>
      <ul className="confirm__runs">
        {runs.map((run) => (
          <li key={run.run_id}>{`${run.scene_id} (${run.phase})`}</li>
        ))}
      </ul>
    </>
  );
}

/**
 * Delete a Frame (plan and tray). Captures its binding and live Runs.
 *
 * @param {object} snapshot
 * @param {string} frameId
 * @returns {ConfirmRequest}
 */
export function deleteFrameRequest(snapshot, frameId) {
  const frame = (snapshot?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
  const runs = liveRunsFor(snapshot?.runtime, frameId);
  return {
    key: `delete:${frameId}`,
    title: `Delete frame ${frameId}?`,
    confirmLabel: "Confirm delete",
    body: (
      <>
        <p>{`Frame ${frameId}: its placement, profile and calibration are removed.`}</p>
        {isBound(frame) && (
          <p>It is bound to an output; the delete is refused until it is unbound.</p>
        )}
        <RunList runs={runs} lead="Live Runs on it — the delete is refused until they finish:" />
        <p>Cannot be undone: recreating the id starts uncommissioned.</p>
      </>
    ),
    run: async () => {
      const result = await deleteFrame(frameId);
      if (result.ok) {
        return { state: "done", message: `Frame ${frameId} deleted.` };
      }
      if (result.code === "unknown_frame") {
        return { state: "already", message: ALREADY_MESSAGE };
      }
      return { state: "refused", message: result.message };
    },
  };
}

/**
 * Unbind one Frame (Binding facet). Captures its generation, Output, live Runs
 * and the Frames on its Player's other Outputs (re-planned too).
 *
 * @param {object} snapshot
 * @param {object|null} bootFacts
 * @param {string} frameId
 * @returns {ConfirmRequest}
 */
export function unbindRequest(snapshot, bootFacts, frameId) {
  const frames = snapshot?.inventory?.frames ?? [];
  const frame = frames.find((candidate) => candidate.id === frameId);
  const { generation, player_id: playerId, output_id: outputId } = frame;
  const siblings = frames.filter(
    (candidate) => candidate.id !== frameId && candidate.player_id === playerId,
  );
  const runs = liveRunsFor(snapshot?.runtime, frameId);
  const output = `${playerHandle(snapshot, bootFacts, playerId)} · ${outputId}`;
  return {
    key: `unbind:${frameId}`,
    title: `Unbind frame ${frameId}?`,
    confirmLabel: "Confirm unbind",
    body: (
      <>
        <p>{`Frame ${frameId} stops being served by ${output}.`}</p>
        <p>Its calibration is kept but marked invalid, so it must be re-commissioned.</p>
        <RunList runs={runs} lead="Its live Runs lose this frame:" />
        {siblings.length > 0 && (
          <p>
            {`The Player's other output is re-planned too: ${siblings
              .map((sibling) => `frame ${sibling.id}`)
              .join(", ")}.`}
          </p>
        )}
        <p>Nothing is lost for good: you can bind it again.</p>
      </>
    ),
    run: async () => fromEquipment(await unbind(frameId, generation), `Frame ${frameId} unbound.`),
  };
}

/**
 * Retire a pending Player (typed handle). Captures the handle and its Outputs.
 *
 * @param {object} snapshot
 * @param {object|null} bootFacts
 * @param {string} playerId
 * @returns {ConfirmRequest}
 */
export function retireRequest(snapshot, bootFacts, playerId) {
  const handle = playerHandle(snapshot, bootFacts, playerId);
  const outputs = outputStates(snapshot, playerId);
  return {
    key: `retire:${playerId}`,
    title: `Retire player ${handle}?`,
    confirmLabel: "Confirm retire",
    handle,
    body: (
      <>
        <p className="confirm__id">{`Player ${playerId}.`}</p>
        <p>Its outputs leave the bindable set:</p>
        <ul className="confirm__outputs">
          {outputs.length === 0 ? (
            <li>No outputs reported</li>
          ) : (
            outputs.map((entry) => (
              <li key={entry.outputId}>
                {outputLabel(snapshot, bootFacts, playerId, entry.outputId)}
              </li>
            ))
          )}
        </ul>
        <p>
          No undo, even after re-imaging: the id comes from the serial. To replace a Pi,
          unbind it instead. Its netboot record still counts toward the release frontier.
        </p>
      </>
    ),
    run: async () => fromEquipment(await retirePlayer(playerId), `Player ${playerId} retired.`),
  };
}

/**
 * Unbind every Output of an in-service Player (the Equipment roster). Captures
 * each bound Frame with its generation and live Runs; the write is the
 * sequence in equipmentApi.js `unbindSequence`, and the dialog ends in a
 * terminal "K of N unbound" summary with each Frame's result.
 *
 * @param {object} snapshot
 * @param {object|null} bootFacts
 * @param {string} playerId
 * @returns {ConfirmRequest}
 */
export function unbindAllRequest(snapshot, bootFacts, playerId) {
  const handle = playerHandle(snapshot, bootFacts, playerId);
  const targets = outputStates(snapshot, playerId)
    .filter((entry) => entry.state === "bound")
    .map((entry) => {
      const frame = snapshot.inventory.frames.find((candidate) => candidate.id === entry.frameId);
      return {
        frameId: frame.id,
        generation: frame.generation,
        outputId: entry.outputId,
        runs: liveRunsFor(snapshot?.runtime, frame.id),
      };
    });
  return {
    key: `unbind-all:${playerId}`,
    title: `Unbind all outputs of player ${handle}?`,
    confirmLabel: "Confirm unbind all",
    body: (
      <>
        <p className="confirm__id">{`Player ${playerId}.`}</p>
        <p>
          Each listed Frame stops being served. Its calibration is kept but marked invalid,
          so it must be re-commissioned:
        </p>
        <ul className="confirm__frames" aria-label="Frames to unbind">
          {targets.map((target) => (
            <li key={target.frameId}>
              {`Frame ${target.frameId} (${target.outputId})`}
              {target.runs.length > 0 &&
                ` — live Runs: ${target.runs.map((run) => run.scene_id).join(", ")}`}
            </li>
          ))}
        </ul>
        <p>Nothing is lost for good: you can bind them again, or bind a replacement Pi.</p>
      </>
    ),
    run: async () => {
      const results = await unbindSequence(targets);
      const unbound = results.filter((entry) => entry.outcome === "done").length;
      return {
        state: "summary",
        message: `${unbound} of ${results.length} unbound`,
        results,
      };
    },
  };
}
