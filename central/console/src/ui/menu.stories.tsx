import type { Meta, StoryObj } from "@storybook/react-vite";

import { Menu } from "./menu";

const meta = {
  title: "Primitives/Menu",
  component: Menu,
  args: {
    label: "More actions for garden",
    items: [
      { label: "Check now", onAction: () => {} },
      { label: "Delete", onAction: () => {} },
    ],
  },
} satisfies Meta<typeof Menu>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Closed: the "More" button. */
export const Default: Story = {};
