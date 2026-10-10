import type { Meta, StoryObj } from "@storybook/react-vite";

import { Alert, AlertLine } from "./alert";

const meta = {
  title: "Primitives/Alert",
  component: Alert,
  args: {
    severity: "alarm",
    title: "Photo Wall refused this Source as too large, so it selects nothing",
    children: (
      <>
        <AlertLine>It has no tags and no dates, so it asks for your whole photo library.</AlertLine>
        <AlertLine>Edit it in Sources and narrow it with tags or dates.</AlertLine>
      </>
    ),
  },
} satisfies Meta<typeof Alert>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Alarm: Story = {};
export const Notice: Story = {
  args: { severity: "notice", title: "Throttled earlier", children: undefined },
};
