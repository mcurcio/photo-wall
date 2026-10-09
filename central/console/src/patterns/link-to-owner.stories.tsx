import type { Meta, StoryObj } from "@storybook/react-vite";

import { LinkToOwner, OwnerLinks } from "./link-to-owner";

const meta = {
  title: "Patterns/LinkToOwner",
  component: LinkToOwner,
  args: { text: "Player …a1b2c3 · Host Management last reported 3 s ago", href: "#/hardware/device-a", severity: "ok" },
} satisfies Meta<typeof LinkToOwner>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Ok: Story = {};
export const Alarm: Story = { args: { text: "Player …a1b2c3 · throttled now", severity: "alarm" } };
export const Notice: Story = { args: { text: "Player …a1b2c3 · warm", severity: "notice" } };
export const Todo: Story = { args: { text: "Frame lobby-left · needs calibration", severity: "todo" } };
/** A link that only navigates: no severity, so no mark and the neutral border. */
export const Plain: Story = { args: { text: "Frame lobby-left", severity: undefined } };
export const Unknown: Story = { args: { text: "Player …a1b2c3 · host health not read", severity: "unknown" } };

/** A Pi's bound Frames, each a link to its Binding on the Wall. */
export const Several: Story = {
  render: () => (
    <OwnerLinks
      label="Bound Frames:"
      links={[
        { text: "Frame lobby-left", href: "#/wall/frames/lobby-left/binding" },
        { text: "Frame lobby-right", href: "#/wall/frames/lobby-right/binding" },
      ]}
    />
  ),
};
