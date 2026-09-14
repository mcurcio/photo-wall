import React from "react";

import { apiWrite } from "./apiWrite.js";
import { useMutate } from "./useMutate.js";

/**
 * Retire a Player (bead G1 — SR-retire): POST /v1/operator/players/{id}/retire.
 *
 * The route takes NO body (central/app.py `retire`). On success the store sets
 * the Player's `retired_at`, bumps its `authority_epoch`, and drops its bindings
 * (central/registry.py:290, decision 0006: a Player is a disposable box). After
 * the caller's `useMutate` refreshes Plane A the Player moves from the Pending to
 * the Retired rail, and — because the Binding facet's bindable-output set is the
 * Outputs of pending Players (`is_bound === false && retired_at == null`) — the
 * retired Player's Output(s) drop out of the bind choices with no extra wiring.
 *
 * @param {string} playerId
 * @returns {Promise<{ok: boolean, status: number, error: string|null, data: any}>}
 */
export async function retirePlayer(playerId) {
  return apiWrite(`/v1/operator/players/${playerId}/retire`, { method: "POST" });
}

/**
 * Equipment rails (Bead 9) — the onboarding surface for new and retired Players.
 *
 * Two rails, each driven straight from the snapshot's PlayerInventory rows:
 *  - **Pending**: `is_bound === false && retired_at == null` — freshly enrolled
 *    or replacement Pis awaiting an operator bind (design J1). This is the
 *    same predicate the store uses for its pending queue
 *    (central/registry.py, test_registry.py:80).
 *  - **Retired**: `retired_at` set — equipment withdrawn from service, kept
 *    visible so the operator can see what was removed.
 *
 * Each entry is a selectable button keyed by the Player's identity (never an
 * invented label). Selection is reported to the caller via `onSelect(playerId)`.
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
  const pending = players.filter(
    (player) => player.is_bound === false && player.retired_at == null,
  );
  const retired = players.filter((player) => player.retired_at != null);

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
