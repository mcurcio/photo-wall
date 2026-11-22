import type { Meta, StoryObj } from "@storybook/react-vite";

import { BottomTabs, HouseWord } from "./app-shell";

const meta = {
  title: "Patterns/AppShell",
  component: BottomTabs,
  args: {
    label: "Main",
    tabs: [
      { key: "home", label: "Home", icon: "home", href: "#/home", current: true },
      { key: "show", label: "Show now", icon: "play", href: "#/now/show/scene", current: false },
      { key: "schedule", label: "Schedule", icon: "calendar", href: "#/schedule", current: false },
      { key: "more", label: "More", icon: "more", onOpen: () => {}, expanded: false },
    ],
  },
} satisfies Meta<typeof BottomTabs>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * The phone's bottom tabs, drawn in place here (in the app they are pinned to the bottom of the
 * screen below the switch and absent above it).
 */
export const BottomTabsPhone: Story = {
  parameters: { phone: true },
  args: { pinned: false },
};
/** The house word in the top bar: all good. */
export const HouseWordAllGood: Story = {
  render: () => <HouseWord verdict={{ severity: "ok", label: "All good", receipt: null }} href="#/home" />,
};
/** The house word when a Frame is not showing. */
export const HouseWordNotShowing: Story = {
  render: () => <HouseWord verdict={{ severity: "alarm", label: "Not showing", receipt: null }} href="#/home" />,
};
