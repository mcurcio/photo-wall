import type { Meta, StoryObj } from "@storybook/react-vite";

import { HoldBanner } from "./power-lane";
import { HOLD, NAMES, NOW } from "./power-week.fixtures";

const meta = {
  title: "Domain/HoldBanner",
  component: HoldBanner,
  args: { holds: [HOLD], names: NAMES, now: NOW, busy: false, onResume: () => {} },
} satisfies Meta<typeof HoldBanner>;

export default meta;
type Story = StoryObj<typeof meta>;

/** All off in force until Night off's 23:00, with Resume schedule. */
export const HeldOff: Story = {};
/** All on until the operator resumes. */
export const HeldOnUntilResumed: Story = { args: { holds: [{ ...HOLD, power: "on", until: null, until_at: null }] } };
