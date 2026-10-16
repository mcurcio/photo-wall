import type { RoomLightsView } from "../roomLightsApi";
import type { FrameLightingDraft, ProfileChoice } from "./frame-lighting";

/*
 * One house's lights for the stories (decision 0028; the owner's room, 2026-10-15): daylight a
 * window Sensor reports, the RGB overheads Home Assistant reports, the bookshelf spotlight he will
 * mount, and a lamp nobody reports. Instants are fixed, so the screenshots never move.
 */

const HEARD = 1_791_000_000;

export const ROOMS: RoomLightsView = {
  lights: [
    {
      id: "daylight", name: "Daylight", reporter: { kind: "sensor", sensor: "window-meter" },
      colour: { kind: "fixed", kelvin: 6500 }, state: { kind: "measured", lux: 320 }, heard_at: HEARD, named_by: ["lobby"],
    },
    {
      id: "overheads", name: "Overheads", reporter: { kind: "home-assistant-light", entity: "light.living_room" },
      colour: { kind: "reported", channel_shares: [0.42, 0.36, 0.22] },
      state: { kind: "dimmer", on: true, brightness: 0.6, rgb: [1, 0.8, 0.6] }, heard_at: HEARD - 60, named_by: ["lobby"],
    },
    {
      id: "bookshelf-spot", name: "Bookshelf spot", reporter: { kind: "home-assistant-light", entity: "light.bookshelf" },
      colour: { kind: "fixed", kelvin: 2700 }, state: { kind: "not-reported" }, heard_at: null, named_by: [],
    },
    {
      id: "lamp", name: "Reading lamp", reporter: { kind: "fixed" }, colour: { kind: "fixed", kelvin: 3000 },
      state: { kind: "dimmer", on: true, brightness: 1, rgb: null }, heard_at: null, named_by: [],
    },
  ],
  sensors: [
    {
      id: "window-meter", name: "Window meter", at: { kind: "home-assistant", entity: "sensor.window_illuminance" },
      reading: 320, heard_at: HEARD, reports: "daylight",
    },
    {
      id: "hall-pi", name: "Hall Pi meter", at: { kind: "pi", player_id: "player-1" }, reading: null, heard_at: null,
      reports: null,
    },
  ],
  home_assistant: { state: "connected", since: HEARD - 3600 },
  refused: [],
};

export const PIS = [{ id: "player-1", name: "Player …1a2b3c" }];

export const PROFILES: ProfileChoice[] = [
  { profile_sha256: "a".repeat(64), recipe_id: "lut33-perceptual-relcol-bpc-1-lcms2.16", name: "Hahnemühle Photo Rag 308 (matte)" },
  { profile_sha256: "b".repeat(64), recipe_id: "lut33-perceptual-relcol-bpc-1-lcms2.16", name: "Canson Baryta Photographique II" },
];

/** As the upgrade leaves a Frame: one wash of the room at the Display's matched lux, a paper that
 * matches the TV. */
export const UPGRADED: FrameLightingDraft = {
  mat_depth_mm: 0,
  lights: [{ kind: "wash", light: "lamp", lux: 1000, reference: "dimmer-at-full" }],
  paper: { whiteness: 0.9, ink_density: 3.5, profile: null },
};

/** The owner's Frame: daylight from the window as a far light, the overheads' wash and the spotlight's cone. */
export const LIVING_ROOM: FrameLightingDraft = {
  mat_depth_mm: 2.5,
  lights: [
    { kind: "far", light: "daylight", lux: 400, reference: 320, from_degrees: 90, steep_degrees: 30, softness_degrees: 25 },
    { kind: "wash", light: "overheads", lux: 150, reference: "dimmer-at-full" },
    {
      kind: "close", light: "bookshelf-spot", lux: 800, reference: "dimmer-at-full", across_mm: -300, up_mm: 900, out_mm: 400,
      inner_beam_degrees: 20, outer_beam_degrees: 40,
    },
  ],
  paper: { whiteness: 0.9, ink_density: 2.3, profile: { profile_sha256: "a".repeat(64), recipe_id: PROFILES[0].recipe_id } },
};
