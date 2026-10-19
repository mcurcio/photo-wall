import type { Meta, StoryObj } from "@storybook/react-vite";

import { FramePosition } from "./frame-position";
import type { Adjustment } from "./live-adjustment";
import { UPGRADED } from "./room-lights/room-lights.fixtures";

const ADJUSTMENT: Adjustment = {
  draft: { fit: { mode: "box", left: 0.125, top: 0.125, width: 0.75, height: 0.75 }, lighting: UPGRADED },
  change: () => ({ valid: true }),
  changeLighting: () => {},
  dirty: false,
  phase: "shown",
  error: null,
  busy: false,
  ack: { revision: 1, at: 1_791_000_000 },
  latest: 1,
  done: async () => true,
  revert: async () => {},
  retry: () => {},
  restore: async () => ({ ok: false, error: "trial_ended" }),
  savedAt: null,
  leftAt: null,
};

const meta = {
  title: "Domain/FramePosition",
  component: FramePosition,
  args: {
    adjustment: ADJUSTMENT, size: [1920, 1080], displayMm: [950, 540], turn: 0,
    bound: true, piHref: "#/players/device-a", hardwareHref: "#/wall/frames/hall/hardware",
  },
} satisfies Meta<typeof FramePosition>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A box three quarters of an upright screen's width and height: its pixels, shape and size in centimetres. */
export const Landscape: Story = {};
/** A square Frame on a 16:9 screen: width and height set apart, 800 × 800 pixels. */
export const Square: Story = {
  args: {
    adjustment: {
      ...ADJUSTMENT, dirty: true,
      draft: { fit: { mode: "box", left: 100 / 1920, top: 140 / 1080, width: 800 / 1920, height: 800 / 1080 }, lighting: UPGRADED },
    },
  },
};
/** A screen hung top to the right: the screen is drawn portrait, a tall box on it, and a change
 * the Pi has not shown yet. */
export const TurnedScreen: Story = {
  args: {
    turn: 90,
    adjustment: {
      ...ADJUSTMENT, dirty: true, phase: "sending", latest: 2,
      draft: { fit: { mode: "box", left: 0.05, top: 0, width: 0.9, height: 0.9 }, lighting: UPGRADED },
    },
  },
};
/** The Display states no size: the Frame's pixels and shape, no centimetres. */
export const NoStatedSize: Story = { args: { displayMm: null } };
/** The screen's size is not known: a 16:9 stand-in, and a note that the numbers are shares of the screen. */
export const UnknownSize: Story = { args: { size: null, displayMm: null } };
/** The resolution is not known but the Display states its size: the stand-in takes the display's own shape. */
export const UnknownResolutionStatedSize: Story = { args: { size: null, displayMm: [950, 540] } };
/** A small box on a turned screen (a quarter of each side): only its corners take the pointer, and
 * its middle moves it. */
export const SmallTurnedBox: Story = {
  args: {
    turn: 90,
    adjustment: {
      ...ADJUSTMENT,
      draft: { fit: { mode: "box", left: 0.375, top: 0.375, width: 0.25, height: 0.25 }, lighting: UPGRADED },
    },
  },
};
