import type { Meta, StoryObj } from "@storybook/react-vite";

import { LeaveGuard } from "./leave-guard";

const meta = {
  title: "Patterns/LeaveGuard",
  component: LeaveGuard,
  args: { open: true, onKeep: () => {}, onRevert: () => {}, onStay: () => {}, keepBlocked: null },
} satisfies Meta<typeof LeaveGuard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};

/** The Pi has not presented the latest change yet: Keep waits, and says so. */
export const KeepBlocked: Story = {
  args: { keepBlocked: "Keep is offered once the Pi presents your latest change." },
};
