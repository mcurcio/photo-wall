import type { Meta, StoryObj } from "@storybook/react-vite";

import { AckBadge } from "./ack-badge";

const meta = {
  title: "Patterns/AckBadge",
  component: AckBadge,
  args: { state: "requested" },
} satisfies Meta<typeof AckBadge>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Requested: Story = {};
export const Acknowledged: Story = { args: { state: "acknowledged", at: "21:04:07" } };
/** Central served no time for the acknowledgement: no dangling separator. */
export const AcknowledgedNoTime: Story = { args: { state: "acknowledged", at: null } };
