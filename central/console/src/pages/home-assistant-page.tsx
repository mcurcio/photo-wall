import { useCallback, useState } from "react";

import { readHomeAssistant } from "../domain/home-assistant/api";
import { HomeAssistant, type Link } from "../domain/home-assistant/home-assistant";
import { reportedBy, sensorAt } from "../domain/home-assistant/model";
import { readRoomLights, saveRoomLight, saveSensor } from "../domain/room-lights/api";
import { newId, refusalWords } from "../domain/room-lights/model";
import type { HomeAssistantView, RoomLightsView } from "../generated/contract";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { usePolledRead } from "../polledRead.js";
import { sinceTime } from "../timeWords.js";

/** How often the page reads again while shown: an entity's word shows within this. */
const CADENCE_MS = 3000;

type Read = { status: "loading" } | { status: "failed" }
  | { status: "read"; view: HomeAssistantView; house: RoomLightsView; failed: boolean };

/**
 * Settings › Home Assistant (`#/home-assistant`): the link's health, every entity with the room
 * light or Sensor it reports (linked here), and the messages the link could not record; read every
 * CADENCE_MS while shown and again after each link. Linking an entity saves the room light or
 * Sensor with the entity as its reference; Central refuses one already taken, in words.
 */
export function HomeAssistantPage() {
  const load = useCallback(async (previous: Read): Promise<Read> => {
    const [view, house] = await Promise.all([readHomeAssistant(), readRoomLights()]);
    if (view.ok && house.ok) return { status: "read", view: view.view, house: house.view, failed: false };
    return previous.status === "read" ? { ...previous, failed: true } : { status: "failed" };
  }, []);
  const { value: read, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, initial: { status: "loading" } as Read });
  const [note, setNote] = useState<string | null>(null);

  if (read.status === "loading") return <Note>Loading…</Note>;
  if (read.status === "failed") {
    return (
      <ProblemCard scope="central" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Central did not answer with the Home Assistant link." doing={null}
        action={{ label: "Try again", onAction: () => void refresh() }} />
    );
  }
  const { house } = read;
  const link = async (entity: string, to: Link): Promise<string | null> => {
    let answer: { ok: true } | { ok: false; error: string };
    let name: string;
    if (to.kind === "light") {
      const light = house.lights.find((each) => each.id === to.id);
      if (light === undefined) return refusalWords("Not linked", "unknown_room_light");
      name = light.light.name;
      answer = await saveRoomLight(light.id, reportedBy(light, entity));
    } else if (to.kind === "sensor") {
      const sensor = house.sensors.find((each) => each.id === to.id);
      if (sensor === undefined) return refusalWords("Not linked", "unknown_sensor");
      name = sensor.sensor.name;
      answer = await saveSensor(sensor.id, sensorAt(name, entity));
    } else {
      const id = newId(to.name, house.sensors.map((each) => each.id));
      if ("problem" in id) return id.problem;
      name = to.name;
      answer = await saveSensor(id.id, sensorAt(name, entity));
    }
    void refresh();
    if (!answer.ok) return refusalWords("Not linked", answer.error);
    setNote(`${entity} now reports ${name}.`);
    return null;
  };
  return (
    <>
      {read.failed && <Note>Central did not answer the last read: this is the link as last read.</Note>}
      <HomeAssistant view={read.view} house={house} since={(at) => sinceTime(at)} note={note} onLink={link} />
    </>
  );
}
