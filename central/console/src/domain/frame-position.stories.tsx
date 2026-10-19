import type { Meta, StoryObj } from "@storybook/react-vite";
import { useEffect } from "react";

import { FramePosition, type FramePositionProps } from "./frame-position";
import type { Adjustment } from "./live-adjustment";
import { UPGRADED } from "./room-lights/room-lights.fixtures";

const ADJUSTMENT: Adjustment = {
  draft: { fit: { mode: "rectangle", centre_x: 0.5, centre_y: 0.5, width: 0.75 }, lighting: UPGRADED },
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
  refused: false,
  sessionFit: null,
};

const meta = {
  title: "Domain/FramePosition",
  component: FramePosition,
  args: {
    adjustment: ADJUSTMENT, size: { width: 1920, height: 1080 }, turn: 0, aspect: 16 / 9,
    piHref: "#/players/device-a", hardwareHref: "#/wall/frames/hall/hardware",
  },
} satisfies Meta<typeof FramePosition>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A landscape Frame on an upright screen: the picture three quarters of the screen's width, centred. */
export const Landscape: Story = {};
/** A portrait Frame on a screen hung top to the right: the screen is drawn portrait, the picture
 * upright on it, and a change the Pi has not shown yet. */
export const TurnedScreen: Story = {
  args: {
    turn: 90, aspect: 1080 / 1920,
    adjustment: {
      ...ADJUSTMENT, dirty: true, phase: "sending", latest: 2,
      draft: { fit: { mode: "rectangle", centre_x: 0.5, centre_y: 0.45, width: 0.9 }, lighting: UPGRADED },
    },
  },
};
/** The Pi has reported no display size: steps are pixels of a 1,920 × 1,080 display. */
export const UnknownSize: Story = { args: { size: null } };

/** Presses "Move right" once on a picture already flush with the right edge: the move is refused. */
function PushedPastTheEdge({ adjustment, size, turn, aspect, piHref, hardwareHref }: FramePositionProps) {
  useEffect(() => {
    document.querySelector<HTMLButtonElement>('button[aria-label="Move right: the picture"]')?.click();
  }, []);
  return <FramePosition adjustment={adjustment} size={size} turn={turn} aspect={aspect} piHref={piHref}
    hardwareHref={hardwareHref} />;
}
/** A move past the screen's edge is refused where it is made, and says so; nothing is sent. */
export const OffScreenRefused: Story = {
  args: {
    adjustment: {
      ...ADJUSTMENT, draft: { fit: { mode: "rectangle", centre_x: 0.75, centre_y: 0.5, width: 0.5 }, lighting: UPGRADED },
    },
  },
  render: PushedPastTheEdge,
};
/** Central refused the draft (it measured the screen otherwise): the Pi keeps the session's position,
 * the draft is drawn dashed and Done waits for a position that fits. */
export const CentralRefused: Story = {
  args: {
    adjustment: {
      ...ADJUSTMENT, dirty: true, refused: true,
      draft: { fit: { mode: "rectangle", centre_x: 0.7, centre_y: 0.5, width: 0.5 }, lighting: UPGRADED },
      sessionFit: { mode: "rectangle", centre_x: 0.5, centre_y: 0.5, width: 0.5 },
      error: { words: "", code: "position_off_screen", stops: false, refused: "" },
    },
  },
};
/** The Frame profile is not set: no editor, only the way to set it. */
export const NoProfile: Story = { args: { aspect: null } };
