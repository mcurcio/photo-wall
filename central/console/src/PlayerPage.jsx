import React, { useEffect, useId, useRef, useState } from "react";

import { retireRequest, unbindAllRequest, useConfirm } from "./ConfirmAction.jsx";
import { bind, identifyOutput } from "./equipmentApi.js";
import { FactLine } from "./FactLine.jsx";
import { clock, fact, words } from "./facts.js";
import { useFleetFacts } from "./fleetApi.js";
import { BOOT_FACTS_UNAVAILABLE, bootOutcomeLabel, isBound, outputLabel, outputStates } from "./health.js";
import { currentSessionBoot, layerEvidence, useNodeDevice } from "./nodeRead.js";
import { AppOperationsSection, RebootSection } from "./PlayerCommands.jsx";
import { playersByDevice } from "./players.js";
import { ReadinessNotice } from "./ReadinessNotice.jsx";
import { formatRoute, isPlainClick, routeIdName } from "./routes.js";
import { SectionBoundary } from "./SectionBoundary.jsx";
import { useMutate } from "./useMutate.js";
import { V1PlayerSection, v1OfferFact } from "./V1Offers.jsx";

const NO_FRAMES = "No unbound frames. Draw one on the plan first.";
const RETIRED_NOT_READ = "Not read: Player retired";


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

/** The five layer rows (§9 Layers) and the closing Panel line. */
function LayersSection({ node, snapshot, playerId, retired }) {
  const rows = layerEvidence({ nodeDevice: node, snapshot, playerId });
  const shown = retired ? rows.filter((row) => row.key === "app") : rows;
  return (
    <>
      {retired ? (
        <p className="player__read-time">{`${RETIRED_NOT_READ} (Host Management, App Manager, App Effect Broker, Display Host)`}</p>
      ) : (
        <ReadTime what="Node read" at={node.readAt} failed={node.error !== null && node.read !== null} />
      )}
      <ReadTime what="Player app read" at={snapshot?.inventory?.read_at} failed={false} />
      <ul className="player__layers" aria-label="Layers">
        {shown.map((row) => (
          <li key={row.key} className="player__layer">
            <div role="group" aria-label={row.layer}>
              <h4 className="player__layer-title">{`${row.layer} (${row.level})`}</h4>
              {row.facts.map((entry) => <FactLine key={entry.label} label={entry.label} fact={entry.fact} />)}
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

/** The Boot section: the current node session's boot, then one record per boot path seen. */
function BootSection({ node, deviceId, bootFacts, fleet, retired }) {
  if (retired) {
    return <p className="player__read-time">{RETIRED_NOT_READ}</p>;
  }
  const current = currentSessionBoot(node);
  const records = [];
  // Node boot offer (/v2/node/boot-offers): the latest the device read holds.
  if (node.enabled === false || node.read == null) {
    records.push(["Node boot offer", current.none?.kind === "unknown" ? current.none : fact({ kind: "unknown", why: "not read yet" })]);
  } else {
    const [latest] = node.read.boot_claims ?? [];
    if (latest !== undefined) {
      records.push(["Node boot offer", fact({
        kind: "set",
        value: latest.offer_refusal == null
          ? `Issued for boot ${latest.kernel_boot_id}, not proof the Player booted`
          : `Refused for boot ${latest.kernel_boot_id}: ${words(latest.offer_refusal)}`,
        receivedAt: latest.first_received_at, readAt: node.read.read_at,
      })]);
    }
  }
  // V1 boot offer (/v1/netboot/offers): the fleet read's latest offer.
  const v1Offer = v1OfferFact(fleet, deviceId);
  if (v1Offer !== null) records.push(["V1 boot offer", v1Offer]);
  // Netboot base without an offer (/v1/netboot/base): the content catalog's record.
  const base = bootOutcomeLabel(bootFacts, deviceId);
  if (base === null || base === BOOT_FACTS_UNAVAILABLE) {
    records.push(["Netboot base without an offer", fact({ kind: "unknown",
      why: base === null ? "boot records not read yet" : "boot records unavailable" })]);
  } else if (base !== "No netboot record") {
    records.push(["Netboot base without an offer", fact({ kind: "set", value: base })]);
  }
  return (
    <>
      <FactLine label="Current node session's boot" fact={current.fact ?? current.none} />
      {records.length === 0 ? (
        <p className="roster__empty">No boot offer or netboot base is recorded for this box.</p>
      ) : (
        records.map(([label, value]) => <FactLine key={label} label={label} fact={value} />)
      )}
      <p className="roster__note">
        A boot path is chosen per boot, not stored per Player. A later boot does not show what caused it.
      </p>
    </>
  );
}

/**
 * The Outputs section: each Output's Binding (a link to the Frame), the shared bind write
 * for a free Output (design rule 1: the same `bind` and Frame-generation fence the Binding
 * facet uses), Identify on an unbound Player, and the Panel facts from the last app start.
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

  // After a bind: open the Frame once the refresh has rendered (to commission it).
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
        const noDisplay = row.standing === "unbound" && entry.state === "no-display";
        const canIdentify = row.standing === "unbound" && entry.state === "free";
        const reasonId = `${ids}-identify-${index}`;
        const report = reports.get(entry.outputId);
        const panel = report?.connected === true
          ? `a ${report.width_px}×${report.height_px} Panel on ${entry.outputId} at its last start`
          : report?.connected === false ? `no Panel on ${entry.outputId} at its last start` : null;
        return (
          <li key={entry.outputId} className={`roster__output roster__output--${entry.state}`}>
            <span className="roster__output-label">
              {entry.state === "bound"
                ? <>{`${entry.outputId} · Bound to `}<FrameLink frameId={entry.frameId} wall={wall} /></>
                : label}
            </span>
            <FactLine label="Panel at last start (stale)" fact={fact({ kind: "reported", source: "Player app",
              receipt: "first", value: panel, receivedAt: player.last_seen,
              readAt: snapshot?.inventory?.read_at, field: "last_seen" })} />
            {entry.frameId !== null && <ReadinessNotice snapshot={snapshot} frameId={entry.frameId} />}
            {(canIdentify || noDisplay) && (
              <span className="roster__identify">
                <button
                  type="button"
                  disabled={noDisplay || busy !== null}
                  aria-describedby={noDisplay ? reasonId : undefined}
                  onClick={() => doIdentify(entry.outputId)}
                >
                  Identify display<span className="visually-hidden">{` ${entry.outputId}`}</span>
                </button>
                {noDisplay && (
                  <span id={reasonId} className="roster__note">
                    Connect a Panel and restart the Player, then press Refresh.
                  </span>
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
 * One Player's home (`#/players/<device-id>`; console DDD §9, Q1 = A): the box's
 * identity and standing, its node layers bottom up, its Outputs, its boot, its reboot, its
 * app operations, its V1 boot offers and its danger zone. Each section shows its own read time and sits behind its own error
 * boundary. Node records are read only here, for this box only (nodeRead.js), and never
 * for a retired box. Writes stay with their aggregate: Bind goes to the Frame, Retire to
 * the Registry, Reboot to the fleet.
 *
 * @param {{deviceId: string, snapshot: object, bootFacts: object|null,
 *          wall: import("./wallState.js").WallMemory}} props
 */
export function PlayerPage({ deviceId, snapshot, bootFacts, wall }) {
  const row = playersByDevice(snapshot, bootFacts).find((candidate) => candidate.deviceId === deviceId)
    ?? null;
  const retired = row?.standing === "retired";
  const node = useNodeDevice(deviceId, { skip: row === null || retired });
  const fleet = useFleetFacts(snapshot);
  const nameRef = useRef(/** @type {HTMLHeadingElement|null} */ (null));
  const focusName = () => nameRef.current?.focus();
  const { open: openDialog, setStatus, confirmation } = useConfirm(focusName, focusName);

  if (row === null) {
    return (
      <div className="player">
        <p className="page__empty">
          {`${routeIdName("Player", deviceId, { start: true })} is not known to Central.`}
          {bootFacts?.loaded ? "" : " Boot records are not read yet."}
        </p>
        <p><a href={formatRoute({ section: "players" })}>All Players</a></p>
      </div>
    );
  }
  const player = row.player;
  return (
    <div className="player">
      <header className="player__header">
        <p><a href={formatRoute({ section: "players" })}>All Players</a></p>
        <h2 ref={nameRef} className="player__name" tabIndex={-1}>{row.name}</h2>
        <FactLine label="Standing" fact={fact({ kind: "set", value: row.standingLabel })} />
        {row.serial !== null && (
          <FactLine label="Serial" fact={fact({ kind: "claimed", value: `Serial ${row.serial}`, source: "the box" })} />
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
      </header>

      <SectionBoundary title="Layers" resetKey={node.readAt}>
        <LayersSection node={node} snapshot={snapshot} playerId={player?.id ?? null} retired={retired} />
      </SectionBoundary>

      <SectionBoundary title="Outputs" resetKey={snapshot?.at}>
        {player === null ? (
          <p className="roster__empty">Not enrolled: the Player app has reported no Outputs.</p>
        ) : (
          <OutputsSection snapshot={snapshot} bootFacts={bootFacts} row={row} wall={wall} setStatus={setStatus} />
        )}
      </SectionBoundary>

      <SectionBoundary title="Boot" resetKey={node.readAt}>
        <BootSection node={node} deviceId={deviceId} bootFacts={bootFacts} fleet={fleet} retired={retired} />
      </SectionBoundary>

      {!retired && (
        <SectionBoundary title="Reboot" resetKey={node.readAt}>
          <RebootSection key={deviceId} deviceId={deviceId} name={row.name} node={node} snapshot={snapshot}
            playerId={player?.id ?? null} />
        </SectionBoundary>
      )}

      {!retired && (
        <SectionBoundary title="App" resetKey={node.operations?.read_at}>
          <AppOperationsSection node={node} />
        </SectionBoundary>
      )}

      {!retired && (
        <SectionBoundary title="V1 boot offers" resetKey={fleet.data?.read_at}>
          <V1PlayerSection key={deviceId} deviceId={deviceId} fleet={fleet} />
        </SectionBoundary>
      )}

      {player !== null && (row.standing === "unbound" || row.standing === "bound") && (
        <SectionBoundary title="Danger zone">
          {row.standing === "unbound" ? (
            <button
              type="button"
              className="roster__action"
              onClick={(event) => openDialog(event, retireRequest(snapshot, bootFacts, player.id))}
            >
              Retire player<span className="visually-hidden">{` ${player.id}`}</span>
            </button>
          ) : (
            <button
              type="button"
              className="roster__action"
              onClick={(event) => openDialog(event, unbindAllRequest(snapshot, bootFacts, player.id))}
            >
              Unbind all outputs<span className="visually-hidden">{` of ${player.id}`}</span>
            </button>
          )}
        </SectionBoundary>
      )}
      {confirmation("roster__status-line")}
    </div>
  );
}
