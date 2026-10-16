import { useCallback, useState } from "react";

import { type PiChoice, RoomLights } from "../domain/room-lights";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { playerName } from "../players.js";
import { usePolledRead } from "../polledRead.js";
import {
  readRoomLights, removeRoomLight, removeSensor, type RoomLightSave, type RoomLightsView, type RoomLightView,
  saveRoomLight, saveSensor, type SensorSave, type SensorView,
} from "../roomLightsApi";
import { refusalWords } from "../roomLightWords.js";

/** How often the page reads the lights again while it is shown: a light switched shows within this. */
const CADENCE_MS = 3000;

type Read = { status: "loading" } | { status: "failed" } | { status: "read"; view: RoomLightsView; failed: boolean };

/** The Pis a Sensor may be wired to, from the snapshot's inventory. */
function pisOf(snapshot: object | null): PiChoice[] {
  const players = (snapshot as { inventory?: { players?: { id: string; device_id: string; serial?: string | null }[] } } | null)
    ?.inventory?.players ?? [];
  return players.map((player) => ({ id: player.id, name: playerName(player.device_id, player.serial ?? null) }));
}

/**
 * Settings › Lights (`#/lights`; decision 0028; design-language T8): the house's room lights and
 * Sensors, and the Home Assistant link. The page reads them every CADENCE_MS while shown (a light
 * switched in the house shows its new state), and again after each change; a failed read keeps the
 * last one with a line saying so. Add, change and remove go straight to Central, which refuses a
 * change that would break a rule (a light a Frame uses, a Sensor that reports a light) in words.
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
  const change = async (done: string, refused: string, work: () => Promise<{ ok: boolean; error?: string }>) => {
    const answer = await work();
    void refresh();
    if (answer.ok) {
      setNote(done);
      return null;
    }
    return refusalWords(refused, (answer as { error: string }).error);
  };
  return (
    <>
      {read.failed && <Note>Central did not answer the last read: these are the lights as last read.</Note>}
      <RoomLights view={read.view} pis={pisOf(snapshot)} note={note}
        onSaveLight={(id: string, body: RoomLightSave) => change(`Saved ${body.name}.`, "Not saved", () => saveRoomLight(id, body))}
        onRemoveLight={(light: RoomLightView) => change(`Removed ${light.name}.`, "Not removed", () => removeRoomLight(light.id))}
        onSaveSensor={(id: string, body: SensorSave) => change(`Saved ${body.name}.`, "Not saved", () => saveSensor(id, body))}
        onRemoveSensor={(sensor: SensorView) => change(`Removed ${sensor.name}.`, "Not removed", () => removeSensor(sensor.id))} />
    </>
  );
}
