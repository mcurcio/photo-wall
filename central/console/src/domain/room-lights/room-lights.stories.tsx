import type { Meta, StoryObj } from "@storybook/react-vite";

import { entryWords, idFromName, readingWords, refusalWords } from "./model";
import { RoomLights } from "./room-lights";
import { HOME_ASSISTANT_STATES, PI_NAMES, ROOMS } from "./room-lights.fixtures";

const meta = {
  title: "Domain/RoomLights",
  component: RoomLights,
  args: {
    view: ROOMS, heard: HOME_ASSISTANT_STATES.entities, piName: (id: string) => PI_NAMES[id] ?? id, since: () => "Tue 14:20", note: null,
    onSaveLight: async () => null, onRemoveLight: async () => null, onSaveSensor: async () => null, onRemoveSensor: async () => null,
  },
} satisfies Meta<typeof RoomLights>;

export default meta;
type Story = StoryObj<typeof meta>;
// Taller than the catalogue's 800 x 600 view: screenshot whole (tests/browser/test_console_catalog_browser.py).
const WHOLE = ["full-page"];

/** The owner's house: daylight a window Sensor measures, the colour overheads at 60 %, the spotlight
 * not heard yet, a lamp always on; two Sensors, one never heard. */
export const House: Story = { tags: WHOLE };
/** Nothing set up yet. */
export const Empty: Story = { args: { view: { lights: [], sensors: [], colour_sensors: [] } } };
/** A remove Central refused, said in words. */
export const RemoveRefused: Story = { tags: WHOLE, args: { note: refusalWords("Not removed", "sensor_reports_a_light") } };
/** An off light, and one that can't tell (its last brightness kept). */
export const OffAndUnavailable: Story = {
  tags: WHOLE,
  args: {
    view: {
      ...ROOMS,
      lights: [
        { ...ROOMS.lights[1], entry: { kind: "reported", colour: { kind: "rgb", rgb: [1, 0.8, 0.6] },
          state: { kind: "dimmer", on: false, brightness: 0 } } },
        { ...ROOMS.lights[2], entry: { kind: "reported", colour: { kind: "kelvin", kelvin: 2700 },
          state: { kind: "dimmer", on: true, brightness: 0.4 } }, heard_at: 1_791_000_000, cant_tell_since: 1_791_000_000 },
      ],
    },
  },
};
/** At phone width. */
export const Phone: Story = { tags: WHOLE, parameters: { phone: true } };

/** The state line is the entry's, never when it was heard: a pure check the catalogue runs before any story renders. */
const checks: [string, string][] = [
  [entryWords({ kind: "reported", colour: { kind: "kelvin", kelvin: 2700 }, state: { kind: "dimmer", on: true, brightness: 0.6 } }), "On, 60 %"],
  [entryWords({ kind: "reported", colour: { kind: "kelvin", kelvin: 2700 }, state: { kind: "dimmer", on: false, brightness: 0 } }), "Off"],
  [entryWords({ kind: "measured", colour: { kind: "kelvin", kelvin: 6500 }, state: { kind: "measured", lux: 1234.4 } }), "1,234 lx"],
  [entryWords({ kind: "reported", colour: { kind: "kelvin", kelvin: 2700 }, state: { kind: "not-reported" } }), "Not reported yet"],
  [entryWords({ kind: "fixed", colour: { kind: "kelvin", kelvin: 3000 } }), "On, 100 %"],
  [readingWords(null), "Not reported yet"],
  [idFromName("Café lamp"), "cafe-lamp"],
  [idFromName("Spot-"), "spot"],
];
for (const [said, meant] of checks) {
  if (said !== meant) throw new Error(`room light words: ${JSON.stringify(said)} is not ${JSON.stringify(meant)}`);
}
