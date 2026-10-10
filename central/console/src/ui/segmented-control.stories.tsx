import type { Meta, StoryObj } from "@storybook/react-vite";
import * as React from "react";

import { SegmentedControl, type SegmentedControlProps } from "./segmented-control";

type Ending = "none" | "black" | "fade";
const OPTIONS = [
  { value: "none", label: "Stops" },
  { value: "black", label: "Black" },
  { value: "fade", label: "Fades out" },
] as const;

function Controlled(args: SegmentedControlProps<Ending>) {
  const [value, setValue] = React.useState(args.value);
  return <SegmentedControl {...args} value={value} onValueChange={setValue} />;
}

const meta = {
  title: "Primitives/SegmentedControl",
  component: Controlled,
  args: { label: "How it ends", value: "none", options: OPTIONS, onValueChange: () => {} },
} satisfies Meta<typeof Controlled>;

export default meta;
type Story = StoryObj<typeof meta>;

export const FirstChosen: Story = {};
export const LastChosen: Story = { args: { value: "fade" } };
export const WithHint: Story = {
  args: { value: "black", hint: "Black covers these Frames for the ending's length." },
};
export const Disabled: Story = { args: { disabled: true } };
