import React, { useRef, useState } from "react";

import { ConfirmAction, retireRequest } from "./ConfirmAction.jsx";
import {
  bootOutcomeLabel,
  playerLiveness,
  playerSerial,
  playerStanding,
} from "./health.js";

/**
 * Equipment rails (Bead 9) — the onboarding surface for new and retired Players.
 *
 * Two rails, each driven straight from the snapshot's PlayerInventory rows:
 *  - **Pending**: health.js `playerStanding` "pending" — no Output bound and
 *    not retired, the store's pending queue: freshly enrolled or replacement
 *    Pis awaiting an operator bind (design J1).
 *  - **Retired**: standing "retired" — equipment withdrawn from service, kept
 *    visible so the operator can see what was removed.
 *
 * Each entry is a selectable button keyed by the Player's identity (never an
 * invented label). Selection is reported to the caller via `onSelect(playerId)`.
 * A Pending entry also states when Central last heard from that Player
 * (health.js `playerLiveness`), so a Pi that enrolled and then went quiet is
 * visible before it is bound.
 *
 * Each Pending entry also carries a deliberate **Retire player** action (bead
 * G1): the legacy operator page had an explicit "Retire a Player" control, and
 * the Bead 17 cutover precondition requires content parity, so the console must
 * re-host it (design R1 note: retire is a legitimate Player lifecycle action).
 * Retire is permanent, so it opens the one confirmation dialog (ConfirmAction,
 * slice 2 §7) with the typed handle; the dialog lives at the rail's top level,
 * keyed by Player. It writes through `useMutate()` so one new Plane A snapshot
 * refreshes the rails AND the bindable-output set together; once done, focus
 * moves to the Retired rail's heading.
 *
 * A Pending entry also names the device's serial and netboot outcome from the
 * App-level boot facts (bootFacts.js), joined on `device_id`.
 *
 * @param {{snapshot: object|null, bootFacts?: object|null,
 *          onSelect: (playerId: string) => void}} props
 */
export function EquipmentRail({ snapshot, bootFacts = null, onSelect }) {
  const [confirm, setConfirm] = useState(/** @type {object|null} */ (null));
  const [status, setStatus] = useState(/** @type {string|null} */ (null));
  const openerRef = useRef(/** @type {HTMLElement|null} */ (null));
  const retiredHeadingRef = useRef(/** @type {HTMLHeadingElement|null} */ (null));
  const retire = (event, playerId) => {
    openerRef.current = event.currentTarget;
    setStatus(null);
    setConfirm(retireRequest(snapshot, bootFacts, playerId));
  };
  const onConfirmClosed = (result) => {
    setConfirm(null);
    if (result?.state === "done") {
      setStatus(result.message);
      retiredHeadingRef.current?.focus();
    } else if (openerRef.current?.isConnected) {
      openerRef.current.focus();
    }
  };
  const players = snapshot?.inventory?.players ?? [];
  const inState = (state) =>
    players.filter((player) => playerStanding(snapshot, player.id)?.state === state);
  const pending = inState("pending");
  const retired = inState("retired");

  return (
    <div className="equipment-rail">
      <div
        className="rail rail--pending"
        role="group"
        aria-label="Pending players"
      >
        <h2 className="rail__title">Pending</h2>
        {pending.length === 0 ? (
          <p className="rail__empty">No pending players.</p>
        ) : (
          <ul className="rail__list">
            {pending.map((player) => (
              <li key={player.id} className="rail__item">
                <button
                  type="button"
                  className="rail__entry"
                  onClick={() => onSelect(player.id)}
                >
                  {player.id}
                </button>
                <span className="rail__liveness">
                  {playerLiveness(snapshot, player.id)?.label}
                </span>
                <span className="rail__boot">
                  {[
                    playerSerial(snapshot, bootFacts, player.id),
                    bootOutcomeLabel(bootFacts, player.device_id),
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
                <button
                  type="button"
                  className="rail__retire"
                  onClick={(event) => retire(event, player.id)}
                >
                  Retire player {player.id}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div
        className="rail rail--retired"
        role="group"
        aria-label="Retired players"
      >
        <h2 ref={retiredHeadingRef} className="rail__title" tabIndex={-1}>
          Retired
        </h2>
        {retired.length === 0 ? (
          <p className="rail__empty">No retired players.</p>
        ) : (
          <ul className="rail__list">
            {retired.map((player) => (
              <li key={player.id} className="rail__item">
                <button
                  type="button"
                  className="rail__entry"
                  onClick={() => onSelect(player.id)}
                >
                  {player.id}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <p className="rail__status-line" role="status">
        {status}
      </p>
      {confirm !== null && (
        <ConfirmAction key={confirm.key} request={confirm} onClose={onConfirmClosed} />
      )}
    </div>
  );
}
