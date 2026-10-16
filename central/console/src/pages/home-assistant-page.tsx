import { useCallback } from "react";

import { readHomeAssistant } from "../domain/home-assistant/api";
import { HomeAssistant, type Link } from "../domain/home-assistant/home-assistant";
import { reportedBy, sensorAt } from "../domain/home-assistant/model";
import { readRoomLights, saveRoomLight, saveSensor } from "../domain/room-lights/api";
import { newId, refusalWords } from "../domain/room-lights/model";
import type { HomeAssistantView, RoomLightsView } from "../generated/contract";
import { NOT_READ, nextRead, type PageRead, PolledPage } from "../patterns/polled-page";
import { type Outcome, useSaveNote } from "../patterns/save-dialog";
import { usePolledRead } from "../polledRead.js";
import { sinceTime } from "../timeWords.js";

/** How often the page reads again while shown: an entity's word shows within this. */
const CADENCE_MS = 3000;

type Read = PageRead<{ view: HomeAssistantView; house: RoomLightsView }>;

/**
 * Settings › Home Assistant (`#/home-assistant`): the link's health, every entity with the room
 * light or Sensor it reports (linked here), and the messages the link could not record; read every
 * CADENCE_MS while shown and again after each link. Linking an entity saves the room light or
 * Sensor with the entity as its reference; Central refuses one already taken, in words.
 */
export function HomeAssistantPage() {
  const load = useCallback(async (previous: Read): Promise<Read> => {
    const [view, house] = await Promise.all([readHomeAssistant(), readRoomLights()]);
    return nextRead(previous, view.ok && house.ok ? { view: view.view, house: house.view } : null);
  }, []);
  const { value: read, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, initial: NOT_READ as Read });
  const { note, settle } = useSaveNote(refresh);
  const notLinked = (answer: { error: string }) => refusalWords("Not linked", answer.error);

  const link = (house: RoomLightsView) => (entity: string, to: Link): Outcome => {
    if (to.kind === "light") {
      const light = house.lights.find((each) => each.id === to.id);
      if (light === undefined) return Promise.resolve(refusalWords("Not linked", "unknown_room_light"));
      return settle(saveRoomLight(light.id, reportedBy(light, entity)), `${entity} now reports ${light.light.name}.`, notLinked);
    }
    if (to.kind === "sensor") {
      const sensor = house.sensors.find((each) => each.id === to.id);
      if (sensor === undefined) return Promise.resolve(refusalWords("Not linked", "unknown_sensor"));
      const name = sensor.sensor.name;
      return settle(saveSensor(sensor.id, sensorAt(name, entity)), `${entity} now reports ${name}.`, notLinked);
    }
    const id = newId(to.name, house.sensors.map((each) => each.id));
    if ("problem" in id) return Promise.resolve(id.problem);
    return settle(saveSensor(id.id, sensorAt(to.name, entity)), `${entity} now reports ${to.name}.`, notLinked);
  };
  return (
    <PolledPage read={read} onRetry={() => void refresh()} what="Central did not answer with the Home Assistant link."
      stale="Central did not answer the last read: this is the link as last read.">
      {({ view, house }) => (
        <HomeAssistant view={view} house={house} since={(at) => sinceTime(at)} note={note} onLink={link(house)} />
      )}
    </PolledPage>
  );
}
