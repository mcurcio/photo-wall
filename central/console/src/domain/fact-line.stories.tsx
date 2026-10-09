import type { Meta, StoryObj } from "@storybook/react-vite";

import { fact } from "../facts.js";
import { FactLine } from "./fact-line";

const meta = {
  title: "Domain/FactLine",
  component: FactLine,
  args: { label: "Standing", fact: fact({ kind: "set", value: "Bound" }) },
} satisfies Meta<typeof FactLine>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Set: Story = {};
export const Reported: Story = {
  args: { label: "Host Management", fact: fact({ kind: "reported", source: "Host Management", receipt: "latest",
    receivedAt: 997, readAt: 1000 }) },
};
export const Claimed: Story = {
  args: { label: "Serial", fact: fact({ kind: "claimed", value: "Serial 10000000a1b2c3", source: "the box" }) },
};
export const Derived: Story = {
  args: { label: "Node API link", fact: fact({ kind: "derived", value: "linked for 10 min",
    basis: "Central's hub holds this Node's leaf" }) },
};
export const Unknown: Story = { args: { label: "CPU", fact: fact({ kind: "unknown", why: "not read" }) } };
export const Banded: Story = {
  args: { label: "Temperature", band: "alarm", suffix: "hot",
    fact: fact({ kind: "reported", source: "Host Management", receipt: "latest", value: "82 °C",
      receivedAt: 999, readAt: 1000 }) },
};
