import type { Meta, StoryObj } from "@storybook/react-vite";

import { TRUTH_KINDS } from "../design/tokens";
import { FactGroup, FactRow, Note } from "./fact-row";

const meta = {
  title: "Patterns/FactRow",
  component: FactRow,
  args: { label: "Temperature", tone: "reported", children: "Host Management last reported 3 s ago · 52 °C" },
} satisfies Meta<typeof FactRow>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Reported: Story = {};
export const Unknown: Story = {
  args: { label: "CPU", tone: "unknown", children: "Unknown: not read" },
};
export const Alarmed: Story = {
  args: { label: "Throttling", tone: "reported", band: "alarm", children: "Throttled now" },
};
export const Unlabelled: Story = { args: { label: undefined, tone: "set", children: "Bound to Frame lobby" } };

/** Every truth kind's tone, in a group with a note. */
export const Kinds: Story = {
  render: () => (
    <FactGroup title="Truth kinds">
      {TRUTH_KINDS.map((kind) => (
        <FactRow key={kind} label={kind} tone={kind}>{`A ${kind} fact`}</FactRow>
      ))}
      <Note>Read as of 10:04:12</Note>
    </FactGroup>
  ),
};
