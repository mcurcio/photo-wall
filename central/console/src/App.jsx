import React from "react";

import "./index.css";
import { useSnapshot } from "./useSnapshot.js";

/**
 * The console app shell (Bead 0 foundation).
 *
 * It renders the empty console frame with a mode-toggle mount point and reads
 * Plane A via useSnapshot(). The shell renders with no live data (empty state);
 * feature beads mount their regions inside `.console__body` and the mode toggle
 * (Wall / Showrunner) lands in the mount point in Bead 12.
 */
export default function App() {
  // Consume Plane A so the foundation wiring is exercised. The shell renders
  // regardless of whether a snapshot has loaded yet (empty state is fine).
  const { snapshot } = useSnapshot();

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
      <main className="console__body">
        {snapshot === null ? (
          <p>Console ready.</p>
        ) : (
          <p>Snapshot loaded.</p>
        )}
      </main>
    </div>
  );
}
