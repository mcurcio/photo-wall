import type { Meta, StoryObj } from "@storybook/react-vite";

import { Field, NumberInput } from "./field";
import { Select } from "./select";
import { Slider } from "./slider";
import { Switch } from "./switch";

const meta = {
  title: "Primitives/Field",
  component: Field,
  args: { label: "Top-left x", children: <NumberInput value={10} step={1} onChange={() => {}} /> },
} satisfies Meta<typeof Field>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A number typed in a Field. */
export const NumberKind: Story = { name: "Number" };

export const WithHelp: Story = {
  args: { label: "Trim left", help: "In percent of the screen.", children: <NumberInput value={2.5} onChange={() => {}} /> },
};

export const WithError: Story = {
  args: { label: "Pixel width", error: "A width is 1 to 16384 pixels.", children: <NumberInput value={0} onChange={() => {}} /> },
};

/** Slider: its value and unit as text beside it. */
export const SliderKind: Story = {
  name: "Slider",
  args: {
    label: "Brightness — Photo Wall picture adjustment",
    help: "Photo Wall brightens or dims the picture it sends; the Display's own settings do not change.",
    children: <Slider value={150} min={0} max={200} step={5} unit="%" onCommit={() => {}} />,
  },
};

/** Select: one choice from a list. */
export const SelectKind: Story = {
  name: "Select",
  args: {
    label: "Move",
    children: (
      <Select
        value="all"
        options={[{ value: "all", label: "Whole picture" }, { value: "0", label: "Top-left corner" }]}
        onChange={() => {}}
      />
    ),
  },
};

/** Switch: on or off, applied at once. */
export const SwitchKind: Story = {
  name: "Switch",
  args: { label: "Video capable", children: <Switch checked onChange={() => {}} /> },
};

export const Disabled: Story = {
  args: { label: "Brightness", children: <Slider value={100} min={0} max={200} step={5} unit="%" onCommit={() => {}} disabled /> },
};
