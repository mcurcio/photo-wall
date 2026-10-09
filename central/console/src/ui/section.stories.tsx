import type { Meta, StoryObj } from "@storybook/react-vite";

import { Section } from "./section";

const meta = {
  title: "Primitives/Section",
  component: Section,
  args: { title: "Health", children: "Temperature 52 °C, last reported 3 s ago." },
} satisfies Meta<typeof Section>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Shown: Story = {};

function Drifted(): never {
  throw new Error("a served payload that drifted");
}

/** A section whose render throws says so; the rest of the page stays. */
export const Failed: Story = { args: { children: <Drifted /> } };
