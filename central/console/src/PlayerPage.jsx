import React, { useEffect, useId, useRef, useState } from "react";

import { deprecatedBootFact } from "./bootFacts.js";
import { unbindAllRequest, useConfirm } from "./ConfirmAction.jsx";
import { bind, identifyOutput } from "./equipmentApi.js";
import { FactLine } from "./domain/fact-line.tsx";
import { PiHeader } from "./domain/pi-header.tsx";
import { clock, fact, words } from "./facts.js";
import {
  interruptionFor, isBound, outputLabel, outputStates,
} from "./health.js";
import { NodeRecords, nodeReadsAllowed, useNodeControlValue } from "./nodeControl.js";
import { currentSessionBoot, layerEvidence, useNodeDevice } from "./nodeRead.js";
import { AppOperationsSection } from "./PlayerCommands.jsx";
import { QualifiedFallback } from "./QualifiedFallback.jsx";
import { StageApp } from "./StageApp.jsx";
import {
  BOOT_FACTS_UNAVAILABLE, enrolledFact, identifyOffer, panelAtEnrollment, playersByDevice, RETIRED_NOT_READ,
} from "./players.js";
import { ReadinessNotice } from "./ReadinessNotice.jsx";
import { formatRoute, isPlainClick, routeIdName } from "./routes.js";
import { SectionBoundary } from "./SectionBoundary.jsx";
import { useMutate } from "./useMutate.js";

const NO_FRAMES = "No unbound frames. Draw one on the plan first.";


/** A link to a Frame's home on the Wall (it opens the facet showing its cause). */
function FrameLink({ frameId, wall }) {
  return (
    <a
      href={formatRoute(wall.frameRoute(frameId))}
      onClick={(event) => {
        if (isPlainClick(event)) wall.prepareVisit(frameId);
      }}
    >
      {`Frame ${frameId}`}
    </a>
  );
}

/** "as of <time>" for a section's read, and whether its last refresh failed. */
function ReadTime({ what, at, failed }) {
  return (
    <p className="player__read-time">
      {at == null ? `${what}: not read yet` : `${what} as of ${clock(at)}`}
      {failed && ", refresh failed"}
    </p>
  );
}

/**
 * The software layer rows (§9 Layers, L1 to L2) and the closing Panel line. Host Management's
 * row (L0) lives on the Pi's Hardware page, in Link and sessions.
 */
function LayersSection({ node, snapshot, playerId, retired }) {
  const rows = layerEvidence({ nodeDevice: node, snapshot, playerId }).filter((row) => row.key !== "host");
  const shown = retired ? rows.filter((row) => row.key === "app") : rows;
  return (
    <>
      {retired ? (
        <p className="player__read-time">{`${RETIRED_NOT_READ} (App Manager, App Effect Broker, Display Host)`}</p>
      ) : (
        <ReadTime what="Node read" at={node.readAt} failed={node.error !== null && node.read !== null} />
      )}
      <ReadTime what="Player app read" at={snapshot?.inventory?.read_at} failed={false} />
      <ul className="player__layers" aria-label="Layers">
        {shown.map((row) => (
          <li key={row.key} className="player__layer">
            <div role="group" aria-label={row.layer}>
              <h4 className="player__layer-title">{`${row.layer} (${row.level})`}</h4>
              {row.facts.map((entry, index) => (
                <FactLine key={`${entry.label}-${index}`} label={entry.label} fact={entry.fact} />
              ))}
              {row.details.length > 0 && (
                <details className="player__details">
                  <summary>{`${row.layer} details`}</summary>
                  <ul>{row.details.map((detail) => <li key={detail}>{detail}</li>)}</ul>
                </details>
              )}
            </div>
          </li>
        ))}
      </ul>
      <FactLine label="Panel pixels" fact={fact({ kind: "unknown", why: "no layer observes them" })} />
    </>
  );
}

/**
 * The Boot section (Part E §25): the current node session's boot, then the latest node boot
 * offer record. When Central serves `deprecated_boot` (G5), one warning line above them says
 * this box's newest boot record is not a node boot; the console shows none of those records.
 */
function BootSection({ node, retired }) {
  if (retired) {
    return <p className="player__read-time">{RETIRED_NOT_READ}</p>;
  }
  const current = currentSessionBoot(node);
  const read = node.read;
  let offer = current.none?.kind === "unknown" ? current.none : null;
  if (read != null) {
    // Node boot offer (/v2/node/boot-offers): the latest the device read holds.
    const [latest] = read.boot_claims ?? [];
    if (latest !== undefined) {
      offer = fact({
        kind: "set",
        value: latest.offer_refusal == null
          ? `Issued for boot ${latest.kernel_boot_id}, not proof the Player booted`
          : `Refused for boot ${latest.kernel_boot_id}: ${words(latest.offer_refusal)}`,
        receivedAt: latest.first_received_at, readAt: read.read_at,
      });
    }
  }
  const deprecated = read == null ? null : deprecatedBootFact(read.deprecated_boot, read.read_at);
  return (
    <>
      {deprecated !== null && (
        <div className="player__warning" role="note">
          <FactLine label="Booted by the deprecated path" fact={deprecated}
            suffix="its kernel command line lacks photowall.node=v2; Select and Stage do not reach it" />
        </div>
      )}
      <FactLine label="Current node session's boot" fact={current.fact ?? current.none} />
      {offer === null ? (
        <p className="roster__empty">No node boot offer recorded for this box.</p>
      ) : (
        <FactLine label="Node boot offer" fact={offer} />
      )}
      {deprecated === null && (
        <p className="roster__note">
          A Pi boots by node path when its kernel command line carries <code>photowall.node=v2</code>.
        </p>
      )}
    </>
  );
}

/**
 * The Outputs section: each Output's Binding (a link to the Frame), the shared bind write
 * for a free Output (design rule 1: the same `bind` and Frame-generation fence the Binding
 * facet uses), Identify Panel where players.js `identifyOffer` offers it (any unbound
 * Output, whatever the Player's standing, as on the Binding facet), the Panel at enrollment and,
 * on a bound Output, its Output interruption when Central serves one (health.js
 * `interruptionFor`, the same fact Frame health shows).
 */
function OutputsSection({ snapshot, bootFacts, row, wall, setStatus }) {
  const mutate = useMutate();
  const ids = useId();
  const player = row.player;
  // Output picks: output id -> {frameId, generation} captured on selection.
  const [picks, setPicks] = useState(() => new Map());
  const [messages, setMessages] = useState(() => new Map());
  const [busy, setBusy] = useState(null);
  const [navigateTo, setNavigateTo] = useState(/** @type {string|null} */ (null));
  const frames = snapshot?.inventory?.frames ?? [];
  const unbound = frames
    .filter((frame) => !isBound(frame))
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const outputs = outputStates(snapshot, player.id);
  const reports = new Map((snapshot?.inventory?.outputs ?? [])
    .filter((output) => output.player_id === player.id)
    .map((output) => [output.output_id, output.observation]));

  // A pick whose Frame is no longer unbound is dropped by the next snapshot and announced,
  // except the one whose bind is in flight: its own refresh shows the Frame bound.
  useEffect(() => {
    const available = new Set(unbound.map((frame) => frame.id));
    const gone = [...picks].filter(([, pick]) => !available.has(pick.frameId));
    if (gone.length === 0) return;
    setPicks(new Map([...picks].filter(([, pick]) => available.has(pick.frameId))));
    const vanished = gone.filter(([key]) => key !== busy).map(([, pick]) => pick.frameId);
    if (vanished.length > 0) {
      const named = vanished.length === 1 ? `Frame ${vanished[0]} is` : `Frames ${vanished.join(", ")} are`;
      setStatus(`${named} no longer available. Choose another frame.`);
    }
  }, [snapshot]);

  // After a bind: open the Frame once the refresh has rendered (to calibrate it).
  useEffect(() => {
    if (navigateTo !== null) {
      wall.visitFrame(navigateTo);
      setNavigateTo(null);
    }
  }, [navigateTo]);

  const setKeyed = (setter, key, value) =>
    setter((current) => {
      const next = new Map(current);
      if (value === null) next.delete(key);
      else next.set(key, value);
      return next;
    });

  const pick = (outputId, frameId) => {
    setKeyed(setMessages, outputId, null);
    const frame = frames.find((candidate) => candidate.id === frameId);
    setKeyed(setPicks, outputId, frame ? { frameId, generation: frame.generation } : null);
  };

  const doBind = async (outputId) => {
    const chosen = picks.get(outputId);
    if (chosen === undefined || busy !== null) return;
    setBusy(outputId);
    setKeyed(setMessages, outputId, null);
    const result = await mutate(() => bind(chosen.frameId, player.id, outputId, chosen.generation));
    setBusy(null);
    setKeyed(setPicks, outputId, null); // an attempt spends the choice (as the Binding facet)
    if (result.outcome === "done") setNavigateTo(chosen.frameId);
    else setKeyed(setMessages, outputId, { kind: "alert", text: result.message });
  };

  const doIdentify = async (outputId) => {
    if (busy !== null) return;
    setBusy(outputId);
    setKeyed(setMessages, outputId, null);
    const result = await mutate(() => identifyOutput(player.id, outputId));
    setBusy(null);
    setKeyed(setMessages, outputId, result.outcome === "done"
      ? { kind: "status",
        text: `Identify requested for ${outputId}. Check the Panel; this request expires in 15 seconds.` }
      : { kind: "alert", text: result.message });
  };

  if (outputs.length === 0) {
    return <p className="roster__empty">No outputs reported</p>;
  }
  return (
    <ul className="roster__outputs" aria-label={`Outputs of ${row.name}`}>
      {outputs.map((entry, index) => {
        const label = outputLabel(snapshot, bootFacts, player.id, entry.outputId);
        const chosen = picks.get(entry.outputId);
        const message = messages.get(entry.outputId);
        const identify = identifyOffer(snapshot, player.id, entry.outputId);
        const reasonId = `${ids}-identify-${index}`;
        const interruption = entry.frameId === null ? null : interruptionFor(snapshot, entry.frameId);
        return (
          <li key={entry.outputId} className={`roster__output roster__output--${entry.state}`}>
            <span className="roster__output-label">
              {entry.state === "bound"
                ? <>{`${entry.outputId} · Bound to `}<FrameLink frameId={entry.frameId} wall={wall} /></>
                : label}
            </span>
            <FactLine label="Panel at enrollment" fact={panelAtEnrollment(reports.get(entry.outputId),
              snapshot?.inventory?.read_at, player.last_seen)} />
            {interruption !== null && (
              <FactLine label="Interruption" fact={interruption.fact} suffix={interruption.suffix} />
            )}
            {entry.frameId !== null && <ReadinessNotice snapshot={snapshot} frameId={entry.frameId} />}
            {identify.absent !== true && (
              <span className="roster__identify">
                <button
                  type="button"
                  disabled={!identify.offer || busy !== null}
                  aria-describedby={identify.offer ? undefined : reasonId}
                  onClick={() => doIdentify(entry.outputId)}
                >
                  Identify Panel<span className="visually-hidden">{` ${entry.outputId}`}</span>
                </button>
                {!identify.offer && (
                  <span id={reasonId} className="roster__note">{identify.reason}</span>
                )}
              </span>
            )}
            {entry.state === "free" &&
              (unbound.length === 0 ? (
                <span className="roster__empty">{NO_FRAMES}</span>
              ) : (
                <span className="roster__bind">
                  <select
                    aria-label={`Frame for ${label}`}
                    value={chosen?.frameId ?? ""}
                    onChange={(event) => pick(entry.outputId, event.target.value)}
                  >
                    <option value="">Bind to a frame…</option>
                    {unbound.map((frame) => (
                      <option key={frame.id} value={frame.id}>{frame.id}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    disabled={chosen === undefined || busy !== null}
                    onClick={() => doBind(entry.outputId)}
                  >
                    Bind<span className="visually-hidden">{` ${entry.outputId}`}</span>
                  </button>
                </span>
              ))}
            {message !== undefined && (
              <p className={message.kind === "alert" ? "roster__alert" : "roster__notice"} role={message.kind}>
                {message.text}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * One Pi's Software and screens page (`#/players/<device-id>`; console by domain § Fleet): the
 * Pi header (linking to the Pi's Hardware page, which holds Reboot, Health, its Node API link
 * and Retire), its Enrollment, its software layers bottom up (L1 to L2), its Outputs, its boot,
 * its app (Stage app and its operations) and Unbind all. It keeps what belongs to Software and
 * Screens until their own pages land. Each section shows its own read time and sits behind its
 * own error boundary. Node records are read only on a Pi's pages, for that box only
 * (nodeRead.js), never for a retired box and never while the shell's node control is not on;
 * with node control off the node sections give way to one "not shown" line (nodeControl.js).
 * Writes stay with their aggregate: Bind goes to the Frame, Stage app to the fleet.
 *
 * @param {{deviceId: string, snapshot: object, bootFacts: object|null,
 *          wall: import("./wallState.js").WallMemory}} props
 */
export function PlayerPage({ deviceId, snapshot, bootFacts, wall }) {
  const row = playersByDevice(snapshot, bootFacts).find((candidate) => candidate.deviceId === deviceId)
    ?? null;
  const retired = row?.standing === "retired";
  const control = useNodeControlValue();
  const node = useNodeDevice(deviceId, { skip: row === null || retired || !nodeReadsAllowed(control) });
  const nameRef = useRef(/** @type {HTMLHeadingElement|null} */ (null));
  const focusName = () => nameRef.current?.focus();
  const { open: openDialog, setStatus, confirmation } = useConfirm(focusName, focusName);

  if (row === null) {
    return (
      <div className="player">
        <p className="page__empty">
          {`${routeIdName("Pi", deviceId, { start: true })} is not known to Central.`}
          {bootFacts?.loaded ? "" : " Boot records are not read yet."}
        </p>
        <p><a href={formatRoute({ section: "hardware" })}>All Pis</a></p>
      </div>
    );
  }
  const player = row.player;
  return (
    <div className="player">
      <div className="player__header">
        <PiHeader row={row} on="software" headingRef={nameRef}>
          {player !== null && (
            <FactLine label="Enrollment" fact={enrolledFact(player, snapshot?.inventory?.read_at)} />
          )}
          {bootFacts?.unavailable && <p className="roster__note">{`${BOOT_FACTS_UNAVAILABLE}; serials may be out of date.`}</p>}
          {row.frames.length > 0 && (
            <p className="player__frames">
              {"Bound Frames: "}
              {row.frames.map((entry, index) => (
                <React.Fragment key={entry.frameId}>
                  {index > 0 && ", "}
                  <FrameLink frameId={entry.frameId} wall={wall} />
                </React.Fragment>
              ))}
            </p>
          )}
          <details className="player__details">
            <summary>Identifiers</summary>
            <ul>
              <li>{`Device ${row.deviceId}`}</li>
              {player !== null && <li>{`Registry Player ${player.id} · authority epoch ${player.authority_epoch}`}</li>}
            </ul>
          </details>
        </PiHeader>
      </div>

      <NodeRecords>
        <SectionBoundary title="Layers" resetKey={node.readAt}>
          <LayersSection node={node} snapshot={snapshot} playerId={player?.id ?? null} retired={retired} />
        </SectionBoundary>
      </NodeRecords>

      <SectionBoundary title="Outputs" resetKey={snapshot?.at}>
        {player === null ? (
          <p className="roster__empty">Not enrolled: the Player app has reported no Outputs.</p>
        ) : (
          <OutputsSection snapshot={snapshot} bootFacts={bootFacts} row={row} wall={wall} setStatus={setStatus} />
        )}
      </SectionBoundary>

      <NodeRecords quiet>
        <SectionBoundary title="Boot" resetKey={node.readAt}>
          <BootSection node={node} retired={retired} />
        </SectionBoundary>

        {!retired && (
          <SectionBoundary title="App" resetKey={node.operations?.read_at}>
            <StageApp key={deviceId} deviceId={deviceId} name={row.name} node={node} snapshot={snapshot}
              player={player} />
            <AppOperationsSection node={node} />
            <QualifiedFallback key={deviceId} deviceId={deviceId} node={node} snapshot={snapshot}
              playerId={player?.id ?? null} />
          </SectionBoundary>
        )}
      </NodeRecords>

      {player !== null && row.standing === "bound" && (
        <SectionBoundary title="Danger zone">
          <button
            type="button"
            className="roster__action"
            onClick={(event) => openDialog(event, unbindAllRequest(snapshot, bootFacts, player.id))}
          >
            Unbind all outputs<span className="visually-hidden">{` of ${player.id}`}</span>
          </button>
        </SectionBoundary>
      )}
      {confirmation("roster__status-line")}
    </div>
  );
}
