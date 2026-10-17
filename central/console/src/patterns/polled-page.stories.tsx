import type { Meta, StoryObj } from "@storybook/react-vite";

import { Note } from "./fact-row";
import { PolledPage } from "./polled-page";

const meta = {
  title: "Patterns/PolledPage",
  component: PolledPage<string>,
  args: {
    read: { status: "read", value: "Overheads: On, 60 %", stale: false },
    what: "Central did not answer with the house's lights.",
    stale: "Central did not answer the last read: these are the lights as last read.",
    onRetry: () => undefined,
    children: (value: string) => <Note>{value}</Note>,
  },
} satisfies Meta<typeof PolledPage<string>>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Read: the page as Central answered. */
export const Read: Story = {};
/** Before the first answer. */
export const Loading: Story = { args: { read: { status: "loading" } } };
/** Central never answered: the problem card, with Try again. */
export const Failed: Story = { args: { read: { status: "failed" } } };
/** The last read failed: the one before, under a line saying so. */
export const Stale: Story = { args: { read: { status: "read", value: "Overheads: On, 60 %", stale: true } } };
