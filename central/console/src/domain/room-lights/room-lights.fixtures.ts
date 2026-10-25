import { CONSTANTS, type FrameLighting, type HomeAssistantStatesView, type RoomLightsView } from "../../generated/contract";

/*
 * One house's lights for the stories: daylight a window Sensor measures, the colour overheads that
 * report themselves, the bookshelf spotlight not heard yet, and a lamp nobody reports. Instants are
 * fixed, so the screenshots never move.
 */

export const HEARD = 1_791_000_000;
const MINUTE = 60;
const RECIPE = "lut33-perceptual-relcol-bpc-1-lcms2.16";

export const ROOMS: RoomLightsView = {
  lights: [
    {
      id: "daylight",
      light: { kind: "measured", name: "Daylight", sensor: "window-meter", colour: { kind: "typed", kelvin: 6500 } },
      entry: { kind: "measured", colour: { kind: "kelvin", kelvin: 6500 }, state: { kind: "measured", lux: 320 } },
      heard_at: HEARD, cant_tell_since: null, named_by: ["lobby"],
      colour: { colour: { kind: "kelvin", kelvin: 6500 }, source: "typed", sensor: null, colour_from: "typed", stated_at: null,
        cant_tell_since: null },
    },
    {
      id: "overheads",
      light: {
        kind: "reported", name: "Overheads", reporter: { source: CONSTANTS.HOME_ASSISTANT_SOURCE, address: "light.living_room" },
        colour: { kind: "its-bulb", typed_kelvin: 2700 },
      },
      entry: {
        kind: "reported", colour: { kind: "rgb", rgb: [1, 0.8, 0.6] },
        state: { kind: "dimmer", on: true, brightness: 0.6 },
      },
      heard_at: HEARD - MINUTE, cant_tell_since: null, named_by: ["lobby"],
      colour: { colour: { kind: "rgb", rgb: [1, 0.8, 0.6] }, source: "its-bulb", sensor: null, colour_from: "following",
        stated_at: HEARD - MINUTE, cant_tell_since: null },
    },
    {
      id: "bookshelf-spot",
      light: {
        kind: "reported", name: "Bookshelf spot", reporter: { source: CONSTANTS.HOME_ASSISTANT_SOURCE, address: "light.bookshelf" },
        colour: { kind: "its-bulb", typed_kelvin: 2700 },
      },
      entry: { kind: "reported", colour: { kind: "kelvin", kelvin: 2700 }, state: { kind: "not-reported" } },
      heard_at: null, cant_tell_since: null, named_by: [],
      colour: { colour: { kind: "kelvin", kelvin: 2700 }, source: "its-bulb", sensor: null, colour_from: "never-stated",
        stated_at: null, cant_tell_since: null },
    },
    {
      id: "lamp",
      light: { kind: "fixed", name: "Reading lamp", colour: { kind: "typed", kelvin: 3000 } },
      entry: { kind: "fixed", colour: { kind: "kelvin", kelvin: 3000 } },
      heard_at: null, cant_tell_since: null, named_by: [],
      colour: { colour: { kind: "kelvin", kelvin: 3000 }, source: "typed", sensor: null, colour_from: "typed", stated_at: null,
        cant_tell_since: null },
    },
  ],
  sensors: [
    {
      id: "window-meter",
      sensor: { name: "Window meter", at: { source: CONSTANTS.HOME_ASSISTANT_SOURCE, address: "sensor.window_illuminance" } },
      reading: { kind: "measured", lux: 320 }, heard_at: HEARD, cant_tell_since: null, reports: "daylight",
    },
    {
      id: "hall-pi",
      sensor: { name: "Hall Pi meter", at: { source: CONSTANTS.PI_SOURCE, address: "player-1" } },
      reading: null, heard_at: null, cant_tell_since: null, reports: null,
    },
  ],
  colour_sensors: [],
};

export const PI_NAMES: Readonly<Record<string, string>> = { "player-1": "Player …1a2b3c" };

/** Home Assistant's states as Central last heard them: the two lights and the window meter the house
 * names, the kitchen light nothing names yet, and the porch meter. */
export const HOME_ASSISTANT_STATES: HomeAssistantStatesView = {
  galleria: { version: "v0.40.0", kinds: ["light", "illuminance", "colour-temperature"], heard_at: HEARD,
    central_version: "0.40.0", update: false },
  heard_at: HEARD,
  entities: [
    { entity: "light.living_room", kind: "light", name: "Living room", reports: "overheads", heard_at: HEARD - MINUTE },
    { entity: "light.bookshelf", kind: "light", name: "Bookshelf", reports: "bookshelf-spot", heard_at: null },
    { entity: "sensor.window_illuminance", kind: "illuminance", name: "Window illuminance", reports: "window-meter", heard_at: HEARD },
    { entity: "light.kitchen", kind: "light", name: "Kitchen", reports: null, heard_at: HEARD - 2 * MINUTE },
    { entity: "sensor.porch_illuminance", kind: "illuminance", name: "Porch illuminance", reports: null, heard_at: HEARD },
  ],
  refused: [],
};

/** The paper profiles on the list. */
export const PROFILES = [
  { profile_sha256: "a".repeat(64), recipe_id: RECIPE, name: "Hahnemühle Photo Rag 308 (matte)" },
  { profile_sha256: "b".repeat(64), recipe_id: RECIPE, name: "Canson Baryta Photographique II" },
];

/** As a Frame starts: one wash of the lamp at the Display's matched lux, a paper that matches the Display. */
export const UPGRADED: FrameLighting = {
  mat_depth_mm: 0,
  lights: [{ kind: "wash", light: "lamp", lux: 1000, reference: { kind: "at-full" } }],
  paper: { whiteness: 0.9, ink_density: 3.5, profile: null },
};

/** The owner's Frame: daylight from the window as a far light, the overheads' wash and the spotlight's cone. */
export const LIVING_ROOM: FrameLighting = {
  mat_depth_mm: 2.5,
  lights: [
    {
      kind: "far", light: "daylight", lux: 400, reference: { kind: "at-reading", lux: 320 }, from_degrees: 90,
      steep_degrees: 30, softness_degrees: 25,
    },
    { kind: "wash", light: "overheads", lux: 150, reference: { kind: "at-full" } },
    {
      kind: "close", light: "bookshelf-spot", lux: 800, reference: { kind: "at-full" }, across_mm: -300, up_mm: 900,
      out_mm: 400, inner_beam_degrees: 20, outer_beam_degrees: 40,
    },
  ],
  paper: { whiteness: 0.9, ink_density: 2.3, profile: { profile_sha256: "a".repeat(64), recipe_id: RECIPE } },
};
