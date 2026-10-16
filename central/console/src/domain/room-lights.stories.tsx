import type { Meta, StoryObj } from "@storybook/react-vite";

import { refusalWords, stateWords } from "../roomLightWords.js";
import { RoomLights } from "./room-lights";
import { PIS, ROOMS } from "./room-lights.fixtures";

const meta = {
  title: "Domain/RoomLights",
  component: RoomLights,
  args: {
    view: ROOMS, pis: PIS, note: null,
    onSaveLight: async () => null, onRemoveLight: async () => null, onSaveSensor: async () => null, onRemoveSensor: async () => null,
  },
} satisfies Meta<typeof RoomLights>;

export default meta;
type Story = StoryObj<typeof meta>;
// Taller than the catalogue's 800 x 600 view: screenshot whole (tests/browser/test_console_catalog_browser.py).
const WHOLE = ["full-page"];

/** The owner's house: daylight a window Sensor reports, the RGB overheads at 60 %, the spotlight not
 * reported yet, a lamp always on; two Sensors, one never heard; Home Assistant connected. */
export const House: Story = { tags: WHOLE };
/** Nothing set up yet, and no Home Assistant broker address. */
export const Empty: Story = {
  args: { view: { lights: [], sensors: [], home_assistant: { state: "not-configured", since: null } } },
};
/** The broker went away: every light keeps its last state, and the link says since when. */
export const HomeAssistantAway: Story = {
  tags: WHOLE,
  args: { view: { ...ROOMS, home_assistant: { state: "away", since: 1_791_000_000 } } },
};
/** A remove Central refused, said in words. */
export const RemoveRefused: Story = {
  tags: WHOLE,
  args: { note: refusalWords("Not removed", "sensor-reports-a-light") },
};
/** Off and unavailable lights. */
export const OffAndUnavailable: Story = {
  tags: WHOLE,
  args: {
    view: {
      ...ROOMS,
      lights: [
        { ...ROOMS.lights[1], state: { kind: "dimmer", on: false, brightness: 0, rgb: null } },
        { ...ROOMS.lights[2], state: { kind: "unavailable" }, heard_at: 1_791_000_000 },
      ],
    },
  },
};
/** At phone width. */
export const Phone: Story = { tags: WHOLE, parameters: { phone: true } };

/** The state line is the state's, never when it was heard: a pure check the catalogue runs before any story renders. */
const checks: [string, string][] = [
  [stateWords({ kind: "dimmer", on: true, brightness: 0.6, rgb: null }), "On, 60 %"],
  [stateWords({ kind: "dimmer", on: false, brightness: 0, rgb: null }), "Off"],
  [stateWords({ kind: "measured", lux: 320 }), "320 lx"],
  [stateWords({ kind: "unavailable" }), "Unavailable"],
  [stateWords({ kind: "not-reported" }), "Not reported yet"],
];
for (const [said, meant] of checks) {
  if (said !== meant) throw new Error(`room light words: ${JSON.stringify(said)} is not ${JSON.stringify(meant)}`);
}
