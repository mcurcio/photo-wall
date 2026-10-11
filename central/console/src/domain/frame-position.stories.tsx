import type { Meta, StoryObj } from "@storybook/react-vite";

import { FramePosition } from "./frame-position";
import type { Adjustment } from "./live-adjustment";

const OPENING: [number, number][] = [[0.1, 0.08], [0.9, 0.05], [0.92, 0.95], [0.08, 0.92]];

const ADJUSTMENT: Adjustment = {
  draft: { fit: { mode: "cover", corners: OPENING, rotation: 0 } },
  change: () => ({ valid: true }),
  dirty: false,
  phase: "shown",
  error: null,
  busy: false,
  ack: { revision: 1, at: 1_791_000_000 },
  latest: 1,
  done: async () => true,
  revert: async () => {},
  retry: () => {},
  savedAt: null,
  leftAt: null,
};

const meta = {
  title: "Domain/FramePosition",
  component: FramePosition,
  args: { adjustment: ADJUSTMENT, size: { width: 1920, height: 1080 }, piHref: "#/players/device-a" },
} satisfies Meta<typeof FramePosition>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Cover, the default: the picture keeps its shape and covers the four corners; any angle. */
export const Cover: Story = {};
/** Cover turned 7.5 degrees, a change the Pi has not shown yet. */
export const CoverTurned: Story = {
  args: {
    adjustment: {
      ...ADJUSTMENT, dirty: true, phase: "sending", latest: 2,
      draft: { fit: { mode: "cover", corners: OPENING, rotation: 7.5 } },
    },
  },
};
/** Corners, the explicit choice: stretched exactly onto the corners, quarter turns only. */
export const Corners: Story = {
  args: { adjustment: { ...ADJUSTMENT, draft: { fit: { mode: "corners", corners: OPENING, rotation: 90 } } } },
};
