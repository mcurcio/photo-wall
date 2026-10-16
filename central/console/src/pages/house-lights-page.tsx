import { useCallback } from "react";

import {
  readRoomLights, removeRoomLight, removeSensor, saveRoomLight, saveSensor,
} from "../domain/room-lights/api";
import { refusalWords } from "../domain/room-lights/model";
import { RoomLights } from "../domain/room-lights/room-lights";
import type { RoomLight, RoomLightsView, RoomLightView, Sensor, SensorView } from "../generated/contract";
import { NOT_READ, nextRead, type PageRead, PolledPage } from "../patterns/polled-page";
import { useSaveNote } from "../patterns/save-dialog";
import { playerNameById } from "../players.js";
import { usePolledRead } from "../polledRead.js";
import { sinceTime } from "../timeWords.js";

/** How often the page reads the lights again while it is shown: a light switched shows within this. */
const CADENCE_MS = 3000;

/**
 * House › Lights (`#/lights`): the house's room lights and Sensors with their last states, read
 * every CADENCE_MS while shown (a light switched in the house shows its new state) and again after
 * each change; a failed read keeps the last one with a line saying so. Changes go straight to
 * Central, which refuses one that would break a rule (a light a Frame uses, a Sensor that measures
 * a light) in words.
 */
export function HouseLightsPage({ snapshot }: { snapshot: object | null }) {
  const load = useCallback(async (previous: PageRead<RoomLightsView>) => {
    const answer = await readRoomLights();
    return nextRead(previous, answer.ok ? answer.view : null);
  }, []);
  const { value: read, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, initial: NOT_READ as PageRead<RoomLightsView> });
  const { note, settle } = useSaveNote(refresh);
  const notSaved = (answer: { error: string }) => refusalWords("Not saved", answer.error);
  const notRemoved = (answer: { error: string }) => refusalWords("Not removed", answer.error);
  return (
    <PolledPage read={read} onRetry={() => void refresh()} what="Central did not answer with the house's lights."
      stale="Central did not answer the last read: these are the lights as last read.">
      {(view) => (
        <RoomLights view={view} note={note} piName={(id) => playerNameById(snapshot, id)} since={(at) => sinceTime(at)}
          onSaveLight={(id: string, light: RoomLight) => settle(saveRoomLight(id, light), `Saved ${light.name}.`, notSaved)}
          onRemoveLight={(light: RoomLightView) => settle(removeRoomLight(light.id), `Removed ${light.light.name}.`, notRemoved)}
          onSaveSensor={(id: string, sensor: Sensor) => settle(saveSensor(id, sensor), `Saved ${sensor.name}.`, notSaved)}
          onRemoveSensor={(sensor: SensorView) => settle(removeSensor(sensor.id), `Removed ${sensor.sensor.name}.`, notRemoved)} />
      )}
    </PolledPage>
  );
}
