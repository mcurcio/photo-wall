import type { Meta, StoryObj } from "@storybook/react-vite";

import { HealthBadge } from "./health-badge";

const meta = {
  title: "Patterns/HealthBadge",
  component: HealthBadge,
  args: { verdict: { severity: "ok", label: "Reporting", receipt: "Host Management last reported 3 s ago" } },
} satisfies Meta<typeof HealthBadge>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Ok: Story = {};
export const Notice: Story = {
  args: { verdict: { severity: "notice", label: "Warm", receipt: "Host Management last reported 3 s ago" } },
};
export const Alarm: Story = {
  args: { verdict: { severity: "alarm", label: "Host Management silent · last reported 4 min ago", receipt: null } },
};
export const Unknown: Story = { args: { verdict: { severity: "unknown", label: "Not read", receipt: null } } };
export const Todo: Story = { args: { verdict: { severity: "todo", label: "Not driving a Frame", receipt: null } } };
