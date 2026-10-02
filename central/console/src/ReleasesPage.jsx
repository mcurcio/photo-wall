import React, { useEffect, useMemo, useRef, useState } from "react";

import { useConfirm } from "./ConfirmAction.jsx";
import { FactLine } from "./FactLine.jsx";
import { clock, fact } from "./facts.js";
import { effectGateFact, NodeRecords, nodeReadsAllowed, useNodeControlValue } from "./nodeControl.js";
import {
  deploymentHandle,
  publishOffer,
  PUBLISH_HELD_WORDS,
  publishConfirmation,
  publishRequest,
  releaseHome,
  selectionOffer,
  selectionRequest,
  selectionSettled,
  sendCatalogCheck,
  sendSelection,
  useHeldPublishes,
  useReleaseRead,
} from "./releases.js";
import { formatRoute } from "./routes.js";
import { SectionBoundary } from "./SectionBoundary.jsx";

// Select's scope (§26, R17): fleet-wide, and wider than any list the console could show.
const SELECT_SCOPE = "Every Player that boots by node path from now on is offered this deployment, including "
  + "Players Central has not seen. Central cannot list which Players will boot. A Pi whose kernel command line "
  + "lacks photowall.node=v2 is misconfigured and is not offered it.";
const NO_APP = "This deployment has no app: every boot from now on is offered no app.";
const SELECT_UNKNOWN = "Outcome unknown: Central did not answer. The next read of the boot selection decides.";

// What the effect gate governs (§25), beside Central's state and reason.
const GATE_GOVERNS = "Reboot and Stage app are refused while it is closed. Central opens it only from a "
  + "deployment certification; this console cannot open it. An open gate is necessary, not sufficient: Central "
  + "re-checks its serving evidence on every Reboot and Stage.";


/** "Release read as of <time>", whether its last refresh failed, or why it has no answer. */
function ReadLine({ releases }) {
  if (releases.read === null) {
    return releases.error === null
      ? <p className="player__read-time">Release read: not read yet</p>
      : <FactLine label="Release read" fact={fact({ kind: "unknown", why: `Central did not answer (${releases.error.code})` })} />;
  }
  return (
    <p className="player__read-time">
      {typeof releases.readAt === "number" ? `Release read as of ${clock(releases.readAt)}` : "Release read time not served"}
      {releases.error !== null && ", refresh failed"}
    </p>
  );
}

function SelectionSection({ read }) {
  return <FactLine label="Boot selection" fact={releaseHome(read).selection} />;
}

function DeploymentsSection({ read, onSelect }) {
  const rows = releaseHome(read).deployments;
  if (rows.length === 0) return <p className="roster__empty">No deployments. Publish a release below.</p>;
  return (
    <ul className="player__commands" aria-label="Deployments">
      {rows.map((row) => {
        const offer = selectionOffer(read, row.deploymentId);
        return (
          <li key={row.deploymentId} className="player__command" aria-label={`Deployment ${row.deploymentId}`}>
            <FactLine label="Deployment" fact={row.deployment} suffix={row.contents} />
            {row.from !== null && <FactLine label="Release" fact={row.from} />}
            {offer.offer === "selected" && <p className="player__state">Selected</p>}
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

/** "with its app" / "without its app": one publish choice's words. */
const choiceWords = (withApp) => (withApp ? "with its app" : "without its app");

/**
 * One publish choice of a release row (§25): its button, or what the page holds, or why not.
 * A release with an app offers both choices, each under its own derived deployment id; one
 * without offers only "without its app".
 */
function PublishChoice({ read, release, withApp, held, onPublish, onSendAgain }) {
  const offer = publishOffer(read, release, withApp, held);
  const choice = choiceWords(withApp);
  // With both choices on the row, every line names its choice.
  const prefix = release.app_environment_sha256 == null ? "" : `${withApp ? "With" : "Without"} its app: `;
  if (offer.offer === "publish") {
    return <button type="button" onClick={(event) => onPublish(event, release, withApp)}>{`Publish ${choice}…`}</button>;
  }
  if (offer.offer === "published") {
    return <p className="player__state">{`${prefix}Published as deployment ${deploymentHandle(offer.deploymentId)}`}</p>;
  }
  if (offer.offer === "blocked") return <p className="roster__note">{`${prefix}Publish unavailable: ${offer.reason}.`}</p>;
  return (
    <>
      <p className="player__state" role="status">{`${prefix}${PUBLISH_HELD_WORDS[offer.offer]}`}</p>
      {offer.offer === "unknown" && (
        <button type="button" onClick={(event) => onSendAgain(event, offer.deploymentId)}>
          {`Send publish ${choice} again…`}
        </button>
      )}
    </>
  );
}

function CatalogSection({ read, held, checking, onCheck, onPublish, onSendAgain }) {
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
            const choices = release.app_environment_sha256 == null ? [false] : [true, false];
            return (
              <li key={release.manifest_sha256} className="player__command" aria-label={`Release ${release.tag}`}>
                <FactLine label="Release" fact={row.catalog} />
                <FactLine label="Contents" fact={row.contents} />
                {row.verified !== null && (
                  <FactLine label="Verification" fact={row.verified} suffix="not proof a Player holds them" />
                )}
                <a href={formatRoute({ section: "releases", flow: "update", id: release.tag })}>
                  Update the wall with this…
                </a>
                {choices.map((withApp) => (
                  <PublishChoice key={String(withApp)} read={read} release={release} withApp={withApp} held={held}
                    onPublish={onPublish} onSendAgain={onSendAgain} />
                ))}
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
 * Select and Publish dialogs send through their one send functions, judged on the newest read.
 */
function ReleaseRecords() {
  const control = useNodeControlValue();
  const releases = useReleaseRead({ skip: !nodeReadsAllowed(control) });
  // The publishes this page holds (releases.js `HeldPublishes`).
  const held = useHeldPublishes();
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
          <p>{request.contents}</p>
          <p>{SELECT_SCOPE}</p>
          {request.noApp && <p>{NO_APP}</p>}
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

  // The Publish dialog, first send or "Send again" (§27): one body, frozen once, shown and sent.
  const openPublish = (event, request, again) => {
    const { lines, ...dialog } = publishConfirmation(request, again, { releases, held });
    open(event, {
      ...dialog,
      key: `publish-${again ? "again-" : ""}${request.body.deployment_id}`,
      body: (
        <>
          {lines.map((line) => <p key={line}>{line}</p>)}
          <details>
            <summary>Request</summary>
            <ul>
              <li>{`Deployment ${request.body.deployment_id}`}</li>
              <li>{`Audit reference ${request.body.operator_audit_ref}`}</li>
            </ul>
          </details>
        </>
      ),
    });
  };

  const onPublish = (event, release, withApp) => {
    const request = publishRequest(releases.read, release, withApp, held);
    if ("refused" in request) {
      setStatus(`Publish unavailable: ${request.refused}.`);
      return;
    }
    openPublish(event, request, false);
  };

  const onSendAgain = (event, deploymentId) => {
    const request = held.frozen(deploymentId);
    if (request !== null) openPublish(event, request, true);
  };

  const onCheck = async () => {
    const outcome = await sendCatalogCheck(heldCheck);
    void releases.refresh();
    setStatus(outcome.message);
  };

  const { read, readAt } = releases;
  return (
    <>
      <ReadLine releases={releases} />
      {read !== null && (read.releases ?? []).length > 0 && (
        <p>
          <a className="releases__update" href={formatRoute({ section: "releases", flow: "update", id: read.releases[0].tag })}>
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
            <CatalogSection read={read} held={held} checking={checking} onCheck={onCheck}
              onPublish={onPublish} onSendAgain={onSendAgain} />
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
 * Fleet › Releases (console DDD Part E §25, beads NR1 and NR2): the release catalog →
 * deployments → boot selection, and the effect gate: the fleet-wide aggregates' home. While node control is off it shows the one "not
 * shown" line and reads nothing (nodeControl.js `NodeRecords`).
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
