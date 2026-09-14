import { useState } from "react";

/**
 * @typedef {"wall"|"showrunner"} Mode
 */

/**
 * Top-level console mode, held in Plane B (component-local, Bead 12).
 *
 * The mode is organization, NOT permission (design §2): it chooses which layer
 * of the one console is on screen — the Wall-First canvas or the Showrunner —
 * and defaults to "wall". Because it lives in ordinary component state (never in
 * the Plane A snapshot), a snapshot refresh replaces the fetched inventory alone
 * and CANNOT reset the operator's chosen mode — the same two-plane discipline
 * useSnapshot/useDraft enforce.
 *
 * @returns {{mode: Mode, setMode: (mode: Mode) => void}}
 */
export function useMode() {
  const [mode, setMode] = useState(/** @type {Mode} */ ("wall"));
  return { mode, setMode };
}
