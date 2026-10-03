import React, { useEffect, useId, useRef, useState } from "react";

import { fact } from "./facts.js";
import { FactLine } from "./FactLine.jsx";
import {
  beginOffer,
  beginRequest,
  qualificationView,
  REQUIREMENT,
  SAMPLING_COST,
  sendBegin,
  useQualificationSampler,
} from "./qualification.js";
import { appHandle } from "./releases.js";

/**
 * App › Qualified fallback (Part E §25, §27): the linked app this boot would qualify, Begin
 * (naming it), this page's sampling while it runs, and the acceptances Central stored. The
 * attempt lives in this component's memory only; Central keeps its rows when the page closes.
 * Begin goes through `sendBegin`; samples through `useQualificationSampler`.
 *
 * `onPhase` (Update the wall, Part E §25a) hears this attempt's phase whenever it changes:
 * "sampling", "accepted", or "stopped" with the stop words; it decides nothing here.
 *
 * @param {{deviceId: string, node: object, snapshot: object|null, playerId: string|null,
 *          onPhase?: (phase: "sampling"|"accepted"|"stopped", words: string|null) => void}} props
 */
export function QualifiedFallback({ deviceId, node, snapshot, playerId, onPhase }) {
  const titleId = useId();
  const reasonId = useId();
  // {request, state}: "in_flight", "unknown" (its answer lost: only it may be sent again),
  // "sampling" (Central began it), "stopped" (the operator stopped sampling).
  const [attempt, setAttempt] = useState(/** @type {{request: object, state: string}|null} */ (null));
  const [message, setMessage] = useState(/** @type {string|null} */ (null));
  const sampling = attempt?.state === "sampling";
  const sampler = useQualificationSampler(attempt?.state === "sampling" || attempt?.state === "stopped"
    ? attempt.request.body.qualification_id : null, { active: sampling });
  const running = sampling && sampler.phase === "sampling";
  const refreshed = useRef(/** @type {string|null} */ (null));

  useEffect(() => {
    // An accepted qualification is listed by the next app-attempts read: read it now.
    if (sampler.phase === "accepted" && refreshed.current !== sampler.id) {
      refreshed.current = sampler.id;
      void node.refresh();
    }
  }, [sampler.phase, sampler.id, node]);

  const stopped = attempt?.state === "stopped" ? "Stopped: you stopped sampling. Begin again when ready."
    : sampler.phase === "stopped" ? sampler.stopped : null;
  const phase = sampler.phase === "accepted" ? "accepted" : stopped !== null && attempt !== null ? "stopped"
    : running ? "sampling" : null;
  const onPhaseRef = useRef(onPhase);
  onPhaseRef.current = onPhase;
  useEffect(() => {
    if (phase !== null) onPhaseRef.current?.(phase, phase === "stopped" ? stopped : null);
  }, [phase, stopped]);

  const view = qualificationView(node.operations);
  const offer = beginOffer(node.operations, snapshot, playerId, running || attempt?.state === "in_flight");
  const resend = attempt?.state === "unknown";

  const begin = async () => {
    const request = resend ? attempt.request
      : beginRequest(node.operations, snapshot, playerId, crypto.randomUUID());
    if ("refused" in request) {
      setMessage(`Not begun: ${request.refused}.`);
      return;
    }
    setMessage(null);
    setAttempt({ request, state: "in_flight" });
    const outcome = await sendBegin(deviceId, request, { node, snapshot, playerId }, false);
    if (outcome.outcome === "done" || outcome.outcome === "already") {
      setAttempt({ request, state: "sampling" });
    } else {
      setAttempt(outcome.outcome === "unknown" ? { request, state: "unknown" } : null);
      setMessage(outcome.message);
    }
  };

  const reason = resend ? null : offer.offer === "blocked" ? offer.reason : null;
  const label = resend ? "Send the same Begin again"
    : offer.offer === "begin" ? `Begin qualifying app ${appHandle(offer.environmentSha256)}` : "Begin qualifying";
  return (
    <section aria-labelledby={titleId}>
      <h4 id={titleId} className="player__layer-title">Qualified fallback</h4>
      <p className="roster__note">{REQUIREMENT}</p>
      <FactLine label="Linked app" fact={view.linkedApp} />
      <p className="roster__note">{SAMPLING_COST}</p>
      <div className="player__stage">
        <button type="button" onClick={begin} disabled={offer.offer !== "begin" && !resend}
          aria-describedby={reason !== null ? reasonId : undefined}>
          {label}
        </button>
        {running && (
          <button type="button" onClick={() => setAttempt({ ...attempt, state: "stopped" })}>Stop sampling</button>
        )}
      </div>
      {reason !== null && offer.offer !== "sampling" && (
        <p id={reasonId} className="roster__note">{`Begin unavailable: ${reason}.`}</p>
      )}
      {message !== null && <p className="roster__status-line" role="alert">{message}</p>}
      {sampler.id !== null && attempt !== null && attempt.state !== "unknown" && (
        <div role="status" aria-label="Qualification progress">
          {sampler.phase === "accepted" ? (
            <FactLine label="Qualification" fact={fact({ kind: "set", value: sampler.answer.text })} />
          ) : stopped !== null ? (
            <p className="roster__status-line">{stopped}</p>
          ) : sampler.answer === null ? (
            <p className="roster__status-line">Sampling: waiting for Central's first answer.</p>
          ) : sampler.answer.kind === "progress" ? (
            <FactLine label="Qualification" fact={fact({ kind: "derived", value: sampler.answer.text,
              basis: "its answer to this page's latest sample" })} />
          ) : (
            <p className="roster__status-line">{sampler.answer.text}</p>
          )}
        </div>
      )}
      {view.acceptances.length === 0 ? (
        <p className="roster__empty">No qualified fallback recorded for this Player on this enrollment.</p>
      ) : (
        <ul className="player__commands" aria-label="Qualified fallbacks">
          {view.acceptances.map((entry, index) => (
            <li key={index}><FactLine label="Stored acceptance" fact={entry} /></li>
          ))}
        </ul>
      )}
    </section>
  );
}
