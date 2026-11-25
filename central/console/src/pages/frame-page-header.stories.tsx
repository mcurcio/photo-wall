import type { Meta, StoryObj } from "@storybook/react-vite";

import { FramePageHeader } from "./frame-page-header";

const meta = {
  title: "Pages/FramePageHeader",
  component: FramePageHeader,
  args: {
    name: "Living room left", chip: { severity: "ok", label: "Showing" }, wall: null,
    back: { text: "Wall", href: "#/wall" }, onRename: async () => ({ ok: true as const }),
  },
} satisfies Meta<typeof FramePageHeader>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Showing, on the house's one wall. */
export const Showing: Story = {};
/** In Night off: Central's status says Resting, so the chip says Resting. */
export const Resting: Story = { args: { chip: { severity: "ok", label: "Resting" }, wall: "Upstairs" } };
/** At phone width, Resting. */
export const RestingPhone: Story = { args: { chip: { severity: "ok", label: "Resting" } }, parameters: { phone: true } };
/** Slice 3: the Frame's own everyday actions behind More, only those Central offers now. */
export const WithActions: Story = {
  args: {
    menu: {
      label: "More actions for Living room left",
      items: [
        { label: "Skip this Frame", onAction: () => {} },
        { label: "Previous on this Frame", onAction: () => {} },
        { label: "Pause for 1 hour", onAction: () => {} },
        { label: "Never show this photo", onAction: () => {} },
        { label: "Why is this playing?", href: "#/wall/frames/living-left/overview" },
      ],
    },
  },
};
