import type { Meta, StoryObj } from "@storybook/react-vite";

import { WeekCalendar } from "./week-calendar";

const DAYS = [
  { date: "2026-10-12", label: "Mon 12 Oct" }, { date: "2026-10-13", label: "Tue 13 Oct" },
  { date: "2026-10-14", label: "Wed 14 Oct" }, { date: "2026-10-15", label: "Thu 15 Oct" },
  { date: "2026-10-16", label: "Fri 16 Oct" }, { date: "2026-10-17", label: "Sat 17 Oct" },
  { date: "2026-10-18", label: "Sun 18 Oct" },
];
const NIGHTS = DAYS.map((day, index) => ({
  key: `night-${day.date}`, start: `${day.date}T23:00`,
  end: index === 6 ? "2026-10-19T00:00" : `${DAYS[index + 1].date}T07:00`,
  name: `Off · Night off, ${day.label} 23:00`, short: "Night off", tone: "off" as const,
}));
const DAYTIME = DAYS.map((day) => ({
  key: `day-${day.date}`, start: `${day.date}T07:00`, end: `${day.date}T23:00`, name: `On, ${day.label} 07:00–23:00`,
  tone: "on" as const,
}));

const meta = {
  title: "Patterns/WeekCalendar",
  component: WeekCalendar,
  args: {
    label: "Week of Mon 12 Oct",
    days: DAYS,
    lanes: [
      { key: "power", label: "Power · All Frames", bands: [...DAYTIME, ...NIGHTS] },
      { key: "schedules", label: "Schedules", bands: DAYS.slice(0, 5).map((day) => ({
        key: day.date, start: `${day.date}T17:00`, end: `${day.date}T23:00`, name: `Evenings, ${day.label} 17:00–23:00`,
        short: "Evenings", tone: "window" as const })) },
    ],
    now: "2026-10-14T12:00",
    colourKey: [
      { tone: "on", label: "Displays on" }, { tone: "off", label: "Night off: displays off" },
      { tone: "held", label: "Held by All off or All on" }, { tone: "window", label: "A Schedule plays" },
    ],
  },
} satisfies Meta<typeof WeekCalendar>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A Power lane over the Schedules, with the now line on Wednesday. */
export const Default: Story = {};
/** A hold from Wednesday noon to Night off, hatched. */
export const Held: Story = {
  args: {
    lanes: [{ key: "power", label: "Power · All Frames", bands: [...NIGHTS, {
      key: "hold", start: "2026-10-14T12:00", end: "2026-10-14T23:00", name: "Held off · console, Wed 14 Oct 12:00–23:00",
      short: "Held off · console", tone: "held" as const }] }],
  },
};
/** Another week: no now line, nothing scheduled. */
export const Empty: Story = { args: { lanes: [{ key: "schedules", label: "Schedules", bands: [] }], now: null } };
/** At phone width: one day at a time (today's, Wednesday), chosen from seven day buttons. */
export const Phone: Story = { parameters: { phone: true } };
