import React, { useEffect, useId, useRef, useState } from "react";

import {
  ConfirmAction,
  retireRequest,
  unbindAllRequest,
} from "./ConfirmAction.jsx";
import { bind } from "./equipmentApi.js";
import {
  BOOT_FACTS_UNAVAILABLE,
  bootOutcomeLabel,
  isBound,
  outputLabel,
  outputStates,
  playerLiveness,
  playerSerial,
  playersInOrder,
  playerStanding,
} from "./health.js";
import { useMutate } from "./useMutate.js";

// The three groups, in order: standing -> heading. Retired starts collapsed.
const GROUPS = [
  { state: "pending", title: "Pending players", empty: "No pending players." },
  { state: "in-service", title: "Bound players", empty: "No bound players." },
  { state: "retired", title: "Retired players", empty: "No retired players." },
];

const NO_PLAYERS = "No Players yet. Power on one Pi on this network; it appears under Pending.";
const NO_FRAMES = "No unbound frames. Draw one on the plan first.";

/**
 * Equipment roster (slice 2 §5): every Player, grouped by its standing
 * (health.js `playerStanding`) — Pending and Bound open by default, Retired
 * collapsed — ordered by registration, each Output by id. It replaces the
 * Equipment rail.
 *
 * Each card is a disclosure button whose accessible name is EXACTLY the Player
 * id; "last heard" is tied to it by `aria-describedby`. Below it: the serial and
 * netboot outcome from the App-level boot facts (bootFacts.js), and each Output
 * in the one Output wording (health.js `outputLabel`).
 *
 * Actions (R1: every binding write still targets a Frame):
 *  - a pending Player offers Retire (typed handle, ConfirmAction);
 *  - an in-service Player offers Unbind all outputs (ConfirmAction's sequence);
 *    it offers no Retire — to replace a Pi, unbind it and bind the new one;
 *  - a free Output offers "Bind to a frame…": a select of unbound Frames and a
 *    Bind button. Choosing a Frame captures its generation, which the bind
 *    carries (the live snapshot is never read at click time). Once the write
 *    and its refresh land, `onNavigate` opens that Frame (App `navigateToFrame`).
 *
 * Plane B (group and card disclosure, the Output-first picks, the open dialog)
 * lives here and survives polls. The ONE dialog sits at the roster's top
 * level, keyed by target and never inside a card, so a poll that regroups a
 * Player cannot unmount it.
 *
 * @param {{snapshot: object, bootFacts?: object|null,
 *          onNavigate?: ((frameId: string) => void)|null}} props
 */
export function EquipmentRoster({ snapshot, bootFacts = null, onNavigate = null }) {
  const mutate = useMutate();
  const ids = useId();
  const [open, setOpen] = useState({ pending: true, "in-service": true, retired: false });
  const [collapsed, setCollapsed] = useState(() => new Set());
  // Output-first picks: "player/output" -> {frameId, generation} captured on selection.
  const [picks, setPicks] = useState(() => new Map());
  const [messages, setMessages] = useState(() => new Map());
  const [busy, setBusy] = useState(null);
  const [confirm, setConfirm] = useState(/** @type {object|null} */ (null));
  const [status, setStatus] = useState(/** @type {string|null} */ (null));
  const [focusRetired, setFocusRetired] = useState(false);
  const [navigateTo, setNavigateTo] = useState(/** @type {string|null} */ (null));
  const openerRef = useRef(/** @type {HTMLElement|null} */ (null));
  const headingRefs = useRef(/** @type {Record<string, HTMLElement|null>} */ ({}));

  const frames = snapshot?.inventory?.frames ?? [];
  const unbound = frames
    .filter((frame) => !isBound(frame))
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const players = playersInOrder(snapshot);

  // A pick whose Frame is no longer unbound is dropped by the next snapshot.
  useEffect(() => {
    const available = new Set(unbound.map((frame) => frame.id));
    if ([...picks.values()].some((pick) => !available.has(pick.frameId))) {
      setPicks(new Map([...picks].filter(([, pick]) => available.has(pick.frameId))));
    }
  }, [snapshot]);

  // After a retire: the Retired group is open and its heading takes focus.
  useEffect(() => {
    if (focusRetired) {
      headingRefs.current.retired?.focus();
      setFocusRetired(false);
    }
  }, [focusRetired]);

  // After an Output-first bind: open the Frame once the refresh has rendered.
  useEffect(() => {
    if (navigateTo !== null) {
      onNavigate?.(navigateTo);
      setNavigateTo(null);
    }
  }, [navigateTo]);

  const setKeyed = (setter, key, value) =>
    setter((current) => {
      const next = new Map(current);
      if (value === null) {
        next.delete(key);
      } else {
        next.set(key, value);
      }
      return next;
    });

  const pick = (key, frameId) => {
    setKeyed(setMessages, key, null);
    const frame = frames.find((candidate) => candidate.id === frameId);
    setKeyed(setPicks, key, frame ? { frameId, generation: frame.generation } : null);
  };

  const doBind = async (playerId, outputId) => {
    const key = `${playerId}/${outputId}`;
    const chosen = picks.get(key);
    if (chosen === undefined || busy !== null) {
      return;
    }
    setBusy(key);
    setKeyed(setMessages, key, null);
    const result = await mutate(() => bind(chosen.frameId, playerId, outputId, chosen.generation));
    setBusy(null);
    setKeyed(setPicks, key, null);
    if (result.outcome === "done") {
      setNavigateTo(chosen.frameId);
    } else {
      setKeyed(setMessages, key, result.message);
    }
  };

  const openDialog = (event, request) => {
    openerRef.current = event.currentTarget;
    setStatus(null);
    setConfirm(request);
  };

  const onConfirmClosed = (result) => {
    const retiring = confirm?.key.startsWith("retire:");
    setConfirm(null);
    if (result?.state === "done") {
      setStatus(result.message);
      if (retiring) {
        setOpen((current) => ({ ...current, retired: true }));
        setFocusRetired(true);
      }
    } else if (openerRef.current?.isConnected) {
      openerRef.current.focus();
    } else {
      headingRefs.current.pending?.focus();
    }
  };

  const toggleCard = (playerId) =>
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(playerId)) {
        next.delete(playerId);
      } else {
        next.add(playerId);
      }
      return next;
    });

  const renderOutput = (player, entry) => {
    const key = `${player.id}/${entry.outputId}`;
    const label = outputLabel(snapshot, bootFacts, player.id, entry.outputId);
    const chosen = picks.get(key);
    const message = messages.get(key);
    return (
      <li key={entry.outputId} className={`roster__output roster__output--${entry.state}`}>
        <span className="roster__output-label">{label}</span>
        {entry.state === "free" &&
          (unbound.length === 0 ? (
            <span className="roster__empty">{NO_FRAMES}</span>
          ) : (
            <span className="roster__bind">
              <select
                aria-label={`Frame for ${label}`}
                value={chosen?.frameId ?? ""}
                onChange={(event) => pick(key, event.target.value)}
              >
                <option value="">Bind to a frame…</option>
                {unbound.map((frame) => (
                  <option key={frame.id} value={frame.id}>
                    {frame.id}
                  </option>
                ))}
              </select>
              <button
                type="button"
                disabled={chosen === undefined || busy !== null}
                onClick={() => doBind(player.id, entry.outputId)}
              >
                Bind<span className="visually-hidden">{` ${entry.outputId}`}</span>
              </button>
            </span>
          ))}
        {message !== undefined && (
          <p className="roster__alert" role="alert">
            {message}
          </p>
        )}
      </li>
    );
  };

  const renderCard = (player, standing, index) => {
    const expanded = !collapsed.has(player.id);
    const detailsId = `${ids}-details-${index}`;
    const describedId = `${ids}-described-${index}`;
    const outputs = outputStates(snapshot, player.id);
    const liveness = standing.state === "retired" ? null : playerLiveness(snapshot, player.id);
    const serial = playerSerial(snapshot, bootFacts, player.id);
    const boot = bootOutcomeLabel(bootFacts, player.device_id);
    return (
      <li key={player.id} className="roster__card">
        <button
          type="button"
          className="roster__player"
          aria-expanded={expanded}
          aria-controls={detailsId}
          aria-describedby={describedId}
          onClick={() => toggleCard(player.id)}
        >
          {player.id}
        </button>
        <p id={describedId} className="roster__standing">
          {[standing.label, liveness?.label].filter(Boolean).join(" · ")}
        </p>
        {expanded && (
          <div id={detailsId} className="roster__details">
            <p className="roster__boot">
              {[serial === null ? null : `Serial ${serial}`, boot].filter(Boolean).join(" · ")}
            </p>
            {outputs.length === 0 ? (
              <p className="roster__empty">No outputs reported</p>
            ) : (
              <ul className="roster__outputs" aria-label={`Outputs of ${player.id}`}>
                {outputs.map((entry) => renderOutput(player, entry))}
              </ul>
            )}
            {standing.state === "pending" && (
              <button
                type="button"
                className="roster__action"
                onClick={(event) =>
                  openDialog(event, retireRequest(snapshot, bootFacts, player.id))
                }
              >
                Retire player<span className="visually-hidden">{` ${player.id}`}</span>
              </button>
            )}
            {standing.state === "in-service" && (
              <button
                type="button"
                className="roster__action"
                onClick={(event) =>
                  openDialog(event, unbindAllRequest(snapshot, bootFacts, player.id))
                }
              >
                Unbind all outputs
                <span className="visually-hidden">{` of ${player.id}`}</span>
              </button>
            )}
          </div>
        )}
      </li>
    );
  };

  return (
    <section className="roster" role="region" aria-label="Equipment">
      <h2 className="roster__title">Equipment</h2>
      {bootFacts?.unavailable && (
        <p className="roster__note">{`${BOOT_FACTS_UNAVAILABLE}; serials may be out of date.`}</p>
      )}
      {GROUPS.map((group) => {
        const members = players
          .map((player, index) => ({ player, index, standing: playerStanding(snapshot, player.id) }))
          .filter(({ standing }) => standing?.state === group.state);
        const isOpen = open[group.state];
        return (
          <section
            key={group.state}
            className="roster__group"
            role="group"
            aria-label={group.title}
          >
            <h3
              ref={(element) => {
                headingRefs.current[group.state === "in-service" ? "bound" : group.state] =
                  element;
              }}
              className="roster__group-title"
              tabIndex={-1}
            >
              <button
                type="button"
                className="roster__group-toggle"
                aria-expanded={isOpen}
                onClick={() => setOpen((current) => ({ ...current, [group.state]: !isOpen }))}
              >
                {`${group.title} (${members.length})`}
              </button>
            </h3>
            {isOpen &&
              (members.length === 0 ? (
                <p className="roster__empty">
                  {players.length === 0 && group.state === "pending" ? NO_PLAYERS : group.empty}
                </p>
              ) : (
                <ul className="roster__cards">
                  {members.map(({ player, index, standing }) => renderCard(player, standing, index))}
                </ul>
              ))}
          </section>
        );
      })}
      <p className="roster__status-line" role="status">
        {status}
      </p>
      {confirm !== null && (
        <ConfirmAction key={confirm.key} request={confirm} onClose={onConfirmClosed} />
      )}
    </section>
  );
}
