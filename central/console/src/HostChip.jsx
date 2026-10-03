import React from "react";

import { hostChip, judgeHost } from "./hostHealth.js";
import { playersByDevice } from "./players.js";
import { formatRoute } from "./routes.js";

/**
 * The Frame's host chip on its Status facet (console DDD §61-§62): ONE link chip naming the
 * bound Player's worst host item from the one classifier (hostHealth.js `classifyHost`),
 * "pi-07 · throttled now", "pi-07 · Host Management silent 3 min", else its receipt, "pi-07 ·
 * Host Management last reported 3 s ago". It links to the Player page, whose Health section
 * carries the detail. Wall side only (R4): Inspector.jsx hands it to the Status facet, so no
 * Show module imports it.
 *
 * Nothing renders for an unbound Frame, nor while the shell's fleet host read is skipped (node
 * control off: its banner names that) or has not loaded. A failed read says so rather than
 * judging the last good values: "pi-07 · host health not read".
 *
 * @param {{snapshot: object|null, bootFacts?: object|null, frameId: string,
 *          hosts: import("./fleetHosts.js").FleetHosts|null}} props
 */
export function HostChip({ snapshot, bootFacts = null, frameId, hosts }) {
  if (hosts === null || (hosts.read === null && !hosts.failed)) return null;
  const player = playersByDevice(snapshot, bootFacts).find((row) =>
    row.frames.some((entry) => entry.frameId === frameId));
  if (player === undefined) return null;
  const chip = hosts.failed
    ? { text: `${player.name} · host health not read`, severity: "unknown" }
    : hostChip(player.name, judgeHost(hosts, player.deviceId).health);
  return (
    <p className="facet__host">
      <a
        className={`card__chip host-chip health--${chip.severity}`}
        href={formatRoute({ section: "players", id: player.deviceId })}
      >
        {chip.text}
      </a>
    </p>
  );
}
