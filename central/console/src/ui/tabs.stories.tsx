import type { Meta, StoryObj } from "@storybook/react-vite";

import { Tabs } from "./tabs";

const meta = {
  title: "Primitives/Tabs",
  component: Tabs,
  args: {
    label: "Frame settings",
    tabs: [
      { value: "overview", label: "Overview" },
      { value: "position", label: "Position" },
      { value: "picture", label: "Picture" },
      { value: "hardware", label: "Hardware" },
    ],
    value: "position",
    onValueChange: () => {},
    children: "The Position tab's panel.",
  },
} satisfies Meta<typeof Tabs>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
