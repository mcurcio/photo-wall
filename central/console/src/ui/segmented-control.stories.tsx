import type { Meta, StoryObj } from "@storybook/react-vite";

import { SegmentedControl } from "./segmented-control";

const meta = {
  title: "Primitives/SegmentedControl",
  component: SegmentedControl,
  args: {
    label: "Step",
    value: "10",
    options: [{ value: "1", label: "1 px" }, { value: "10", label: "10 px" }, { value: "50", label: "50 px" }],
    onChange: () => {},
  },
} satisfies Meta<typeof SegmentedControl>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
export const Disabled: Story = { args: { disabled: true } };
