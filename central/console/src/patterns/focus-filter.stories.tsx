import type { Meta, StoryObj } from "@storybook/react-vite";

import { FocusFilter, FocusLink } from "./focus-filter";

const meta = {
  title: "Patterns/FocusFilter",
  component: FocusFilter,
  args: { focused: "Player …a1b2c3", noun: "Pi", clearHref: "#/hardware" },
} satisfies Meta<typeof FocusFilter>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Focused: Story = {};
/** Not focused: nothing shows. */
export const NotFocused: Story = { args: { focused: null } };
/** The link that sets the focus, as a row carries it. */
export const Link: Story = {
  render: () => <FocusLink href="#/hardware?pi=device-a" name="Player …a1b2c3" />,
};
