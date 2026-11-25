import type { Meta, StoryObj } from "@storybook/react-vite";

import { setHouseClock } from "../timeWords.js";
import { NeverShowList } from "./never-show-list";
import { swatch } from "./source.fixtures";

const at = (hour: number, minute: number, day = 22) => Date.UTC(2026, 10, day, hour, minute) / 1000;

const meta = {
  title: "Domain/NeverShowList",
  component: NeverShowList,
  args: {
    photos: [
      { asset_id: "a1", kind: "image", by: "person:op_aaaaaaaaaaaaaaaa", listed_at: at(21, 4) },
      { asset_id: "a2", kind: "video", by: "console", listed_at: at(9, 30, 20) },
      { asset_id: "a3", kind: "image", by: "console", listed_at: at(18, 15, 21) },
    ],
    now: at(21, 10),
    busy: false,
    onShowAgain: () => {},
    // A same-document picture, so a story reaches no server.
    thumbnail: (assetId: string) => swatch(assetId.charCodeAt(1), assetId === "a2" ? 900 : 1600, assetId === "a2" ? 1600 : 1067),
  },
  beforeEach: () => {
    setHouseClock({ zone: "Europe/London", clock: "24h" });
    return () => setHouseClock({ zone: null, clock: "24h" });
  },
} satisfies Meta<typeof NeverShowList>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Three photos hidden from the wall, newest first, each with Show again. */
export const Default: Story = {};
/** Nothing listed: where to list one from. */
export const Empty: Story = { args: { photos: [] } };
/** A Show again is on its way: the buttons wait. */
export const Busy: Story = { args: { busy: true } };
/** At phone width. */
export const Phone: Story = { parameters: { phone: true } };
