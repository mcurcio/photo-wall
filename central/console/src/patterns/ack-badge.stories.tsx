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
/** An action asked of a Pi that is not a preview: its own words, the same wait. */
export const RequestedAction: Story = { args: { state: "requested", words: "Restart requested" } };
export const Acknowledged: Story = { args: { state: "acknowledged", at: "21:04:07" } };
/** The Pi presents the change before one Central refused: said in brackets. */
export const AcknowledgedRefused: Story = {
  args: { state: "acknowledged", at: "18:30:41", note: "your last change was refused" },
};
/** Central served no time for the acknowledgement: no dangling separator. */
export const AcknowledgedNoTime: Story = { args: { state: "acknowledged", at: null } };
