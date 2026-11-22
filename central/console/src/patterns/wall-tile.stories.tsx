import type { Meta, StoryObj } from "@storybook/react-vite";

import { WallTile } from "./wall-tile";

const MENU = {
  label: "More actions for Living room left",
  as: "popup" as const,
  items: [
    { label: "Why is this playing?", href: "#/wall/frames/living-left/overview" },
    { label: "Open Frame", href: "#/wall/frames/living-left/overview" },
  ],
};
const PI_MENU = { ...MENU, items: [{ label: "Restart app", onAction: () => {} }, { label: "Reboot the Pi", onAction: () => {} }, ...MENU.items] };

const meta = {
  title: "Patterns/WallTile",
  component: WallTile,
  args: {
    name: "Living room left",
    href: "#/wall/frames/living-left/overview",
    chip: { severity: "ok", label: "Showing" },
    line: "Evenings · until 23:00",
    menu: MENU,
  },
  decorators: [(Story) => <div className="w-56"><Story /></div>],
} satisfies Meta<typeof WallTile>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Presented by its Pi: what plays and until when. */
export const Showing: Story = {};
/** Dark on purpose: why, and until when. */
export const Resting: Story = { args: { chip: { severity: "ok", label: "Resting" }, line: "Night off until 07:00" } };
/** Should be showing and is not: what is wrong, naming its part; the fix is offered in its menu. */
export const NotShowing: Story = {
  args: { chip: { severity: "alarm", label: "Not showing" }, line: "The photo app is not running on the Pi.", menu: PI_MENU },
};
/** No report: when Central last heard its Pi, never a guess. */
export const CantTell: Story = {
  args: { chip: { severity: "unknown", label: "Can't tell" }, line: "Can't reach Pi 3f2a9c since 21:04. It may be off, or its network may be down.", menu: PI_MENU },
};
/** Playing, with something to look at. */
export const NeedsALook: Story = {
  args: { chip: { severity: "notice", label: "Needs a look" }, line: "The photo library (Immich) this Frame's Scene reads needs a look." },
};
/** On its way by itself: the step and its count. */
export const GettingReady: Story = {
  args: { chip: { severity: "notice", label: "Getting ready" }, line: "Preparing photos · 3 of 12" },
};
/** Still in setup: its step on a `todo` chip, and no actions (the tile opens its page). */
export const Setup: Story = {
  args: { chip: { severity: "todo", label: "Check its Position" }, line: "Check where the picture sits on its display", menu: undefined },
};
/** An action asked of its Pi and not acknowledged yet: the action is no longer offered. */
export const Requested: Story = {
  args: { chip: { severity: "alarm", label: "Not showing" }, line: "The photo app has stopped responding.", requested: "Restart requested" },
};
/** The last read is not current: greyed, with since when. */
export const Stale: Story = { args: { stale: "Can't tell since 21:04" } };
/** At phone width, two across: half the width, and its menu opens as a sheet. */
export const Phone: Story = {
  args: { menu: { ...PI_MENU, as: "sheet" }, chip: { severity: "alarm", label: "Not showing" }, line: "The photo app is not running on the Pi." },
  decorators: [(Story) => <div className="w-44"><Story /></div>],
};
