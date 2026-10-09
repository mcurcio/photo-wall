import { busLinkFact, clock, fact } from "../facts.js";
import { layerEvidence, type NodeDevice, nodeUnknown } from "../nodeRead.js";
import { Note } from "../patterns/fact-row";
import { RETIRED_NOT_READ } from "../players.js";
import { Disclosure } from "../ui/disclosure";
import { FactLine } from "./fact-line";

export interface PiLinkProps {
  node: NodeDevice;
  snapshot: object | null;
  playerId: string | null;
  retired: boolean;
}

/**
 * A Pi's Link and sessions (console by domain § Fleet, Hardware): whether Central's hub holds
 * its Node API link (facts.js `busLinkFact`, from this Pi's node read), then Host Management's
 * session: when it last reported, and which session and boot (nodeRead.js `layerEvidence`, its
 * L0 row). A retired Pi is not read.
 */
export function PiLink({ node, snapshot, playerId, retired }: PiLinkProps) {
  if (retired) return <Note>{`${RETIRED_NOT_READ} (Host Management)`}</Note>;
  const host = layerEvidence({ nodeDevice: node, snapshot, playerId }).find((row) => row.key === "host");
  const read = node.read as { bus_link?: unknown } | null;
  return (
    <>
      <Note>
        {node.readAt == null ? "Node read: not read yet" : `Node read as of ${clock(node.readAt)}`}
        {node.error !== null && node.read !== null && ", refresh failed"}
      </Note>
      <FactLine label="Node API link" fact={read === null
        ? fact({ kind: "unknown", why: nodeUnknown(node) })
        : busLinkFact(read.bus_link as never, node.readAt)} />
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
