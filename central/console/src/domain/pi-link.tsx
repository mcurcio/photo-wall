import { clock } from "../facts.js";
import { layerEvidence, type NodeDevice } from "../nodeRead.js";
import { Note } from "../patterns/fact-row";
import { RETIRED_NOT_READ } from "../players.js";
import { Disclosure } from "../ui/disclosure";
import { hostsBusLink } from "./bus-link";
import { FactLine } from "./fact-line";
import type { HostsRead } from "./hosts-read";

export interface PiLinkProps {
  deviceId: string;
  /** The shell's fleet host read: where Central serves the link. */
  hosts: HostsRead | null;
  node: NodeDevice;
  snapshot: object | null;
  playerId: string | null;
  retired: boolean;
}

/**
 * A Pi's Link and sessions (console by domain § Fleet, Hardware): whether Central's hub holds
 * its Node API link (`hostsBusLink`, from the fleet host read the list reads too), then Host Management's
 * session: when it last reported, and which session and boot (nodeRead.js `layerEvidence`, its
 * L0 row). A retired Pi is not read.
 */
export function PiLink({ deviceId, hosts, node, snapshot, playerId, retired }: PiLinkProps) {
  if (retired) return <Note>{`${RETIRED_NOT_READ} (Host Management)`}</Note>;
  const host = layerEvidence({ nodeDevice: node, snapshot, playerId }).find((row) => row.key === "host");
  return (
    <>
      <Note>
        {node.readAt == null ? "Node read: not read yet" : `Node read as of ${clock(node.readAt)}`}
        {node.error !== null && node.read !== null && ", refresh failed"}
      </Note>
      <FactLine label="Node API link" fact={hostsBusLink(hosts, deviceId)} />
      {host !== undefined && (
        <div role="group" aria-label={host.layer}>
          {host.facts.map((entry, index) => (
            <FactLine key={`${entry.label}-${index}`} label={entry.label} fact={entry.fact} />
          ))}
          {host.details.length > 0 && (
            <Disclosure summary={`${host.layer} details`}>
              <ul>{host.details.map((detail) => <li key={detail}>{detail}</li>)}</ul>
            </Disclosure>
          )}
        </div>
      )}
    </>
  );
}
