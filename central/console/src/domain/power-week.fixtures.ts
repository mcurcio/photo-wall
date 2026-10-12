import type { NightOffView } from "./night-off-editor";
import type { HoldView, PowerBand, Week, WeekWindow } from "./power-lane";

/** A London week from Mon 12 Oct 2026: Night off 23:00-07:00 every night on one Frame. */
const DATES = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"];

function nightBands(frameId: string): PowerBand[] {
  const bands: PowerBand[] = [];
  const band = (power: "on" | "off", because: PowerBand["because"], start: string, end: string): PowerBand => ({
    frame_id: frameId, power, because, start_at: 0, end_at: 0, start, end, hold_id: null, source: null,
  });
  bands.push(band("off", "night-off", `${DATES[0]}T00:00`, `${DATES[0]}T07:00`));
  DATES.forEach((date, index) => {
    bands.push(band("on", "on-underneath", `${date}T07:00`, `${date}T23:00`));
    const next = DATES[index + 1];
    bands.push(band("off", "night-off", `${date}T23:00`, next === undefined ? "2026-10-19T00:00" : `${next}T07:00`));
  });
  return bands.map((entry, index) => ({ ...entry, start_at: index, end_at: index + 1 }));
}

const WINDOWS: WeekWindow[] = DATES.slice(0, 5).map((date, index) => ({
  program_id: "p-evenings", name: "Evenings", scene_id: "evening", priority: "normal",
  start_at: 100 + index, end_at: 200 + index, start: `${date}T17:00`, end: `${date}T23:00`, plays: true,
}));

export const WEEK: Week = {
  first_day: DATES[0], zone: "Europe/London", default_scene_id: "calm", windows: WINDOWS, power: nightBands("bedroom"),
};

/** All off pressed at Wed 12:00: a hold until Night off's 23:00. */
const HELD_BANDS = WEEK.power.flatMap((band) => band.start === "2026-10-14T07:00" ? [
  { ...band, end: "2026-10-14T12:00" },
  { ...band, power: "off" as const, because: "hold" as const, start: "2026-10-14T12:00", end: "2026-10-14T23:00",
    start_at: band.start_at + 0.5, hold_id: "8d4c2f7a-3b1e-4c5d-9e6f-1a2b3c4d5e6f", source: { kind: "console" as const } },
] : [band]);
export const HELD_WEEK: Week = { ...WEEK, power: HELD_BANDS };

export const HOLD: HoldView = {
  hold_id: "8d4c2f7a-3b1e-4c5d-9e6f-1a2b3c4d5e6f", power: "off", frame_ids: ["bedroom"], source: { kind: "console" },
  started_at: 1_792_000_000, until_at: 1_792_039_600, until: "2026-10-14T23:00",
};

/** The same All off on a second Frame outside Night off: held to Night off's next on time. */
export const HOLD_HALL: HoldView = { ...HOLD, frame_ids: ["hall"], until_at: 1_792_068_400, until: "2026-10-15T07:00" };

export const NAMES = { bedroom: "Bedroom", hall: "Hall" };
export const NOW = "2026-10-14T12:00";

export const NIGHT_OFF: NightOffView = {
  night_off: {
    off: { at: "clock", time: "23:00" }, on: { at: "clock", time: "07:00" },
    days: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], frames: { kind: "all-frames" },
    darkness: "display-off", revision: 3,
  },
  next_off: "2026-10-14T23:00",
  next_on: "2026-10-15T07:00",
  waits_for_zone: false,
};
