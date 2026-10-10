import type { Meta, StoryObj } from "@storybook/react-vite";

import { Button } from "./button";

const meta = {
  title: "Primitives/Button",
  component: Button,
  args: { children: "Reboot" },
} satisfies Meta<typeof Button>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Outline: Story = {};
export const Primary: Story = { args: { variant: "primary", children: "Save" } };
export const Ghost: Story = { args: { variant: "ghost", children: "Rename" } };
export const Danger: Story = { args: { variant: "danger", children: "Confirm retire" } };
export const Disabled: Story = { args: { disabled: true } };
