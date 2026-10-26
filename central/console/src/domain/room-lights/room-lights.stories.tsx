import type { Meta, StoryObj } from "@storybook/react-vite";

import type { ColourLineView, RoomLightView } from "../../generated/contract";
import { brightnessWords, colourLineWords, entryWords, idFromName, readingWords, refusalWords } from "./model";
import { RoomLights } from "./room-lights";
import { HEARD, HOME_ASSISTANT_STATES, PI_NAMES, ROOMS } from "./room-lights.fixtures";

const meta = {
  title: "Domain/RoomLights",
  component: RoomLights,
  args: {
    view: ROOMS, heard: HOME_ASSISTANT_STATES.entities, piName: (id: string) => PI_NAMES[id] ?? id, since: () => "Tue 14:20", note: null,
    onSaveLight: async () => null, onRemoveLight: async () => null, onSaveSensor: async () => null, onRemoveSensor: async () => null,
    onSaveColourSensor: async () => null, onRemoveColourSensor: async () => null,
  },
} satisfies Meta<typeof RoomLights>;

export default meta;
type Story = StoryObj<typeof meta>;
// Taller than the catalogue's 800 x 600 view: screenshot whole (tests/browser/test_console_catalog_browser.py).
const WHOLE = ["full-page"];
const [DAYLIGHT, OVERHEADS, SPOT, LAMP] = ROOMS.lights;
const SKY = ROOMS.colour_sensors[0];

/** `light` renamed, with its colour line `colour`. */
const lined = (light: RoomLightView, name: string, colour: Partial<ColourLineView>): RoomLightView => ({
  ...light, id: idFromName(name), light: { ...light.light, name } as RoomLightView["light"], colour: { ...light.colour, ...colour },
});

/** A bulb that reports itself, heard on at 60 % and following its own kelvin: its entry and its
 * colour line from the same facts, as Central gives them. */
const following = (name: string, kelvin: number): RoomLightView => lined({
  ...SPOT, entry: { kind: "reported", colour: { kind: "kelvin", kelvin }, state: { kind: "dimmer", on: true, brightness: 0.6 } },
  heard_at: HEARD,
}, name, { colour: { kind: "kelvin", kelvin }, colour_from: "following", stated_at: HEARD });

/** The owner's house: daylight a window Sensor measures, its colour following the sky's colour
 * sensor; the colour overheads at 60 %, following their bulb; the spotlight not heard yet, typed
 * until its bulb says; a lamp always on, typed; two Sensors, one never heard; one colour sensor. */
export const House: Story = { tags: WHOLE };
/** Nothing set up yet. */
export const Empty: Story = { args: { view: { lights: [], sensors: [], colour_sensors: [] } } };
/** A remove Central refused, said in words. */
export const RemoveRefused: Story = { tags: WHOLE, args: { note: refusalWords("Not removed", "sensor_reports_a_light") } };
/** Removing a colour sensor a light follows, refused in words. */
export const ColourSensorFollowed: Story = { tags: WHOLE, args: { note: refusalWords("Not removed", "colour_sensor_followed") } };
/** An off light, and one that can't tell (its last brightness kept; its bulb never stated a colour,
 * so its typed colour stands and is never said to be kept). */
export const OffAndUnavailable: Story = {
  tags: WHOLE,
  args: {
    view: {
      ...ROOMS,
      lights: [
        { ...OVERHEADS, entry: { kind: "reported", colour: { kind: "rgb", rgb: [1, 0.8, 0.6] },
          state: { kind: "dimmer", on: false, brightness: 0 } } },
        { ...SPOT, entry: { kind: "reported", colour: { kind: "kelvin", kelvin: 2700 },
          state: { kind: "dimmer", on: true, brightness: 0.4 } }, heard_at: HEARD, cant_tell_since: HEARD,
          colour: { ...SPOT.colour, cant_tell_since: HEARD } },
      ],
    },
  },
};
/** Each colour line: typed; following its bulb in white and in a colour mode; following a colour
 * sensor; typed until the bulb or the colour sensor says; can't tell, the colour kept; a bulb's
 * colour outside what the screen draws. */
export const ColourLines: Story = {
  tags: WHOLE,
  args: {
    view: {
      ...ROOMS,
      lights: [
        LAMP,
        following("Hall bulb", 3400),
        OVERHEADS,
        DAYLIGHT,
        SPOT,
        lined(DAYLIGHT, "Porch light", { colour: { kind: "kelvin", kelvin: 5500 }, colour_from: "never-stated", stated_at: null }),
        lined(DAYLIGHT, "Window light", { cant_tell_since: HEARD - 3600 }),
        following("Cold bulb", 34000),
      ],
      colour_sensors: [{ ...SKY, followed_by: ["daylight", "porch-light", "window-light"] }],
    },
  },
};
/** The colour sensor can't tell: its colour kept, and the light following it says so. */
export const ColourSensorCantTell: Story = {
  tags: WHOLE,
  args: {
    view: {
      ...ROOMS,
      lights: [{ ...DAYLIGHT, colour: { ...DAYLIGHT.colour, cant_tell_since: HEARD - 600 } }],
      colour_sensors: [{ ...SKY, cant_tell_since: HEARD - 600 }],
    },
  },
};
/** No colour sensor yet: the list says what one is for. */
export const NoColourSensors: Story = { tags: WHOLE, args: { view: { ...ROOMS, lights: [LAMP], colour_sensors: [] } } };
/** At phone width. */
export const Phone: Story = { tags: WHOLE, parameters: { phone: true } };

/** The state and colour lines are the entry's and Central's line, never when they were heard: a pure
 * check the catalogue runs before any story renders. */
const clock = () => "18:42";
const names = { sky: "Sky colour" };
const checks: [string, string][] = [
  [entryWords({ kind: "reported", colour: { kind: "kelvin", kelvin: 2700 }, state: { kind: "dimmer", on: true, brightness: 0.6 } }), "On, 60 %"],
  [entryWords({ kind: "reported", colour: { kind: "kelvin", kelvin: 2700 }, state: { kind: "dimmer", on: false, brightness: 0 } }), "Off"],
  [entryWords({ kind: "measured", colour: { kind: "kelvin", kelvin: 6500 }, state: { kind: "measured", lux: 1234.4 } }), "1,234 lx"],
  [entryWords({ kind: "reported", colour: { kind: "kelvin", kelvin: 2700 }, state: { kind: "not-reported" } }), "Not reported yet"],
  [entryWords({ kind: "fixed", colour: { kind: "kelvin", kelvin: 3000 } }), "On, 100 %"],
  [readingWords(null), "Not reported yet"],
  [idFromName("Café lamp"), "cafe-lamp"],
  [idFromName("Spot-"), "spot"],
  [colourLineWords(DAYLIGHT.colour, names, clock), "Following Sky colour 3,400 K"],
  [colourLineWords(OVERHEADS.colour, names, clock), "Following its bulb's colour"],
  [colourLineWords({ ...SPOT.colour, cant_tell_since: HEARD }, names, clock), "Typed 2,700 K until its bulb says · can't tell since 18:42"],
  [colourLineWords(SPOT.colour, names, clock), "Typed 2,700 K until its bulb says"],
  [colourLineWords({ ...DAYLIGHT.colour, cant_tell_since: HEARD }, names, clock), "Can't tell since 18:42 (keeping 3,400 K)"],
  [brightnessWords({ ...SPOT, entry: { kind: "reported", colour: { kind: "kelvin", kelvin: 2700 },
    state: { kind: "dimmer", on: true, brightness: 0.6 } }, cant_tell_since: HEARD }, clock), "Can't tell since 18:42 (keeping On, 60 %)"],
];
for (const [said, meant] of checks) {
  if (said !== meant) throw new Error(`room light words: ${JSON.stringify(said)} is not ${JSON.stringify(meant)}`);
}
