import type { Meta, StoryObj } from "@storybook/react-vite";

import { PowerLane, scheduleLane } from "./power-lane";
import { HELD_WEEK, NAMES, NOW, WEEK } from "./power-week.fixtures";

const meta = {
  title: "Domain/PowerLane",
  component: PowerLane,
  args: { week: WEEK, names: NAMES, now: NOW, below: [scheduleLane(WEEK)], calendarLabel: "Week of Mon 12 Oct" },
} satisfies Meta<typeof PowerLane>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Night off every night, Evenings Monday to Friday, with the colour key. */
export const NightOff: Story = {};
/** All off pressed: a hatched hold until 23:00. */
export const Held: Story = { args: { week: HELD_WEEK, below: [scheduleLane(HELD_WEEK)] } };
/** At phone width: one day at a time (today's), chosen from the seven day buttons. */
export const Phone: Story = {
  args: { week: HELD_WEEK, below: [scheduleLane(HELD_WEEK)] },
  parameters: { phone: true },
};
