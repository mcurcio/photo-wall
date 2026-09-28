import React, { useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { rankedContributions } from "./join.js";
import {
  mediaNow,
  sourceState,
  splitCandidates,
  whyNothingNew,
  workerLoad,
  workerState,
} from "./mediaHealth.js";

/**
 * One frame's candidates from one Source: `GET /v1/operator/sources/{ref}/
 * candidates?frame_id=<frame>`, which Central HARD-FILTERS by the frame's
 * profile (design J4). The authoring choosers and "Check this frame" both read
 * through here. Throws on a refusal, with the served error code.
 *
 * @param {string} sourceRef
 * @param {string} frameId
 * @returns {Promise<Array<object>>}
 */
export async function readCandidates(sourceRef, frameId) {
  // frame_id makes Central drop every asset ineligible for THIS frame's
  // profile — the profile hard-filter. Dropping it would offer incompatible
  // assets, which is exactly what the chooser's mutation probe attacks.
  const result = await apiWrite(
    `/v1/operator/sources/${encodeURIComponent(sourceRef)}/candidates?frame_id=${encodeURIComponent(frameId)}`,
    { method: "GET" },
  );
  if (!result.ok) {
    throw new Error(result.error ?? `HTTP ${result.status}`);
  }
  return result.data?.candidates ?? [];
}

/**
 * The media pipeline (pass 2 slice 3 §14), in the "Now" column: the worker
 * (checked in, jobs, cache) and each Source's refresh, from the media read
 * Plane A already fetches. Central fetches and prepares all media; Players
 * receive it only from Central.
 *
 * @param {{snapshot: object|null}} props
 */
export function MediaPipeline({ snapshot }) {
  const now = mediaNow(snapshot);
  const health = snapshot?.media?.health ?? null;
  const worker = workerState(health, now);
  const sources = snapshot?.media?.sources ?? [];
  return (
    <section className="showrunner__region" role="region" aria-label="Media pipeline">
      <h2 className="showrunner__region-title">Media pipeline</h2>
      <p className="showrunner__region-note">
        Central fetches media from the photo library and prepares it; Players get it only
        from Central.
      </p>
      <dl className="record">
        <dt>Worker</dt>
        <dd className={`health--${worker?.severity ?? "todo"}`} aria-label="Media worker">
          {worker === null ? "Its state was not served." : worker.label}
        </dd>
        {worker !== null && worker.state !== "ok" && (
          <>
            <dt>Jobs and cache</dt>
            <dd>{workerLoad(health)}</dd>
          </>
        )}
      </dl>
      {sources.length === 0 ? (
        <p className="showrunner__empty">No Sources yet.</p>
      ) : (
        <ul className="media-pipeline__sources" aria-label="Source refreshes">
          {sources.map((source) => (
            <SourceRefresh key={source.source_ref} source={source} now={now} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** One Source's refresh: its state, counts, diagnostics and next refresh. */
function SourceRefresh({ source, now }) {
  const state = sourceState(source, now);
  const counts = source.counts ?? {};
  const reported = [...new Set((source.diagnostics ?? []).map((entry) => entry.code))];
  const next = source.next_refresh ? source.next_refresh - now : null;
  return (
    <li className="media-pipeline__source" aria-label={`Refresh of ${source.source_ref}`}>
      <dl className="record">
        <dt>Source</dt>
        <dd>{source.source_ref}</dd>
        <dt>State</dt>
        <dd className={`health--${state.severity}`}>{state.label}</dd>
        {counts.discovered !== undefined && (
          <>
            <dt>Last refresh</dt>
            <dd>
              {`found ${counts.discovered ?? 0} · usable ${counts.valid ?? 0} · pending ` +
                `${counts.pending ?? 0} · rejected ${counts.rejected ?? 0}`}
            </dd>
          </>
        )}
        {reported.length > 0 && (
          <>
            <dt>Reported</dt>
            <dd>{reported.map((code) => code.replaceAll("_", " ")).join(", ")}</dd>
          </>
        )}
        {next !== null && Number.isFinite(next) && (
          <>
            <dt>Next refresh</dt>
            <dd>{next > 0 ? `due in ${Math.ceil(next)} s` : "due now"}</dd>
          </>
        )}
      </dl>
    </li>
  );
}

/**
 * "Why nothing new on <frame>?" (§14): its own group, beside the ranked Why
 * list and never inside it. Each step is a served fact; the first one that is
 * not ok is marked "Stops here". "Check this frame" reads the frame's
 * candidates on demand and splits them as Central's planner would.
 *
 * @param {{snapshot: object|null, frameId: string}} props
 */
export function WhyNothingNew({ snapshot, frameId }) {
  const [check, setCheck] = useState(/** @type {object|null} */ (null));
  const [checking, setChecking] = useState(false);
  const steps = whyNothingNew(snapshot, frameId, check);
  const title = `Why nothing new on ${frameId}?`;

  const runCheck = async () => {
    const [winner] = rankedContributions(snapshot?.runtime, frameId);
    const frame = (snapshot?.inventory?.frames ?? []).find((entry) => entry.id === frameId);
    setChecking(true);
    try {
      // Several Sources merge by asset id, as the planner merges them.
      const lists = await Promise.all(
        (winner?.source_refs ?? []).map((ref) => readCandidates(ref, frameId)),
      );
      const merged = new Map(lists.flat().map((candidate) => [candidate.asset_id, candidate]));
      setCheck({ counts: splitCandidates([...merged.values()], frame?.profile ?? null) });
    } catch (error) {
      setCheck({ error: error instanceof Error ? error.message : "the request did not complete" });
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="why-chain" role="group" aria-label={title}>
      <h3 className="why-chain__title">{title}</h3>
      <ol className="why-chain__steps" aria-label="Steps">
        {steps.map((step) => (
          <li
            key={step.title}
            className={`why-chain__step why-chain__step--${step.state}`}
            aria-current={step.stops ? "step" : undefined}
          >
            {step.stops && <strong className="why-chain__stop">Stops here. </strong>}
            <span className="why-chain__step-title">{`${step.title} `}</span>
            {step.text}
            {step.title === "Check this frame" && step.state !== "skip" && (
              <button type="button" disabled={checking} onClick={runCheck}>
                {check === null ? "Check this frame" : "Check again"}
              </button>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
