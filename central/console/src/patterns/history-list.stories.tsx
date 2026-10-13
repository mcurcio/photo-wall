import type { Meta, StoryObj } from "@storybook/react-vite";

import { ProblemCard } from "./problem-card";
import { type HistoryDay, HistoryList } from "./history-list";

const ENABLED = { disabled: false };

const DAYS: readonly HistoryDay[] = [
  {
    label: "Tue 13 Oct",
    rows: [
      {
        id: "c", time: "21:04:07", what: "Tried, not kept", notKept: true, summary: "Brightness 120 %",
        details: [{ label: "Brightness", value: "120 %" }, { label: "Contrast", value: "100 %" }],
        restore: ENABLED,
      },
      {
        id: "b", time: "20:58:31", what: "Kept (Done)", notKept: false, summary: "Brightness 110 % · Gamma 2.4",
        details: [{ label: "Brightness", value: "110 %" }, { label: "Gamma", value: "2.4" }],
        restore: ENABLED,
      },
    ],
  },
  {
    label: "Mon 12 Oct",
    rows: [
      {
        id: "a", time: "22:10:02", what: "Put back", notKept: true,
        summary: "Brightness 100 % · Contrast 100 % · Gamma 2.2",
        details: [{ label: "Brightness", value: "100 %" }, { label: "Gamma", value: "2.2" }],
        restore: ENABLED,
      },
    ],
  },
];

const meta = {
  title: "Patterns/HistoryList",
  component: HistoryList,
  args: {
    title: "History",
    intro: "Every change, newest first. Restore puts one back in the editor; press Done to keep it.",
    days: DAYS,
    empty: "No changes yet.",
    busy: false,
    restoring: null,
    onRestore: () => {},
  },
} satisfies Meta<typeof HistoryList>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Nothing recorded yet. */
export const Empty: Story = { args: { days: [] } };
/** The first read in flight: a skeleton, `aria-busy`. */
export const Loading: Story = { args: { days: [], busy: true } };
/** Kept rows and rows not kept (dashed, muted), over two days, with older ones to load. */
export const Entries: Story = { args: { onOlder: () => {} } };
/** A line above the days. */
export const Notice: Story = { args: { notice: "These values changed outside the editor since the last kept change." } };
/** No editor to restore into: every Restore disabled, the shared reason said once. */
export const RestoreDisabled: Story = {
  args: {
    days: DAYS.map((day) => ({
      ...day, rows: day.rows.map((row) => ({ ...row, restore: { disabled: true, reason: "Open while the editor runs" } })),
    })),
  },
};
/** One row being restored: every Restore waits. */
export const Restoring: Story = { args: { restoring: "b" } };
/** The read failed. */
export const Problem: Story = {
  args: {
    days: [],
    problem: (
      <ProblemCard scope="central" variant="inline" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="The server did not answer with this history." doing={null} action={{ label: "Retry", onAction: () => {} }} />
    ),
  },
};
/** A phone's width: each row's time, words and Restore stack. */
export const Phone: Story = { parameters: { phone: true }, args: { onOlder: () => {} } };
