import React, { useEffect, useId, useRef, useState } from "react";

import { FactLine } from "./FactLine.jsx";
import { clock, fact } from "./facts.js";
import {
  appOperationState,
  heldReboot,
  rebootCommandState,
  rebootOffer,
  rebootRefusal,
  rebootRequest,
  rebootTarget,
  sendReboot,
} from "./fleetCommands.js";
import { NODE_OFF, nodeUnknown } from "./nodeRead.js";
const tail = (id) => `…${String(id).slice(-6)}`;

/** One command's named state and its evidence (fleetCommands.js `CommandState`). */
function StateEntry({ state, children }) {
  return (
    <li className="player__command">
      <p className="player__state">{state.label}</p>
      <FactLine label="Evidence" fact={state.fact} />
      {state.prior != null && <FactLine label="Evidence" fact={state.prior} />}
      {children}
    </li>
  );
}

/**
 * The reboot dialog (§10, §11 "Reboot dialog home"): the fleet module's own, so the
 * shared ConfirmAction is not widened. It shows and sends ONE frozen request
 * (fleetCommands.js `rebootRequest`); "Retry the same request" re-sends that body
 * unchanged. Esc and Cancel are blocked while a send is in flight. Send is disabled by
 * `rebootRefusal` on the read on screen; the send itself is judged again by `sendReboot` on
 * the hook's newest read (§10), so the dialog never decides which read counts.
 *
 * @param {{deviceId: string, name: string, request: import("./fleetCommands.js").FrozenRebootRequest,
 *          retry: boolean, node: object,
 *          onSent: (result: import("./fleetCommands.js").RebootResult) => void,
 *          onClose: (result: import("./fleetCommands.js").RebootResult|null) => void}} props
 */
function RebootDialog({ deviceId, name, request, retry, node, onSent, onClose }) {
  const titleId = useId();
  const dialogRef = useRef(/** @type {HTMLDialogElement|null} */ (null));
  const cancelRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const flying = useRef(false);
  const resultRef = useRef(/** @type {import("./fleetCommands.js").RebootResult|null} */ (null));
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [phase, setPhase] = useState(/** @type {"idle"|"in-flight"|"terminal"} */ ("idle"));
  const [result, setResult] = useState(/** @type {import("./fleetCommands.js").RebootResult|null} */ (null));

  useEffect(() => {
    const dialog = dialogRef.current;
    const onCancel = (event) => {
      if (flying.current) event.preventDefault();
    };
    const onCloseEvent = () => {
      if (flying.current) {
        dialog.showModal(); // a close got past the guard: keep the dialog and its state
        return;
      }
      onCloseRef.current(resultRef.current);
    };
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("close", onCloseEvent);
    dialog.showModal();
    cancelRef.current?.focus();
    return () => {
      dialog.removeEventListener("cancel", onCancel);
      dialog.removeEventListener("close", onCloseEvent);
    };
  }, []);

  const stale = rebootRefusal(request, node);
  const send = async () => {
    if (flying.current) return;
    flying.current = true;
    dialogRef.current.setAttribute("closedby", "none");
    dialogRef.current.focus();
    setPhase("in-flight");
    const sent = await sendReboot(deviceId, request, node);
    flying.current = false;
    dialogRef.current?.setAttribute("closedby", "closerequest");
    resultRef.current = sent;
    onSent(sent);
    if (sent.outcome === "done") {
      dialogRef.current?.close();
      return;
    }
    setResult(sent);
    setPhase("terminal");
  };

  const close = () => {
    if (!flying.current) dialogRef.current.close();
  };

  const body = request.body;
  return (
    <dialog ref={dialogRef} className="confirm" aria-labelledby={titleId} tabIndex={-1}>
      <h2 id={titleId} className="confirm__title">{retry ? `Retry rebooting ${name}?` : `Reboot ${name}?`}</h2>
      <div className="confirm__body">
        <p>{`Host Management restarts the box on boot ${request.kernelBootId}.`}</p>
        {request.frames.length === 0 ? (
          <p>No Frames are bound to this Player.</p>
        ) : (
          <>
            <p>Frames bound to this Player go dark until it is back:</p>
            <ul className="confirm__runs" aria-label="Bound Frames and their live Runs">
              {request.frames.map((entry) => (
                <li key={entry.frameId}>
                  {entry.runs.length === 0
                    ? `Frame ${entry.frameId}: no live Run`
                    : `Frame ${entry.frameId}: live Run ${entry.runs.map((run) => `${run.sceneId} (${run.phase})`).join(", ")}`}
                </li>
              ))}
            </ul>
          </>
        )}
        <p>The Run stays active. Central sends no command to other Frames or Actuators.</p>
        <p>{`Central offers the request to Host Management for ${request.windowSeconds} s. Delivery is unknown until Host Management responds.`}</p>
        {request.previous !== null && <p>{request.previous}</p>}
        <details>
          <summary>Request</summary>
          <ul>
            <li>{`Audit reference ${body.operator_audit_ref}`}</li>
            <li>{`Command ${body.command_id}`}</li>
            <li>{`Session ${body.session_id} · device generation ${body.device_generation} · gate generation ${body.rollout_generation}`}</li>
          </ul>
        </details>
      </div>
      {phase === "terminal" && result !== null && (
        <p className="confirm__outcome" role="status">{result.message}</p>
      )}
      {stale !== null && phase !== "in-flight" && <p className="confirm__outcome" role="alert">{`${stale}.`}</p>}
      <div className="confirm__actions">
        {(phase !== "terminal" || result?.retryable) && (
          <button type="button" className="confirm__confirm" onClick={send}
            disabled={phase === "in-flight" || stale !== null}>
            {retry || phase === "terminal" ? "Retry the same request" : "Reboot Player"}
          </button>
        )}
        <button ref={cancelRef} type="button" onClick={close} disabled={phase === "in-flight"}>
          {phase === "terminal" ? "Close" : "Cancel"}
        </button>
      </div>
      {phase === "in-flight" && <p className="confirm__progress" role="status">Sending…</p>}
    </dialog>
  );
}

/**
 * The Reboot section (§9, §10): "Reboot Player", with its reason when it is unavailable,
 * and the reboot history with one named state per request. While any command on the target
 * session is outstanding the only offer is a retry of the request this page sent (its
 * frozen body); one this page does not hold cannot be resent, so Reboot waits for it to
 * expire. Retry is derived from the held request (fleetCommands.js `rebootOffer`).
 *
 * @param {{deviceId: string, name: string, node: object, snapshot: object,
 *          playerId: string|null}} props
 */
export function RebootSection({ deviceId, name, node, snapshot, playerId }) {
  const reasonId = useId();
  const openerRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  // The request this page last sent and Central may hold (fleetCommands.js `heldReboot`).
  const [held, setHeld] = useState(/** @type {import("./fleetCommands.js").FrozenRebootRequest|null} */ (null));
  const [dialog, setDialog] = useState(/** @type {{request: object, retry: boolean}|null} */ (null));
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState(/** @type {string|null} */ (null));

  if (node.enabled === false) {
    return <FactLine label="Reboot" fact={fact({ kind: "unknown", why: NODE_OFF })} />;
  }
  const commands = node.read?.reboot_commands ?? [];
  const target = rebootTarget(node, node.gate);
  const offer = rebootOffer(target, commands, node.readAt, held);
  const retryHeld = offer.offer === "retry";
  const blocked = offer.offer === "blocked" ? offer.reason : null;

  const open = () => {
    setStatus(null);
    if (retryHeld) {
      setDialog({ request: held, retry: true });
      return;
    }
    const built = rebootRequest(target, commands, snapshot, node.readAt,
      { playerId, commandId: crypto.randomUUID(), reason, held });
    if ("refused" in built) {
      setStatus(`Reboot unavailable: ${built.refused}.`);
      return;
    }
    setDialog({ request: built, retry: false });
  };

  const onSent = (result) => {
    setHeld(heldReboot(dialog.request, result));
    void node.refresh();
  };

  const onClose = (result) => {
    setDialog(null);
    if (result?.outcome === "done") setStatus(result.message);
    openerRef.current?.focus();
  };

  return (
    <>
      <div className="player__reboot">
        {!retryHeld && (
          <label className="player__reason">
            {"Reason (optional, one word)"}
            <input type="text" value={reason} maxLength={200} disabled={blocked !== null}
              onChange={(event) => setReason(event.target.value)} />
          </label>
        )}
        <button ref={openerRef} type="button" disabled={blocked !== null} onClick={open}
          aria-describedby={blocked !== null ? reasonId : undefined}>
          {retryHeld ? "Retry reboot request" : "Reboot Player"}
        </button>
        {blocked !== null && <p id={reasonId} className="roster__note">{`Reboot unavailable: ${blocked}.`}</p>}
      </div>
      {status !== null && <p className="roster__status-line" role="status">{status}</p>}
      <h4 className="player__layer-title">Reboot history</h4>
      {node.read == null ? (
        <FactLine label="Reboot history" fact={fact({ kind: "unknown", why: nodeUnknown(node) ?? "not read yet" })} />
      ) : commands.length === 0 ? (
        <p className="roster__empty">No reboot requests recorded for this box.</p>
      ) : (
        <ul className="player__commands" aria-label="Reboot history">
          {commands.map((command) => (
            <StateEntry key={command.command_id} state={rebootCommandState(command, node.readAt, { nodeDevice: node })}>
              <details className="player__details">
                <summary>{`Request ${tail(command.command_id)}`}</summary>
                <ul>
                  <li>{`Command ${command.command_id}`}</li>
                  {typeof command.issued_at === "number" && <li>{`Recorded at ${clock(command.issued_at)}`}</li>}
                  <li>{`Targeted boot ${command.command?.producer?.kernel_boot_id ?? "not served"}`}</li>
                </ul>
              </details>
            </StateEntry>
          ))}
        </ul>
      )}
      {dialog !== null && (
        <RebootDialog key={dialog.request.body.command_id} deviceId={deviceId} name={name}
          request={dialog.request} retry={dialog.retry} node={node} onSent={onSent} onClose={onClose} />
      )}
    </>
  );
}

/**
 * The App section (§9, §10): the box's app operations with their named states, read-only
 * in pass 1. Each reads the broker's response, not only Central's `state`.
 *
 * @param {{node: object}} props
 */
export function AppOperationsSection({ node }) {
  if (node.enabled === false) {
    return <FactLine label="App operations" fact={fact({ kind: "unknown", why: NODE_OFF })} />;
  }
  const read = node.operations;
  if (read == null) {
    const why = nodeUnknown(node)
      ?? (node.error ? `the app operations read failed (${node.error.code})` : "not read yet");
    return <FactLine label="App operations" fact={fact({ kind: "unknown", why })} />;
  }
  const operations = read.operations;
  return (
    <>
      <p className="player__read-time">
        {typeof read.read_at === "number" ? `App operations read as of ${clock(read.read_at)}` : "App operations: read time not served"}
        {node.error !== null && ", refresh failed"}
      </p>
      {operations.length === 0 ? (
        <p className="roster__empty">No app operations recorded for this box.</p>
      ) : (
        <ul className="player__commands" aria-label="App operations">
          {operations.map((operation) => (
            <StateEntry key={operation.operation_id} state={appOperationState(operation, read.read_at)}>
              <details className="player__details">
                <summary>{`Operation ${tail(operation.operation_id)}`}</summary>
                <ul>
                  <li>{`Operation ${operation.operation_id}`}</li>
                  <li>{`Command ${operation.command_id}`}</li>
                </ul>
              </details>
            </StateEntry>
          ))}
        </ul>
      )}
    </>
  );
}
