import type { Meta, StoryObj } from "@storybook/react-vite";

import { Disclosure } from "./disclosure";

const meta = {
  title: "Primitives/Disclosure",
  component: Disclosure,
  args: {
    summary: "Details",
    children: "Kernel 6.12.25, firmware 2026-09-14, booted from Central.",
  },
} satisfies Meta<typeof Disclosure>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Closed: Story = {};
export const Open: Story = { args: { defaultOpen: true } };
