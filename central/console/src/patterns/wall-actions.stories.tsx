import type { Meta, StoryObj } from "@storybook/react-vite";

import { WallActions } from "./wall-actions";

const meta = {
  title: "Patterns/WallActions",
  component: WallActions,
  args: {
    wall: "Living room wall",
    actions: [
      { key: "next", label: "Skip", name: "Skip every Frame on Living room wall", onAction: () => {} },
      { key: "previous", label: "Previous", name: "Previous on every Frame on Living room wall", onAction: () => {} },
    ],
  },
} satisfies Meta<typeof WallActions>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Skip and Previous on a whole wall, each named for every Frame it moves. */
export const Default: Story = {};
/** Only Skip is offered (no Frame of the wall has an earlier photo): Previous is absent, not disabled. */
export const SkipOnly: Story = { args: { actions: [{ key: "next", label: "Skip", name: "Skip every Frame on Living room wall", onAction: () => {} }] } };
/** A press is on its way to Central: both wait. */
export const Busy: Story = { args: { busy: true } };
/** At phone width: the buttons are 44 px targets and wrap. */
export const Phone: Story = { parameters: { phone: true } };
