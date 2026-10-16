import { useCallback, useRef } from "react";

import { retireRequest, useConfirm } from "../ConfirmAction.jsx";
import { frameLinks } from "../domain/frame-links";
import { HostHealthPanel, type HostsRead } from "../domain/host-health";
import { PiHeader } from "../domain/pi-header";
import { PiLink } from "../domain/pi-link";
import { readDrawn, readRefusedReports, readRoomLights, removeSensor, saveSensor } from "../domain/room-lights/api";
import { drawnOf, newId, piSensor, refusalWords, refusedOfPi, sensorOfPi } from "../domain/room-lights/model";
import { PiLight } from "../domain/room-lights/pi-light";
import type { DrawnView, RefusedReportsView, RoomLightsView, SensorView } from "../generated/contract";
import { useNodeDevice } from "../nodeRead.js";
import { EmptyState, EntityPage } from "../patterns/entity-page";
import { Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { useSaveNote } from "../patterns/save-dialog";
import { RebootSection } from "../PlayerCommands.jsx";
import { BOOT_FACTS_UNAVAILABLE, playersByDevice } from "../players.js";
import { usePolledRead } from "../polledRead.js";
import { formatRoute, routeIdName } from "../routes.js";
import { sinceTime } from "../timeWords.js";
import { Button } from "../ui/button";
import { Section } from "../ui/section";

/** How often the Pi's light is read again while the page is shown. */
const LIGHT_CADENCE_MS = 5000;

type LightRead = {
  rooms: RoomLightsView | null; drawn: DrawnView | null; refused: RefusedReportsView | null; failed: boolean;
};

/** The house's lights, what each Pi draws with and the one refusal log, read every LIGHT_CADENCE_MS
 * while `open` (a failed read keeps the last and says so). */
function useLightRead(open: boolean) {
  const load = useCallback(async (previous: LightRead): Promise<LightRead> => {
    const [rooms, drawn, refused] = await Promise.all([readRoomLights(), readDrawn(), readRefusedReports()]);
    return {
      rooms: rooms.ok ? rooms.view : previous.rooms,
      drawn: drawn.ok ? drawn.drawn : previous.drawn,
      refused: refused.ok ? refused.refused : previous.refused,
      failed: !rooms.ok || !drawn.ok || !refused.ok,
    };
  }, []);
  return usePolledRead(load, { cadenceMs: LIGHT_CADENCE_MS, skip: !open,
    initial: { rooms: null, drawn: null, refused: null, failed: false } as LightRead });
}

export interface HardwarePiPageProps {
  deviceId: string;
  snapshot: object | null;
  bootFacts: { unavailable?: boolean; loaded?: boolean; devices?: Map<string, object> } | null;
  /** The shell's fleet host read; null while it is skipped (sign-in not confirmed): no Health. */
  hosts: HostsRead | null;
}

/**
 * One Pi's Hardware page (`#/hardware/<device-id>`; console by domain § Fleet): the Pi header,
 * then Reboot (with its history), Health (from the shell's fleet host read), Link and sessions
 * (the fleet host read), Light (whether it draws with the house's lights as they are now, and the
 * light Sensor wired to it) and the Danger zone (Retire). Its bound Frames are links to their
 * Binding on the Wall: a Bound Pi is retired only after it is unbound there. Node records are
 * read only here and on the Pi's Software page, never for a retired Pi. Each section sits behind its own error boundary.
 */
export function HardwarePiPage({ deviceId, snapshot, bootFacts, hosts }: HardwarePiPageProps) {
  const row = playersByDevice(snapshot, bootFacts as never).find((candidate) => candidate.deviceId === deviceId)
    ?? null;
  const retired = row?.standing === "retired";
  const node = useNodeDevice(deviceId, { skip: row === null || retired });
  const nameRef = useRef<HTMLHeadingElement | null>(null);
  const focusName = () => nameRef.current?.focus();
  const { open, confirmation } = useConfirm(focusName, focusName);
  const enrolled = row !== null && !retired && row.player !== null;
  const { value: light, refresh: rereadLight } = useLightRead(enrolled);
  const { note: lightNote, settle } = useSaveNote(rereadLight);

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
        <Section title="Reboot" resetKey={node.readAt}>
          <RebootSection key={deviceId} deviceId={deviceId} name={row.name} node={node} snapshot={snapshot as never}
            playerId={player?.id ?? null} />
        </Section>
      )}
      {!retired && hosts !== null && (
        <Section title="Health" resetKey={(hosts.read as { read_at?: number } | null)?.read_at}>
          <HostHealthPanel hosts={hosts} deviceId={deviceId} />
        </Section>
      )}
      {enrolled && player !== null && (
        <Section title="Light">
          <PiLight key={player.id} sensor={light.rooms === null ? undefined : sensorOfPi(light.rooms.sensors, player.id)}
            drawn={light.drawn === null ? undefined : drawnOf(light.drawn.players, player.id)}
            refused={light.refused === null ? [] : refusedOfPi(light.refused.refused, player.id)}
            since={(at) => sinceTime(at)} unread={light.failed}
            note={lightNote}
            onSaveSensor={async (name) => {
              const sensors = light.rooms?.sensors ?? [];
              const known = sensorOfPi(sensors, player.id);
              const target = known !== undefined ? { id: known.id } : newId(name, sensors.map((each) => each.id));
              if ("problem" in target) return target.problem;
              return settle(saveSensor(target.id, piSensor(name, player.id)), `Saved ${name}.`,
                (answer) => refusalWords("Not saved", answer.error));
            }}
            onRemoveSensor={(sensor: SensorView) => settle(removeSensor(sensor.id), `Removed ${sensor.sensor.name}.`,
              (answer) => refusalWords("Not removed", answer.error))} />
        </Section>
      )}
      <Section title="Link and sessions" resetKey={node.readAt}>
        <PiLink deviceId={deviceId} hosts={hosts} node={node} snapshot={snapshot} playerId={player?.id ?? null} retired={retired} />
      </Section>
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
