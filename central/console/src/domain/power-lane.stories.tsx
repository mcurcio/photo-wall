import type { Meta, StoryObj } from "@storybook/react-vite";

import { PowerLane, scheduleLane } from "./power-lane";
import { HELD_WEEK, HOLD, NAMES, NOW, WEEK } from "./power-week.fixtures";

const meta = {
  title: "Domain/PowerLane",
  component: PowerLane,
  args: {
    week: WEEK, names: NAMES, holds: [], now: NOW, below: [scheduleLane(WEEK)], calendarLabel: "Week of Mon 12 Oct",
    busy: false, onResume: () => {}, said: null,
  },
} satisfies Meta<typeof PowerLane>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Night off every night, Evenings Monday to Friday. */
export const NightOff: Story = {};
/** All off pressed: a hatched hold until 23:00, with Resume schedule. */
export const Held: Story = {
  args: { week: HELD_WEEK, below: [scheduleLane(HELD_WEEK)], holds: [HOLD], said: "Holding every display off until the next scheduled change." },
};
