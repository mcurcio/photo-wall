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
/** Entries that are links to the page that owns the action, beside one done here. */
export const WithLinks: Story = {
  args: {
    label: "More actions for Living room left",
    items: [
      { label: "Restart app", onAction: () => {} },
      { label: "Why is this playing?", href: "#/wall/frames/living-left/overview" },
      { label: "Open Frame", href: "#/wall/frames/living-left/overview" },
    ],
  },
};
/** As it opens on a phone: the same button, its entries in a sheet from the bottom. */
export const AsSheet: Story = { args: { as: "sheet" } };
