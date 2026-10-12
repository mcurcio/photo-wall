import type { Meta, StoryObj } from "@storybook/react-vite";

import { HoldBanner } from "./power-lane";
import { HOLD, HOLD_HALL, NAMES, NOW } from "./power-week.fixtures";

const meta = {
  title: "Domain/HoldBanner",
  component: HoldBanner,
  args: { holds: [HOLD], names: NAMES, now: NOW, busy: false, onResume: () => {} },
} satisfies Meta<typeof HoldBanner>;

export default meta;
type Story = StoryObj<typeof meta>;

/** All off in force until the bedroom's next change, Night off's 23:00, with Resume schedule. */
export const HeldOff: Story = {};
/** All on until the operator resumes. */
export const HeldOnUntilResumed: Story = { args: { holds: [{ ...HOLD, power: "on", until: null, until_at: null }] } };
/** All off on two Frames that end apart: the bedroom (in Night off) at 23:00, the hall (outside it)
 * at Night off's on time, 07:00; one row and one Resume schedule each. */
export const HeldEachUntilItsOwnChange: Story = { args: { holds: [HOLD, HOLD_HALL] } };
