import type { Meta, StoryObj } from "@storybook/react-vite";

import { SEVERITIES } from "../design/tokens";
import { StatusChip } from "./status-chip";

const meta = {
  title: "Primitives/StatusChip",
  component: StatusChip,
  args: { severity: "ok", children: "Showing" },
} satisfies Meta<typeof StatusChip>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Ok: Story = {};
export const Todo: Story = { args: { severity: "todo", children: "Not calibrated" } };
export const Notice: Story = { args: { severity: "notice", children: "Throttled earlier" } };
export const Alarm: Story = { args: { severity: "alarm", children: "Lost its link" } };
export const Unknown: Story = { args: { severity: "unknown", children: "Not read yet" } };

/** Every severity side by side. */
export const All: Story = {
  render: () => (
    <div className="flex flex-wrap gap-2">
      {SEVERITIES.map((severity) => (
        <StatusChip key={severity} severity={severity}>
          {severity}
        </StatusChip>
      ))}
    </div>
  ),
};
