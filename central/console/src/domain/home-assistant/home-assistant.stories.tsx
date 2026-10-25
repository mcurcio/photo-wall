import type { Meta, StoryObj } from "@storybook/react-vite";

import { HEARD, HOME_ASSISTANT_STATES, ROOMS } from "../room-lights/room-lights.fixtures";
import { HomeAssistant } from "./home-assistant";

const meta = {
  title: "Domain/HomeAssistant",
  component: HomeAssistant,
  args: { view: HOME_ASSISTANT_STATES, house: ROOMS, since: () => "Tue 14:20" },
} satisfies Meta<typeof HomeAssistant>;

export default meta;
type Story = StoryObj<typeof meta>;
// Taller than the catalogue's 800 x 600 view: screenshot whole (tests/browser/test_console_catalog_browser.py).
const WHOLE = ["full-page"];

/** Heard: three entities linked, the kitchen light not linked yet (listed first). */
export const Heard: Story = { tags: WHOLE };
/** Nothing heard yet: the Galleria integration has not published. */
export const NotHeardYet: Story = {
  args: {
    view: { galleria: { version: null, kinds: [], heard_at: null, central_version: "0.40.0", update: true },
      heard_at: null, entities: [], refused: [] },
  },
};
/** States Central could not record, kept with the reason. */
export const StatesNotRecorded: Story = {
  tags: WHOLE,
  args: {
    view: {
      ...HOME_ASSISTANT_STATES,
      refused: [
        { source: "home-assistant", address: "light.kitchen", reason: "unknown_address", at: HEARD },
        { source: "home-assistant", address: "sensor.porch", reason: "out_of_range", at: HEARD - 600 },
      ],
    },
  },
};
