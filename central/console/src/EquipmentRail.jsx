import React from "react";

import { retirePlayer } from "./equipmentApi.js";
import { playerLiveness, playerStanding } from "./health.js";
import { useMutate } from "./useMutate.js";

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
 * It writes through the shared `useMutate()` hook (primitive #7) so one new
 * Plane A snapshot refreshes the rails AND the bindable-output set together.
 *
 * @param {{snapshot: object|null, onSelect: (playerId: string) => void}} props
 */
export function EquipmentRail({ snapshot, onSelect }) {
  const mutate = useMutate();
  const retire = (playerId) => mutate(() => retirePlayer(playerId));
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
                <button
                  type="button"
                  className="rail__retire"
                  onClick={() => retire(player.id)}
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
        <h2 className="rail__title">Retired</h2>
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
    </div>
  );
}
