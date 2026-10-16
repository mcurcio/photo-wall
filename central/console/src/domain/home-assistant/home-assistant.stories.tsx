import type { Meta, StoryObj } from "@storybook/react-vite";

import type { HomeAssistantView } from "../../generated/contract";
import { HEARD, ROOMS } from "../room-lights/room-lights.fixtures";
import { HomeAssistant } from "./home-assistant";

const LINKED: HomeAssistantView = {
  link: { state: "connected", since: HEARD - 3600 },
  entities: [
    { entity: "light.living_room", reports_light: "overheads", reports_sensor: null, heard_at: HEARD - 60 },
    { entity: "light.bookshelf", reports_light: "bookshelf-spot", reports_sensor: null, heard_at: null },
    { entity: "sensor.window_illuminance", reports_light: null, reports_sensor: "window-meter", heard_at: HEARD },
    { entity: "light.kitchen", reports_light: null, reports_sensor: null, heard_at: HEARD - 120 },
  ],
  refused: [],
};

const meta = {
  title: "Domain/HomeAssistant",
  component: HomeAssistant,
  args: { view: LINKED, house: ROOMS, since: () => "Tue 14:20", note: null, onLink: async () => null },
} satisfies Meta<typeof HomeAssistant>;

export default meta;
type Story = StoryObj<typeof meta>;
// Taller than the catalogue's 800 x 600 view: screenshot whole (tests/browser/test_console_catalog_browser.py).
const WHOLE = ["full-page"];

/** Connected: three entities linked, the kitchen light not linked yet (listed first). */
export const Connected: Story = { tags: WHOLE };
/** No broker address: nothing reports through Home Assistant. */
export const NotSetUp: Story = {
  args: { view: { link: { state: "not-configured", since: null }, entities: [], refused: [] } },
};
/** The broker went away: the link says since when. */
export const Away: Story = { tags: WHOLE, args: { view: { ...LINKED, link: { state: "away", since: HEARD } } } };
/** Messages the link could not record, kept with the reason. */
export const MessagesNotRecorded: Story = {
  tags: WHOLE,
  args: {
    view: {
      ...LINKED,
      refused: [
        { topic: "photowall/light/light.kitchen", reason: "unknown_address", at: HEARD },
        { topic: "photowall/illuminance/sensor.porch", reason: "out_of_range", at: HEARD - 600 },
      ],
    },
  },
};
