import React, { useEffect, useId, useRef, useState } from "react";

import { ALREADY_MESSAGE, retirePlayer, unbind, unbindSequence } from "./equipmentApi.js";
import { deleteFrame } from "./framesApi.js";
import { isBound, outputLabel, outputStates, playerHandle } from "./health.js";
import { liveRunsFor } from "./join.js";
import { usePageHidden } from "./pageVisibility.js";
import { CHANGED_MESSAGE, UNKNOWN_MESSAGE } from "./sendOutcome.js";
import { frameStoredReferences } from "./sceneTargets.js";
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
 * Its buttons are disabled then, so focus moves to the dialog itself (tabIndex -1)
 * rather than falling out of it; a refusal, or a sign-in overlay that opened over
 * it (SignInScreen.jsx), then finds focus still inside.
 *
 * WHILE ITS PAGE IS HIDDEN (pageVisibility.js; a Show page left by a link or browser
 * Back) the dialog is put away, since `hidden` on the page does not hide a modal
 * `<dialog>` from the top layer and the page shown would stay inert. An idle dialog is
 * cancelled, as Esc would; one in flight or showing its outcome is closed without ending
 * and shown again, focused, when its page is; a write that finishes "done" meanwhile
 * ends it as usual. This owner covers every surface that uses {@link useConfirm}.
 *
 * Each surface owns ONE of these at its top level through {@link useConfirm},
 * keyed by target and never inside a list row, so a poll that regroups a
 * Player cannot unmount it.
 *
 * @typedef {"done"|"refused"|"changed"|"already"|"unknown"|"summary"} ConfirmState
 * @typedef {{frameId: string, outcome: string, label: string}} FrameResult
 * @typedef {{state: ConfirmState, message: string|null, results?: FrameResult[]}} ConfirmResult
 * @typedef {{key: string, title: string, body: React.ReactNode, confirmLabel: string,
 *            handle?: string|null, progress?: string,
 *            run: () => Promise<ConfirmResult>}} ConfirmRequest
 *   `progress` replaces "Sending…" while in flight, for a write whose work happens inside its
 *   request (a release publish says what Central is downloading).
 *
 * @param {{request: ConfirmRequest,
 *          onClose: (result: ConfirmResult|null) => void}} props
 *   `onClose(null)` after Cancel or Esc (focus returns to the opener);
 *   otherwise the result the dialog ended with.
 */
export function ConfirmAction({ request, onClose }) {
  const mutate = useMutate();
  const pageHidden = usePageHidden();
  const pageHiddenRef = useRef(pageHidden);
  pageHiddenRef.current = pageHidden;
  // Closed, not ended, while its page is hidden.
  const suspendedRef = useRef(false);
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
      if (suspendedRef.current) {
        return; // put away while its page is hidden
      }
      if (phaseRef.current === "in-flight") {
        // A close request got past the guard: re-open, keeping the state.
        if (!dialog.open) {
          dialog.showModal();
        }
        return;
      }
      onCloseRef.current(resultRef.current);
    };
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("close", onCloseEvent);
    if (pageHiddenRef.current) {
      suspendedRef.current = true;
    } else {
      dialog.showModal();
      (inputRef.current ?? cancelRef.current)?.focus();
    }
    return () => {
      dialog.removeEventListener("cancel", onCancel);
      dialog.removeEventListener("close", onCloseEvent);
    };
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (pageHidden && dialog.open) {
      if (phaseRef.current === "idle") {
        dialog.close(); // cancelled, as Esc would
      } else {
        suspendedRef.current = true;
        dialog.close();
      }
    } else if (!pageHidden && suspendedRef.current) {
      suspendedRef.current = false;
      if (!dialog.open) {
        dialog.showModal();
      }
      (phaseRef.current === "terminal" ? closeRef.current : dialog)?.focus();
    }
  }, [pageHidden]);

  useEffect(() => {
    const dialog = dialogRef.current;
    dialog.setAttribute("closedby", phase === "in-flight" ? "none" : "closerequest");
    if (phase === "terminal") {
      closeRef.current?.focus();
    } else if (phase === "closing") {
      // Closed only once the render carrying the post-write snapshot has
      // committed, so the owner's focus successor sees the fresh surface.
      if (suspendedRef.current) {
        // Put away while its page is hidden: nothing to close, so it ends here.
        suspendedRef.current = false;
        onCloseRef.current(resultRef.current);
      } else {
        dialog.close();
      }
    }
  }, [phase]);

  const handle = request.handle ?? null;
  const typedOk = handle === null || typed.trim().toLowerCase() === handle.toLowerCase();

  const confirm = async () => {
    if (phaseRef.current !== "idle" || !typedOk) {
      return;
    }
    setAlert(null);
    dialogRef.current?.focus();
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
    <dialog ref={dialogRef} className="confirm" aria-labelledby={titleId} tabIndex={-1}>
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
              {request.progress ?? "Sending…"}
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
 * when a poll removed or disabled it, `successor` moves it on.
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
    } else if (openerRef.current?.isConnected && !openerRef.current.disabled) {
      openerRef.current.focus();
    } else {
      successor?.();
    }
  };

  const confirmation = (statusClass) => (
    <>
      {status !== null && <p className={statusClass} role="status">{status}</p>}
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

function frameReferenceRefusal(result) {
  const details = [
    ["saved Scenes", result.scene_ids],
    ["Programs", result.program_ids],
    ["queued activations", result.queued_activation_ids],
  ].filter(([, ids]) => ids.length > 0)
    .map(([label, ids]) => `${label}: ${ids.join(", ")}`);
  const reported = details.length > 0 ? ` Central reports ${details.join("; ")}.` : "";
  const actions = [];
  if (result.scene_ids.length > 0) actions.push("Edit or remove the saved Scenes.");
  if (result.program_ids.length > 0) actions.push("Edit or remove the Programs.");
  if (result.queued_activation_ids.length > 0) {
    actions.push("Review the queued activations and retry after Central resolves them.");
  }
  const guidance = actions.length > 0
    ? ` Press Refresh. ${actions.join(" ")}`
    : " Press Refresh to see the current references before retrying.";
  return `This Frame is still referenced.${reported}${guidance}`;
}

/**
 * Delete a Frame (plan and tray). Captures its binding, live Runs, and stored references.
 *
 * @param {object} snapshot
 * @param {string} frameId
 * @returns {ConfirmRequest}
 */
export function deleteFrameRequest(snapshot, frameId) {
  const frame = (snapshot?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
  const runs = liveRunsFor(snapshot?.runtime, frameId);
  const { scenes, programs } = frameStoredReferences(snapshot, frameId);
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
        {scenes.length > 0 ? (
          <>
            <p>Saved Scenes targeting this Frame must be edited or removed before deletion:</p>
            <ul className="confirm__runs">
              {scenes.map((sceneId) => <li key={sceneId}>{sceneId}</li>)}
            </ul>
          </>
        ) : (
          <p>No saved Scenes reference this Frame in this snapshot.</p>
        )}
        {programs.length > 0 && (
          <>
            <p>Upcoming Programs using those Scenes also need review:</p>
            <ul className="confirm__runs">
              {programs.map((programId) => <li key={programId}>{programId}</li>)}
            </ul>
          </>
        )}
        <p>Central checks references again when you confirm.</p>
        <p>Cannot be undone: recreating the id starts uncalibrated.</p>
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
      if (result.code === "frame_referenced") {
        return {
          state: "refused",
          message: frameReferenceRefusal(result),
        };
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
    title: `Disconnect Frame ${frameId} from its Pi?`,
    confirmLabel: "Disconnect",
    body: (
      <>
        <p>{`Frame ${frameId} stops being fed by Pi ${output}.`}</p>
        <p>Its position is kept, but it must be set again once the Frame is connected.</p>
        <RunList runs={runs} lead="These Scenes stop showing on it:" />
        {siblings.length > 0 && (
          <p>
            {`The Pi's other Frames get their content again too: ${siblings
              .map((sibling) => `Frame ${sibling.id}`)
              .join(", ")}.`}
          </p>
        )}
        <p>Nothing is lost for good: you can connect it again.</p>
      </>
    ),
    run: async () => fromEquipment(await unbind(frameId, generation), `Frame ${frameId} is disconnected from its Pi.`),
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
          unbind it instead.
        </p>
      </>
    ),
    run: async () => fromEquipment(await retirePlayer(playerId), `Player ${playerId} retired.`),
  };
}

/**
 * Unbind every Output of a bound Player (its Player page). Captures
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
    title: `Unbind each Output of Player ${handle}?`,
    confirmLabel: "Confirm unbind all",
    body: (
      <>
        <p className="confirm__id">{`Player ${playerId}.`}</p>
        <p>
          Central unbinds them one at a time. One that changed since you opened this is
          skipped; if an outcome is unknown, the rest are not attempted.
        </p>
        <p>
          Each listed Frame stops being served. Its calibration is kept but marked invalid,
          so it must be calibrated again:
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
