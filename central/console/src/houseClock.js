import { useCallback, useEffect } from "react";

import { readHouse } from "./houseApi.js";
import { usePolledRead } from "./polledRead.js";
import { setHouseClock } from "./timeWords.js";

/** The event Settings › House sends after a save: the shell re-reads the house's clock at once. */
const HOUSE_SAVED = "photo-wall:house-saved";

/** Said by Settings › House after Central saved it. */
export function announceHouseSaved() {
  window.dispatchEvent(new Event(HOUSE_SAVED));
}

/** How often the house's clock is re-read: a save in another tab shows within this. */
const CADENCE_MS = 60000;

/**
 * The house's clock (Settings › House #2, #5) for every time the console shows: the shell's one
 * read, which sets timeWords.js's zone and 12- or 24-hour clock (`setHouseClock`). Returns a key
 * that changes with them ("Europe/London 24h"; null until read) for the shell's route context, so
 * every page renders again when the house's clock changes. A failed read keeps the clock it has.
 *
 * @param {{skip: boolean}} options skip until sign-in is confirmed
 * @returns {string|null}
 */
export function useHouseClock({ skip }) {
  const load = useCallback(async (previous) => {
    const answer = /** @type {{ok: true, settings: {time: {zone: string}, clock: "12h"|"24h"}}|{ok: false}} */ (await readHouse());
    if (!answer.ok) return previous;
    setHouseClock({ zone: answer.settings.time.zone, clock: answer.settings.clock });
    return `${answer.settings.time.zone} ${answer.settings.clock}`;
  }, []);
  const { value, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, skip, initial: /** @type {string|null} */ (null) });
  useEffect(() => {
    const saved = () => void refresh();
    window.addEventListener(HOUSE_SAVED, saved);
    return () => window.removeEventListener(HOUSE_SAVED, saved);
  }, [refresh]);
  return value;
}
