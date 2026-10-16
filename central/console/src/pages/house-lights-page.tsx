import { useCallback, useState } from "react";

import {
  readRoomLights, removeRoomLight, removeSensor, saveRoomLight, saveSensor,
} from "../domain/room-lights/api";
import { refusalWords } from "../domain/room-lights/model";
import { RoomLights } from "../domain/room-lights/room-lights";
import type { RoomLight, RoomLightsView, RoomLightView, Sensor, SensorView } from "../generated/contract";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { playerNameById } from "../players.js";
import { usePolledRead } from "../polledRead.js";
import { sinceTime } from "../timeWords.js";

/** How often the page reads the lights again while it is shown: a light switched shows within this. */
const CADENCE_MS = 3000;

type Read = { status: "loading" } | { status: "failed" } | { status: "read"; view: RoomLightsView; failed: boolean };

/**
 * House › Lights (`#/lights`): the house's room lights and Sensors with their last states, read
 * every CADENCE_MS while shown (a light switched in the house shows its new state) and again after
 * each change; a failed read keeps the last one with a line saying so. Changes go straight to
 * Central, which refuses one that would break a rule (a light a Frame uses, a Sensor that measures
 * a light) in words.
 */
export function HouseLightsPage({ snapshot }: { snapshot: object | null }) {
  const load = useCallback(async (previous: Read): Promise<Read> => {
    const answer = await readRoomLights();
    if (answer.ok) return { status: "read", view: answer.view, failed: false };
    return previous.status === "read" ? { ...previous, failed: true } : { status: "failed" };
  }, []);
  const { value: read, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, initial: { status: "loading" } as Read });
  const [note, setNote] = useState<string | null>(null);

  if (read.status === "loading") return <Note>Loading…</Note>;
  if (read.status === "failed") {
    return (
      <ProblemCard scope="central" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Central did not answer with the house's lights." doing={null}
        action={{ label: "Try again", onAction: () => void refresh() }} />
    );
  }
  /** One change: null when Central took it (the page reads again and says so), else its words. */
  const change = async (done: string, refused: string, work: () => Promise<{ ok: true } | { ok: false; error: string }>) => {
    const answer = await work();
    void refresh();
    if (answer.ok) {
      setNote(done);
      return null;
    }
    return refusalWords(refused, answer.error);
  };
  return (
    <>
      {read.failed && <Note>Central did not answer the last read: these are the lights as last read.</Note>}
      <RoomLights view={read.view} note={note} piName={(id) => playerNameById(snapshot, id)} since={(at) => sinceTime(at)}
        onSaveLight={(id: string, light: RoomLight) => change(`Saved ${light.name}.`, "Not saved", () => saveRoomLight(id, light))}
        onRemoveLight={(light: RoomLightView) => change(`Removed ${light.light.name}.`, "Not removed", () => removeRoomLight(light.id))}
        onSaveSensor={(id: string, sensor: Sensor) => change(`Saved ${sensor.name}.`, "Not saved", () => saveSensor(id, sensor))}
        onRemoveSensor={(sensor: SensorView) => change(`Removed ${sensor.sensor.name}.`, "Not removed", () => removeSensor(sensor.id))} />
    </>
  );
}
