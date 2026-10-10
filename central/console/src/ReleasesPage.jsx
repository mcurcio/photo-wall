import React, { useEffect, useMemo, useRef, useState } from "react";

import { useConfirm } from "./ConfirmAction.jsx";
import { FactLine } from "./domain/fact-line.tsx";
import { clock, fact } from "./facts.js";
import { effectGateFact, NodeRecords, nodeReadsAllowed, useNodeControlValue } from "./nodeControl.js";
import {
  deploymentHandle,
  newestStable,
  releaseHome,
  selectionConfirmation,
  selectionOffer,
  selectionRequest,
  selectionSettled,
  sendCatalogCheck,
  sendSelection,
  useReleaseRead,
} from "./releases.js";
import { formatRoute } from "./routes.js";
import { SectionBoundary } from "./SectionBoundary.jsx";

const SELECT_UNKNOWN = "Outcome unknown: Central did not answer. The next read of the boot selection decides.";

// What the effect gate governs (§25), beside Central's state and reason.
const GATE_GOVERNS = "Reboot and Stage app are refused while it is closed. Central opens it only from a "
  + "deployment certification; this console cannot open it. An open gate is necessary, not sufficient: Central "
  + "re-checks its serving evidence on every Reboot and Stage.";


/** "Release read as of <time>", whether its last refresh failed, or why it has no answer. */
function ReadLine({ releases }) {
  // role=status with aria-busy while a read is in flight: busy clears only once the read is
  // settled (what Select judges), matching the snapshot status in the shell.
  const status = { role: "status", "aria-label": "Release read", "aria-busy": releases.busy ? "true" : undefined };
  if (releases.read === null) {
    return releases.error === null
      ? <p className="player__read-time" {...status}>Release read: not read yet</p>
      : (
        <div className="player__read-time" {...status}>
          <FactLine label="Release read" fact={fact({ kind: "unknown", why: `Central did not answer (${releases.error.code})` })} />
        </div>
      );
  }
  return (
    <p className="player__read-time" {...status}>
      {typeof releases.readAt === "number" ? `Release read as of ${clock(releases.readAt)}` : "Release read time not served"}
      {releases.error !== null && ", refresh failed"}
    </p>
  );
}

function SelectionSection({ read }) {
  const home = releaseHome(read);
  return (
    <>
      <FactLine label="Boot selection" fact={home.selection} />
      {home.previous !== null && <FactLine label="Previous" fact={home.previous} />}
    </>
  );
}

function DeploymentsSection({ read, onSelect }) {
  const rows = releaseHome(read).deployments;
  if (rows.length === 0) {
    return <p className="roster__empty">No deployments. Central records one for each valid release it observes.</p>;
  }
  return (
    <ul className="player__commands" aria-label="Deployments">
      {rows.map((row) => {
        const offer = selectionOffer(read, row.deploymentId);
        return (
          <li key={row.deploymentId} className="player__command" aria-label={`Deployment ${row.deploymentId}`}>
            <FactLine label="Deployment" fact={row.deployment} suffix={row.contents} />
            {row.from !== null && <FactLine label="Release" fact={row.from} />}
            {offer.offer === "selected" && <p className="player__state">Selected</p>}
            {row.previous && <p className="player__state">Previous selection</p>}
            {offer.offer === "select" && (
              <button type="button" onClick={(event) => onSelect(event, row.deploymentId)}>
                Select for every boot…
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The releases Central observed (§25, §6.5): one row per tag, newest first, each with one
 * readiness fact and one verb, "Put vX on the wall…", which opens Update the wall for it
 * (§25a: the one confirmation lives there). On the selected row it reads "Continue putting vX
 * on the wall…": the rolling reboot is tab-driven (R8), so a paused rollout of a release that
 * is not the newest stable resumes from here. A Rejected release has none.
 */
function CatalogSection({ read, checking, onCheck }) {
  const rows = releaseHome(read).releases;
  return (
    <>
      <button type="button" onClick={onCheck} disabled={checking}>
        {checking ? "Checking GitHub releases…" : "Check GitHub releases now"}
      </button>
      {rows.length === 0 ? <p className="roster__empty">GitHub releases has reported no node release yet.</p> : (
        <ul className="player__commands" aria-label="Release catalog">
          {rows.map((row) => {
            const { release } = row;
            return (
              <li key={release.tag} className="player__command" aria-label={`Release ${release.tag}`}>
                <FactLine label="Release" fact={row.catalog} />
                {row.prerelease && <p className="roster__note">Pre-release: listed, not downloaded ahead</p>}
                {row.contents !== null && <FactLine label="Contents" fact={row.contents} />}
                <FactLine label="Download" fact={row.readiness} suffix="not proof a Player holds it" />
                {row.newerRejected !== null && <p className="roster__note">{row.newerRejected}</p>}
                {row.label !== null && <p className="player__state">{row.label}</p>}
                {row.deploymentId !== null && (
                  <a href={formatRoute({ section: "releases", flow: "update", id: release.tag })}>
                    {row.label === "Selected for every boot"
                      ? `Continue putting ${release.tag} on the wall…`
                      : `Put ${release.tag} on the wall…`}
                  </a>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

/** The effect gate (§25, §26): the shell's one reading of it, in its one wording. */
function EffectGateSection({ gate }) {
  return (
    <>
      <FactLine label="Effect gate" fact={effectGateFact(gate)} />
      <p className="roster__note">{GATE_GOVERNS}</p>
    </>
  );
}

/**
 * The release sections (§25): one read feeds them all (releases.js `useReleaseRead`), and the
 * Select dialog sends through its one send function, judged on the newest read.
 */
function ReleaseRecords() {
  const control = useNodeControlValue();
  const releases = useReleaseRead({ skip: !nodeReadsAllowed(control) });
  // This page's unanswered "Check GitHub releases now" (releases.js `HeldCheck`): the same shape.
  const checkRef = useRef(false);
  const [checking, setChecking] = useState(false);
  const heldCheck = useMemo(() => ({
    get: () => checkRef.current,
    set: (value) => {
      checkRef.current = value;
      setChecking(value);
    },
  }), []);
  // A selection whose answer was lost: the first read started after the answer settles it.
  const [pending, setPending] = useState(/** @type {{request: object, after: number}|null} */ (null));
  const { open, setStatus, confirmation } = useConfirm(null);

  useEffect(() => {
    if (pending !== null && releases.seq > pending.after && releases.read !== null) {
      setStatus(selectionSettled(pending.request, releases.read).message);
      setPending(null);
    }
  }, [pending, releases.seq, releases.read, setStatus]);

  const onSelect = (event, deploymentId) => {
    const request = selectionRequest(releases.read, deploymentId);
    if ("refused" in request) {
      setStatus(`Select unavailable: ${request.refused}.`);
      return;
    }
    setPending(null);
    open(event, {
      key: `select-${deploymentId}-${request.body.expected_revision}`,
      title: `Select deployment ${deploymentHandle(deploymentId)} for every boot?`,
      body: (
        <>
          {selectionConfirmation(request).map((line) => <p key={line}>{line}</p>)}
          <details>
            <summary>Request</summary>
            <ul>
              <li>{`Deployment ${deploymentId}`}</li>
              <li>{`Expected revision ${request.body.expected_revision}`}</li>
            </ul>
          </details>
        </>
      ),
      confirmLabel: "Select for every boot",
      run: async () => {
        const outcome = await sendSelection(request, releases);
        if (outcome.outcome === "unknown") setPending({ request, after: releases.startedReads() });
        void releases.refresh();
        return { state: outcome.outcome, message: outcome.outcome === "unknown" ? SELECT_UNKNOWN : outcome.message };
      },
    });
  };

  const onCheck = async () => {
    const outcome = await sendCatalogCheck(heldCheck);
    void releases.refresh();
    setStatus(outcome.message);
  };

  const { read, readAt } = releases;
  const newest = newestStable(read);
  return (
    <>
      <ReadLine releases={releases} />
      {newest !== null && (
        <p>
          <a className="releases__update" href={formatRoute({ section: "releases", flow: "update", id: newest.tag })}>
            Update the wall…
          </a>
        </p>
      )}
      {confirmation("roster__status-line")}
      {read !== null && (
        <>
          <SectionBoundary title="Boot selection" resetKey={readAt}>
            <SelectionSection read={read} />
          </SectionBoundary>
          <SectionBoundary title="Deployments" resetKey={readAt}>
            <DeploymentsSection read={read} onSelect={onSelect} />
          </SectionBoundary>
          <SectionBoundary title="Release catalog" resetKey={readAt}>
            <CatalogSection read={read} checking={checking} onCheck={onCheck} />
          </SectionBoundary>
        </>
      )}
      <SectionBoundary title="Effect gate" resetKey={control.gate}>
        <EffectGateSection gate={control.gate} />
      </SectionBoundary>
    </>
  );
}

/**
 * Fleet › Releases (console DDD Part E §25, beads NR1, NR2 and B7): the releases Central
 * observed and downloads by itself → their deployments → the boot selection, and the effect
 * gate: the fleet-wide aggregates' home. While node control is off it shows the one "not shown"
 * line and reads nothing (nodeControl.js `NodeRecords`).
 */
export function ReleasesPage() {
  return (
    <div className="player">
      <NodeRecords>
        <ReleaseRecords />
      </NodeRecords>
    </div>
  );
}
