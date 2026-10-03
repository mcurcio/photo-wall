import React, { useId, useRef, useState } from "react";

import { useNodeControlValue } from "./nodeControl.js";
import { useReleaseRead } from "./releases.js";
import { formatRoute } from "./routes.js";
import {
  boundFrames,
  boundRuleLines,
  pendingStage,
  sendStage,
  stageBlocker,
  stageRefusal,
  stageReplaces,
  stageRequest,
  stageScope,
  stageTargets,
  useHeldStage,
} from "./stage.js";
import { useSendDialog } from "./useSendDialog.js";

// The effect gate's and the deployments' home (Part E §25).
const RELEASES = formatRoute({ section: "releases" });

/**
 * The bound rule (§25, D16) with its qualification caveat: the one rendering for every Stage
 * surface, over stage.js `boundRuleLines` (the rule, then the caveat as a note).
 */
export function BoundRule() {
  const [rule, ...caveats] = boundRuleLines();
  return (
    <>
      <p>{rule}</p>
      {caveats.map((line) => <p key={line} className="roster__note">{line}</p>)}
    </>
  );
}

/**
 * The Stage app dialog (Part E §25, §27): lists the deployments that carry an app (the release
 * read, mounted only while this dialog is open), states that a stage lasts this boot only, what
 * a newer stage replaces and, when the Player drives Frames, the bound rule. Its ids and the
 * reads it froze its fences on are fixed when it opens (`opened`), so the request built from a
 * choice is the same every time; once sent, the choice is locked and "Send the same request
 * again" re-sends that very request. `sendStage` judges every send on the newest reads.
 *
 * @param {{deviceId: string, name: string, opened: object, held: import("./stage.js").HeldStages,
 *          node: object, control: import("./nodeControl.js").NodeControl,
 *          onSent: (result: object) => void, onClose: (result: object|null) => void}} props
 */
function StageDialog({ deviceId, name, opened, held, node, control, onSent, onClose }) {
  const titleId = useId();
  const { dialogRef, cancelRef, phase, result, run, close } = useSendDialog(onClose);
  const releases = useReleaseRead();
  const sentRef = useRef(opened.request ?? null);
  const [chosen, setChosen] = useState(opened.request?.body.deployment_id ?? "");
  const targets = stageTargets(releases.read);
  const request = sentRef.current
    ?? (chosen === "" ? null : stageRequest(opened.device, opened.gate, chosen, opened.ids));
  const refusal = request === null ? null : "refused" in request ? request.refused
    : stageRefusal(request, node, control.gate, held.get());
  const again = sentRef.current !== null;

  const send = () => {
    if (request === null || "refused" in request) return;
    sentRef.current = request;
    return run(() => sendStage(deviceId, request, { node, control }, held), onSent);
  };

  const replaces = sentRef.current?.replaces ?? opened.replaces;
  return (
    <dialog ref={dialogRef} className="confirm" aria-labelledby={titleId} tabIndex={-1}>
      <h2 id={titleId} className="confirm__title">{`Stage an app on ${name}?`}</h2>
      <div className="confirm__body">
        <p>{stageScope(releases.read?.selection)}</p>
        {replaces !== null && <p>{replaces}</p>}
        {opened.frames.length > 0 && (
          <>
            <BoundRule />
            <p>{`Frames this Player drives: ${opened.frames.join(", ")}.`}</p>
          </>
        )}
        <fieldset className="confirm__choices" disabled={again || phase === "in-flight"}>
          <legend>Deployment</legend>
          {releases.read == null ? (
            <p>{releases.error ? `Deployments could not be read (${releases.error.code}).` : "Reading deployments…"}</p>
          ) : targets.length === 0 ? (
            <p>
              {"No deployment carries an app. Publish a release with its app on "}
              <a href={RELEASES}>Releases</a>
              {"."}
            </p>
          ) : targets.map((row) => (
            <label key={row.deploymentId} className="confirm__choice">
              <input type="radio" name="stage-deployment" value={row.deploymentId}
                checked={chosen === row.deploymentId} onChange={() => setChosen(row.deploymentId)} />
              {`Deployment ${row.deploymentId.slice(0, 4)}… · ${row.contents}${row.selected ? " · selected for every boot" : ""}`}
            </label>
          ))}
        </fieldset>
        <p>Central judges this stage when you send it and answers in its own words; a refusal records nothing.</p>
        {request !== null && !("refused" in request) && (
          <details>
            <summary>Request</summary>
            <ul>
              <li>{`Audit reference ${request.body.operator_audit_ref}`}</li>
              <li>{`Operation ${request.body.operation_id} · command ${request.body.command_id}`}</li>
              <li>{`Session ${request.body.session_id} · device generation ${request.body.device_generation} · gate generation ${request.body.rollout_generation}`}</li>
            </ul>
          </details>
        )}
      </div>
      {phase === "terminal" && result !== null && (
        <p className="confirm__outcome" role="status">{result.message}</p>
      )}
      {refusal !== null && phase !== "in-flight" && !(phase === "terminal" && result?.outcome !== "unknown") && (
        <p className="confirm__outcome" role="alert">{`${refusal}.`}</p>
      )}
      <div className="confirm__actions">
        {(phase === "idle" || result?.outcome === "unknown") && (
          <button type="button" className="confirm__confirm" onClick={send}
            disabled={phase === "in-flight" || request === null || refusal !== null}>
            {again ? "Send the same request again" : "Stage app"}
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
 * App › Stage app (Part E §25): the button, disabled with its reason on served facts only
 * (stage.js `stageBlocker`; a gate reason links to Releases › Effect gate), and the dialog.
 * The page holds the stage it sent until the app-attempts read lists it; while its answer is
 * lost, only that request may be sent again. The gate is the shell's, read again on open.
 *
 * @param {{deviceId: string, name: string, node: object, snapshot: object,
 *          player: {id: string, retired_at?: number|null}|null}} props
 */
export function StageApp({ deviceId, name, node, snapshot, player }) {
  const reasonId = useId();
  const openerRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const control = useNodeControlValue();
  const held = useHeldStage();
  const [opened, setOpened] = useState(/** @type {object|null} */ (null));
  const [status, setStatus] = useState(/** @type {string|null} */ (null));

  const pending = pendingStage(held.get(), node.operations);
  const blocker = stageBlocker(node, node.operations, control.gate, player);
  const waiting = pending !== null && pending.state !== "unknown"
    ? "this page's stage is not listed yet; the next read settles it" : null;
  const reason = pending?.state === "unknown" ? null : waiting ?? blocker?.reason ?? null;

  const open = () => {
    setStatus(null);
    void control.refresh(); // the gate, read again on demand when a dialog opens (§25)
    const playerId = player?.id ?? null;
    setOpened(pending?.state === "unknown"
      ? { request: pending.request, frames: boundFrames(snapshot, playerId), replaces: pending.request.replaces }
      : { device: node, gate: control.gate, frames: boundFrames(snapshot, playerId), replaces: stageReplaces(node.operations),
        ids: { operationId: crypto.randomUUID(), commandId: crypto.randomUUID() } });
  };

  const onSent = () => {
    void node.refresh();
  };

  const onClose = (result) => {
    setOpened(null);
    if (result?.outcome === "done") setStatus(result.message);
    openerRef.current?.focus();
  };

  return (
    <div className="player__stage">
      <button ref={openerRef} type="button" disabled={reason !== null} onClick={open}
        aria-describedby={reason !== null ? reasonId : undefined}>
        {pending?.state === "unknown" ? "Send the held stage again…" : "Stage app…"}
      </button>
      {reason !== null && (
        <p id={reasonId} className="roster__note">
          {`Stage app unavailable: ${reason}.`}
          {waiting === null && blocker?.gate === true && <>{" "}<a href={RELEASES}>See Releases › Effect gate</a></>}
        </p>
      )}
      {status !== null && <p className="roster__status-line" role="status">{status}</p>}
      {opened !== null && (
        <StageDialog deviceId={deviceId} name={name} opened={opened} held={held} node={node} control={control}
          onSent={onSent} onClose={onClose} />
      )}
    </div>
  );
}
