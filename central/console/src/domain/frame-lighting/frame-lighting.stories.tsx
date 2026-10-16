import type { Meta, StoryObj } from "@storybook/react-vite";

import { LIVING_ROOM, PROFILES, ROOMS, UPGRADED } from "../room-lights/room-lights.fixtures";
import { FrameLighting } from "./frame-lighting";
import { referenceFor, referenceWords } from "./model";

const meta = {
  title: "Domain/FrameLighting",
  component: FrameLighting,
  args: { lighting: UPGRADED, onChange: () => {}, rooms: ROOMS, profiles: PROFILES, disabled: false },
} satisfies Meta<typeof FrameLighting>;

export default meta;
type Story = StoryObj<typeof meta>;
// Taller than the catalogue's 800 x 600 view: screenshot whole (tests/browser/test_console_catalog_browser.py).
const WHOLE = ["full-page"];

/** As a Frame starts: one even wash at the Display's matched light, a paper that matches the Display. */
export const Upgraded: Story = { tags: WHOLE };
/** The owner's room: daylight from the right (its Sensor's reading as the reference), the overheads'
 * wash and the bookshelf spotlight's cone, on Photo Rag at Gloss ink. */
export const LivingRoom: Story = { tags: WHOLE, args: { lighting: LIVING_ROOM } };
/** No lights: the picture is the Display's black, and the note says so. */
export const NoLights: Story = { tags: WHOLE, args: { lighting: { ...UPGRADED, lights: [] } } };
/** No room lights in the house yet: Add waits, and the link goes to House › Lights. */
export const NoRoomLights: Story = {
  tags: WHOLE,
  args: { lighting: { ...UPGRADED, lights: [] }, rooms: { ...ROOMS, lights: [] }, profiles: [] },
};
/** As many lights as a Frame takes: Add is disabled, and the note says why. */
export const Full: Story = {
  tags: WHOLE,
  args: { lighting: { ...LIVING_ROOM, lights: [...LIVING_ROOM.lights, UPGRADED.lights[0]] } },
};
/** While Done or Revert runs, or the Pi stopped taking changes: nothing can change. */
export const Disabled: Story = { tags: WHOLE, args: { lighting: LIVING_ROOM, disabled: true } };

/** The reference each room light takes: a pure check the catalogue runs before any story renders. */
const [daylight, overheads] = ROOMS.lights;
const checks: [string, string][] = [
  [referenceWords(referenceFor(daylight.light, ROOMS.sensors[0])), "Read when its Sensor said 320 lx"],
  [referenceWords(referenceFor(overheads.light, undefined)), "Read with the light on at 100 %"],
  [referenceWords(referenceFor(daylight.light, ROOMS.sensors[1])), "Read with the light on at 100 %"],
];
for (const [said, meant] of checks) {
  if (said !== meant) throw new Error(`Frame lighting words: ${JSON.stringify(said)} is not ${JSON.stringify(meant)}`);
}
