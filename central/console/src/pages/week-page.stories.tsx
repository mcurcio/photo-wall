import type { Meta, StoryObj } from "@storybook/react-vite";

import { HELD_WEEK, HOLD, NIGHT_OFF, NOW, WEEK } from "../domain/power-week.fixtures";
import { setHouseClock } from "../timeWords.js";
import { WeekView } from "./week-page";

const READ = {
  ok: true as const, week: WEEK, holds: [], holdLasts: "next-scheduled-change" as const, nightOff: NIGHT_OFF,
  zone: "Europe/London", placeSet: true,
};

const meta = {
  title: "Pages/Week",
  component: WeekView,
  args: {
    read: READ, now: NOW, frames: [{ id: "bedroom", name: "Bedroom" }], sceneIds: ["calm", "evening"], busy: false,
    said: null, sceneSaid: null, thisWeek: true, onWeek: () => {}, onHold: () => {}, onHoldLasts: () => {}, onResume: () => {},
    onDefaultScene: () => {}, onSaveNightOff: async () => ({ ok: true as const }),
  },
} satisfies Meta<typeof WeekView>;

export default meta;
type Story = StoryObj<typeof meta>;

const held = { read: { ...READ, week: HELD_WEEK, holds: [HOLD] }, said: "Holding every display off until 23:00." };
const empty = { read: { ...READ, week: { ...WEEK, windows: [] } } };

/** The week: Night off every night, Evenings Monday to Friday, the colour key and the now line. */
export const Default: Story = {};
/** All off in force: the hold banner with Resume schedule, the hatched hold on the lane. */
export const Held: Story = { args: held };
/** No Schedule this week: how to add one. */
export const NoSchedule: Story = { args: empty };
/** The house keeps a 12-hour clock (Settings › House): every time on the page follows it. */
export const TwelveHourClock: Story = {
  args: held,
  beforeEach: () => {
    setHouseClock({ zone: "Europe/London", clock: "12h" });
    return () => setHouseClock({ zone: null, clock: "24h" });
  },
};
/** At phone width: one day at a time, every control a 44 px target. */
export const Phone: Story = { parameters: { phone: true } };
/** At phone width, held. */
export const HeldPhone: Story = { args: held, parameters: { phone: true } };
