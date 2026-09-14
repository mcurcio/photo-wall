import React from "react";

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
 * @param {{snapshot: object|null, onSelect: (playerId: string) => void}} props
 */
export function EquipmentRail({ snapshot, onSelect }) {
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
