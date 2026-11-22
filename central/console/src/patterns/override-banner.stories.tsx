import type { Meta, StoryObj } from "@storybook/react-vite";

import { Button } from "../ui/button";
import { Inline } from "../ui/stack";
import { Note } from "./fact-row";
import { OverrideBanner } from "./override-banner";

const meta = {
  title: "Patterns/OverrideBanner",
  component: OverrideBanner,
  args: {
    kind: "power-hold",
    label: "Holds",
    title: "Held off until 23:00",
    children: (
      <Inline>
        <Note>Held off · console · until 23:00 · Bedroom, Hall</Note>
        <Button>Resume schedule</Button>
      </Inline>
    ),
  },
} satisfies Meta<typeof OverrideBanner>;

export default meta;
type Story = StoryObj<typeof meta>;

/** All off in force until Night off, with its way back. */
export const Default: Story = {};
/** Show now on top of the schedule, with its end and its way back. */
export const Takeover: Story = {
  args: {
    kind: "takeover",
    label: "Show now",
    title: "Showing “Holiday 2024” until 22:04",
    action: { label: "Back to normal", onAction: () => {} },
    children: <Note>On Living room left, Living room right</Note>,
  },
};
/** A Frame holding the photo it shows, with its end and its way back. */
export const Pause: Story = {
  args: {
    kind: "pause",
    label: "Paused",
    title: "Paused until 22:04",
    action: { label: "Carry on", onAction: () => {} },
    children: <Note>Hall holds its photo; the schedule keeps going underneath.</Note>,
  },
};
/** At phone width: the line wraps and the button stays whole. */
export const Phone: Story = { parameters: { phone: true } };
