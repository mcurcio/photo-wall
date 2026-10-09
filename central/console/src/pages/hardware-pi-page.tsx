import { useRef } from "react";

import { retireRequest, useConfirm } from "../ConfirmAction.jsx";
import { frameLinks } from "../domain/frame-links";
import { HostHealthPanel, type HostsRead } from "../domain/host-health";
import { PiHeader } from "../domain/pi-header";
import { PiLink } from "../domain/pi-link";
import { NodeRecords, nodeReadsAllowed, useNodeControlValue } from "../nodeControl.js";
import { useNodeDevice } from "../nodeRead.js";
import { EmptyState, EntityPage } from "../patterns/entity-page";
import { Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { RebootSection } from "../PlayerCommands.jsx";
import { BOOT_FACTS_UNAVAILABLE, playersByDevice } from "../players.js";
import { formatRoute, routeIdName } from "../routes.js";
import { Button } from "../ui/button";
import { Section } from "../ui/section";

export interface HardwarePiPageProps {
  deviceId: string;
  snapshot: object | null;
  bootFacts: { unavailable?: boolean; loaded?: boolean; devices?: Map<string, object> } | null;
  /** The shell's fleet host read; null while it is skipped (node control not on): no Health. */
  hosts: HostsRead | null;
}

/**
 * One Pi's Hardware page (`#/hardware/<device-id>`; console by domain § Fleet): the Pi header,
 * then Reboot (with its history), Health (from the shell's fleet host read), Link and sessions
 * (the fleet host read) and the Danger zone (Retire). Its bound Frames are links to their
 * Binding on the Wall: a Bound Pi is retired only after it is unbound there. Node records are
 * read only here and on the Pi's Software page, never for a retired Pi and never while node
 * control is not on. Each section sits behind its own error boundary.
 */
export function HardwarePiPage({ deviceId, snapshot, bootFacts, hosts }: HardwarePiPageProps) {
  const row = playersByDevice(snapshot, bootFacts as never).find((candidate) => candidate.deviceId === deviceId)
    ?? null;
  const retired = row?.standing === "retired";
  const control = useNodeControlValue();
  const node = useNodeDevice(deviceId, { skip: row === null || retired || !nodeReadsAllowed(control) });
  const nameRef = useRef<HTMLHeadingElement | null>(null);
  const focusName = () => nameRef.current?.focus();
  const { open, confirmation } = useConfirm(focusName, focusName);

  if (row === null) {
    return (
      <EntityPage header={null}>
        <EmptyState>
          {`${routeIdName("Pi", deviceId, { start: true })} is not known to Central.`}
          {bootFacts?.loaded ? "" : " Boot records are not read yet."}
        </EmptyState>
        <p><a href={formatRoute({ section: "hardware" })}>All Pis</a></p>
      </EntityPage>
    );
  }
  const player = row.player as { id: string } | null;
  const frames = frameLinks(row.frames);
  return (
    <EntityPage header={
      <PiHeader row={row} on="hardware" headingRef={nameRef}>
        {bootFacts?.unavailable && <Note>{`${BOOT_FACTS_UNAVAILABLE}; serials may be out of date.`}</Note>}
        <OwnerLinks label="Bound Frames:" links={frames} />
      </PiHeader>
    }>
      {!retired && (
        <NodeRecords quiet>
          <Section title="Reboot" resetKey={node.readAt}>
            <RebootSection key={deviceId} deviceId={deviceId} name={row.name} node={node} snapshot={snapshot as never}
              playerId={player?.id ?? null} />
          </Section>
        </NodeRecords>
      )}
      {!retired && hosts !== null && (
        <NodeRecords quiet>
          <Section title="Health" resetKey={(hosts.read as { read_at?: number } | null)?.read_at}>
            <HostHealthPanel hosts={hosts} deviceId={deviceId} />
          </Section>
        </NodeRecords>
      )}
      <NodeRecords>
        <Section title="Link and sessions" resetKey={node.readAt}>
          <PiLink deviceId={deviceId} hosts={hosts} node={node} snapshot={snapshot} playerId={player?.id ?? null} retired={retired} />
        </Section>
      </NodeRecords>
      {player !== null && row.standing === "unbound" && (
        <Section title="Danger zone">
          <Button aria-label={`Retire player ${player.id}`}
            onClick={(event) => open(event, retireRequest(snapshot as never, bootFacts, player.id))}>
            Retire player
          </Button>
        </Section>
      )}
      {player !== null && row.standing === "bound" && (
        <Section title="Danger zone">
          <EmptyState>Retire is offered once this Pi drives no Frame. Unbind it from each Frame first:</EmptyState>
          <OwnerLinks links={frames} />
        </Section>
      )}
      {confirmation("")}
    </EntityPage>
  );
}
