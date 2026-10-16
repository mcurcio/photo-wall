import { useCallback } from "react";

import { readHomeAssistant } from "../domain/home-assistant/api";
import { HomeAssistant } from "../domain/home-assistant/home-assistant";
import { readRoomLights } from "../domain/room-lights/api";
import type { HomeAssistantStatesView, RoomLightsView } from "../generated/contract";
import { NOT_READ, nextRead, type PageRead, PolledPage } from "../patterns/polled-page";
import { usePolledRead } from "../polledRead.js";
import { sinceTime } from "../timeWords.js";

/** How often the page reads again while shown: a new state shows within this. */
const CADENCE_MS = 3000;

type Read = PageRead<{ view: HomeAssistantStatesView; house: RoomLightsView }>;

/**
 * Settings › Home Assistant (`#/home-assistant`): when Central last heard Home Assistant's states,
 * every light and illuminance entity with the room light or Sensor it reports, and the states
 * Central could not record; read every CADENCE_MS while shown. The picking is House › Lights'.
 */
export function HomeAssistantPage() {
  const load = useCallback(async (previous: Read): Promise<Read> => {
    const [view, house] = await Promise.all([readHomeAssistant(), readRoomLights()]);
    return nextRead(previous, view.ok && house.ok ? { view: view.view, house: house.view } : null);
  }, []);
  const { value: read, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, initial: NOT_READ as Read });
  return (
    <PolledPage read={read} onRetry={() => void refresh()} what="Central did not answer with Home Assistant's states."
      stale="Central did not answer the last read: these are the states as last read.">
      {({ view, house }) => <HomeAssistant view={view} house={house} since={(at) => sinceTime(at)} />}
    </PolledPage>
  );
}
