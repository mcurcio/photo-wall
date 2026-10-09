import { hostChip, judgeHost } from "../hostHealth.js";
import { OwnerLinks } from "../patterns/link-to-owner";
import { playersByDevice } from "../players.js";
import { formatRoute } from "../routes.js";
import type { HostsRead } from "./hosts-read";

export interface HostHealthLinkProps {
  snapshot: object | null;
  bootFacts?: object | null;
  frameId: string;
  hosts: HostsRead | null;
}

/**
 * A Frame's host link (console DDD §61-§62, design rule H1): ONE read-only link naming the bound
 * Pi's worst host item from the one classifier (hostHealth.js `classifyHost`), "pi-07 · throttled
 * now", "pi-07 · Host Management silent 3 min", else its receipt, "pi-07 · Host Management last
 * reported 3 s ago". It leads to the Pi's Hardware page, where host health is judged and
 * explained. Wall side only (R4): Inspector.jsx hands it to the Status facet.
 *
 * Nothing renders for an unbound Frame, nor while the shell's fleet host read is skipped (node
 * control off: its banner names that) or has not loaded. A failed read says so rather than
 * judging the last good values: "pi-07 · host health not read".
 */
export function HostHealthLink({ snapshot, bootFacts = null, frameId, hosts }: HostHealthLinkProps) {
  if (hosts === null || (hosts.read === null && !hosts.failed)) return null;
  const player = playersByDevice(snapshot, bootFacts as never).find((row) =>
    row.frames.some((entry) => entry.frameId === frameId));
  if (player === undefined) return null;
  const chip = hosts.failed
    ? { text: `${player.name} · host health not read`, severity: "unknown" as const }
    : hostChip(player.name, judgeHost(hosts, player.deviceId).health);
  return (
    <OwnerLinks links={[{ text: chip.text, severity: chip.severity,
      href: formatRoute({ section: "hardware", id: player.deviceId }) }]} />
  );
}
