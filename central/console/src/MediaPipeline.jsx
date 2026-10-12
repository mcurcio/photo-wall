import React, { useEffect, useState } from "react";

import { readCandidates } from "./candidatesApi.js";
import { rankedContributions } from "./join.js";
import { ReadinessNotice } from "./ReadinessNotice.jsx";
import { sourceName } from "./sourceNames.js";
import {
  checkCounts,
  mediaNow,
  sourceState,
  whyNothingNew,
  workerLoad,
  workerState,
} from "./mediaHealth.js";
import { sourcePool } from "./photoSourcesApi.js";
import { counted, lastCompleteWords } from "./sourceWords.js";

/**
 * The media pipeline (pass 2 slice 3 §14), in the "Now" column: the worker
 * (checked in, jobs, cache) and each Source's check (roadmap 4), from the media read
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
        <ul className="media-pipeline__sources" aria-label="Source checks">
          {sources.map((source) => (
            <SourceCheck key={source.source_ref} source={source} now={now} />
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * One Source's check: its state in words, how far a running check has read, and when its last
 * complete check was (against the media read time: both are the database's clock).
 */
function SourceCheck({ source, now }) {
  const state = sourceState(source, now);
  const check = source.check ?? null;
  const last = lastCompleteWords(check, now);
  return (
    <li className="media-pipeline__source" aria-label={`Check of ${sourceName(source)}`}>
      <dl className="record">
        <dt>Source</dt>
        <dd>{sourceName(source)}</dd>
        <dt>State</dt>
        <dd className={`health--${state.severity}`}>{state.label}</dd>
        {check?.state === "checking" && (
          <>
            <dt>Checking</dt>
            <dd>{`Checking: ${counted(check.seen_so_far, "photo")} so far`}</dd>
          </>
        )}
        {last !== null && (
          <>
            <dt>Last complete check</dt>
            <dd>{last}</dd>
          </>
        )}
      </dl>
    </li>
  );
}

/**
 * Each of `refs`' pools as "Why nothing new?" judges them: its photos and whether its check is
 * running, read once per ref list and again when a served check moves on.
 */
function usePools(refs, sources) {
  const [pools, setPools] = useState({});
  const key = JSON.stringify(refs.map((ref) => [ref,
    sources.find((source) => source.source_ref === ref)?.check ?? null]));
  useEffect(() => {
    let current = true;
    const wanted = JSON.parse(key).map(([ref]) => ref);
    Promise.all(wanted.map(async (ref) => [ref, await sourcePool(ref)])).then((answers) => {
      if (!current) return;
      setPools(Object.fromEntries(answers.filter(([, answer]) => answer.ok).map(([ref, { pool }]) => [ref, {
        photos: pool.in_pool.photos + pool.in_pool.videos,
        checking: pool.check?.state === "checking" || pool.check?.state === "never_checked",
      }])));
    });
    return () => { current = false; };
  }, [key]);
  return pools;
}

/**
 * "Why nothing new on <frame>?" (§14): its own group, beside the ranked Why
 * list and never inside it. Each step is a served fact; the first one that is
 * not ok is marked "Stops here". "Check this frame" reads the frame's
 * candidates on demand and tallies the standing Central serves for each.
 *
 * @param {{snapshot: object|null, frameId: string}} props
 */
export function WhyNothingNew({ snapshot, frameId }) {
  const [check, setCheck] = useState(/** @type {object|null} */ (null));
  const [checking, setChecking] = useState(false);
  const [winner] = rankedContributions(snapshot?.runtime, frameId);
  const pools = usePools(winner?.source_refs ?? [], snapshot?.media?.sources ?? []);
  const steps = whyNothingNew(snapshot, frameId, check, pools);
  const title = `Why nothing new on ${frameId}?`;

  const runCheck = async () => {
    const [winner] = rankedContributions(snapshot?.runtime, frameId);
    setChecking(true);
    try {
      const reads = await Promise.all(
        // At the winning Scene's crop limit (roadmap 5a), as the Planner judges its photos.
        (winner?.source_refs ?? []).map((ref) => readCandidates(ref, frameId, winner?.selection?.crop_limit)),
      );
      setCheck({ counts: checkCounts(reads) });
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
      <ReadinessNotice snapshot={snapshot} frameId={frameId} />
    </div>
  );
}
