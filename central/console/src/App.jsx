import React, { useEffect, useMemo, useRef, useState } from "react";

import "./index.css";
import { EquipmentRail } from "./EquipmentRail.jsx";
import { Inspector } from "./Inspector.jsx";
import { Plan } from "./Plan.jsx";
import { detectRecovery } from "./recovery.js";
import { UnplacedTray } from "./UnplacedTray.jsx";
import { setToken, useSnapshot } from "./useSnapshot.js";

/**
 * The console app shell.
 *
 * Bead 0 built the empty frame + Plane A wiring. Bead 1 adds:
 *  - a minimal shared "Token" control (design §2): an "Operator token" input and
 *    a "Connect" button that set the in-memory token and trigger one Plane A
 *    refresh. The token is never persisted (useSnapshot holds it in memory only).
 *  - a Surface filter that groups frames by `surface_id` and switches plans,
 *    defaulting to the first Surface present.
 *  - the read-only per-Surface SVG Plan and the Unplaced tray.
 */
export default function App() {
  const { snapshot, refresh } = useSnapshot();
  const [tokenInput, setTokenInput] = useState("");
  const [surfaceId, setSurfaceId] = useState(/** @type {string|null} */ (null));
  const [selection, setSelection] = useState(/** @type {string|null} */ (null));
  // Which Inspector facet is open (Plane B, component-local). Defaults to
  // "commissioning" and resets to it each time a new Frame is selected.
  const [facet, setFacet] = useState(/** @type {string} */ ("commissioning"));
  // The pending/retired Player last selected in the Equipment rail (Plane B).
  const [selectedPlayer, setSelectedPlayer] = useState(/** @type {string|null} */ (null));

  // Auto-recovery banner (design J1, §1a D-a). Recovery is INFERRED by diffing
  // the CURRENT Plane A snapshot against the PRIOR one, so App retains the prior
  // snapshot itself in a ref — useSnapshot's frozen {snapshot, refresh} shape is
  // untouched. The banner surfaces "a known Pi returned already bound"; it is
  // suppressed on the true first run (no prior snapshot) by detectRecovery.
  const prevSnapshotRef = useRef(/** @type {object|null} */ (null));
  const [recovered, setRecovered] = useState(/** @type {string[]} */ ([]));
  useEffect(() => {
    if (snapshot == null) {
      return;
    }
    const returned = detectRecovery(prevSnapshotRef.current, snapshot);
    if (returned.length > 0) {
      setRecovered(returned);
    }
    prevSnapshotRef.current = snapshot;
  }, [snapshot]);

  // Selecting a Frame (on the plan or in the tray) opens its Inspector on the
  // default facet; the facet contract's default is "commissioning".
  const selectFrame = (frameId) => {
    setSelection(frameId);
    setFacet("commissioning");
  };

  // Surfaces present in the snapshot, sorted for a deterministic default.
  const surfaces = useMemo(() => {
    const frames = snapshot?.inventory?.frames ?? [];
    return [...new Set(frames.map((frame) => frame.surface_id))].sort();
  }, [snapshot]);

  // Default to the first Surface present; fall back if the chosen one vanished.
  const activeSurface =
    surfaceId !== null && surfaces.includes(surfaceId) ? surfaceId : surfaces[0] ?? null;

  const connect = (event) => {
    event.preventDefault();
    // In-memory only, mirroring the legacy flat page — never persisted.
    setToken(tokenInput);
    // Trigger one Plane A load with the freshly-set token; a failure leaves the
    // empty state in place (Bead 18 adds richer error surfacing).
    refresh().catch(() => {});
  };

  return (
    <div className="console">
      <header className="console__header">
        <h1 className="console__title">Operator Console</h1>
        <div
          className="console__mode-toggle"
          role="group"
          aria-label="Console mode"
        >
          {/* Mode toggle mounts here in Bead 12. */}
        </div>
      </header>

      <form className="console__token" onSubmit={connect}>
        <label className="console__token-field">
          Operator token
          <input
            type="password"
            name="operator-token"
            autoComplete="off"
            value={tokenInput}
            onChange={(event) => setTokenInput(event.target.value)}
          />
        </label>
        <button type="submit">Connect</button>
      </form>

      <main className="console__body">
        {snapshot === null ? (
          <p>Console ready.</p>
        ) : (
          <>
            {recovered.length > 0 && (
              <div className="console__recovery" role="status">
                <p className="console__recovery-text">
                  Recovered — already bound (serial match, not identity):{" "}
                  {recovered.join(", ")}
                </p>
                <button type="button" onClick={() => setRecovered([])}>
                  Dismiss
                </button>
              </div>
            )}
            <EquipmentRail snapshot={snapshot} onSelect={setSelectedPlayer} />
            {selectedPlayer !== null && (
              <p className="console__selected-player">
                Pending player selected: {selectedPlayer}
              </p>
            )}
            <div className="console__surface-filter">
              <label className="console__surface-field">
                Surface
                <select
                  aria-label="Surface"
                  value={activeSurface ?? ""}
                  onChange={(event) => {
                    setSurfaceId(event.target.value);
                    setSelection(null);
                  }}
                >
                  {surfaces.map((surface) => (
                    <option key={surface} value={surface}>
                      {surface}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <Plan
              snapshot={snapshot}
              surfaceId={activeSurface}
              selection={selection}
              onSelect={selectFrame}
            />
            <UnplacedTray snapshot={snapshot} onSelect={selectFrame} />
            {selection !== null && (
              <Inspector
                snapshot={snapshot}
                frameId={selection}
                facet={facet}
                onFacet={setFacet}
              />
            )}
          </>
        )}
      </main>
    </div>
  );
}
