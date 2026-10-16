import type { Meta, StoryObj } from "@storybook/react-vite";

import { FrameLighting } from "./frame-lighting";
import { LIVING_ROOM, PROFILES, ROOMS, UPGRADED } from "./room-lights.fixtures";

const meta = {
  title: "Domain/FrameLighting",
  component: FrameLighting,
  args: { lighting: UPGRADED, onChange: () => {}, rooms: ROOMS, profiles: PROFILES, disabled: false },
} satisfies Meta<typeof FrameLighting>;

export default meta;
type Story = StoryObj<typeof meta>;
// Taller than the catalogue's 800 x 600 view: screenshot whole (tests/browser/test_console_catalog_browser.py).
const WHOLE = ["full-page"];

/** As the upgrade leaves a Frame: one even wash at the Display's matched light, a paper that matches the TV. */
export const Upgraded: Story = { tags: WHOLE };
/** The owner's room: daylight from the right (its Sensor's reading as the reference), the overheads'
 * wash and the bookshelf spotlight's cone, on Photo Rag at Gloss ink. */
export const LivingRoom: Story = { tags: WHOLE, args: { lighting: LIVING_ROOM } };
/** No lights: the picture is the panel's black, and the note says so. */
export const NoLights: Story = { tags: WHOLE, args: { lighting: { ...UPGRADED, lights: [] } } };
/** No room lights in the house yet: Add waits, and the link goes to Settings › Lights. */
export const NoRoomLights: Story = {
  tags: WHOLE,
  args: { lighting: { ...UPGRADED, lights: [] }, rooms: { ...ROOMS, lights: [] }, profiles: [] },
};
/** Four lights: Add is disabled, and the note says why. */
export const Full: Story = {
  tags: WHOLE,
  args: { lighting: { ...LIVING_ROOM, lights: [...LIVING_ROOM.lights, UPGRADED.lights[0]] } },
};
/** While Done or Revert runs, or the Pi stopped taking changes: nothing can change. */
export const Disabled: Story = { tags: WHOLE, args: { lighting: LIVING_ROOM, disabled: true } };
