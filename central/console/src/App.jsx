import React, { useMemo, useState } from "react";

import "./index.css";
import { Plan } from "./Plan.jsx";
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
              onSelect={setSelection}
            />
            <UnplacedTray snapshot={snapshot} onSelect={setSelection} />
          </>
        )}
      </main>
    </div>
  );
}
