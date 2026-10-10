import type { Meta, StoryObj } from "@storybook/react-vite";

import { NudgePad } from "./nudge-pad";

const meta = {
  title: "Patterns/NudgePad",
  component: NudgePad,
  args: { label: "Move the picture", subject: "the whole picture", onNudge: () => {} },
} satisfies Meta<typeof NudgePad>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
export const Disabled: Story = { args: { disabled: true } };
