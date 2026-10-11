import type { Meta, StoryObj } from "@storybook/react-vite";

import { Field } from "./field";
import { MultiToggle } from "./multi-toggle";

const OPTIONS = [
  { value: "photo", label: "Photos" },
  { value: "live_photo", label: "Live photos" },
  { value: "video", label: "Videos" },
] as const;

const meta = {
  title: "Primitives/MultiToggle",
  component: MultiToggle,
  args: { label: "Media", value: ["photo", "video"], options: OPTIONS, onChange: () => {}, required: true },
} satisfies Meta<typeof MultiToggle>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};

export const AllOn: Story = { args: { value: ["photo", "live_photo", "video"] } };

/** Inside a Field, the Field's label names the group and its help describes it. */
export const InField: Story = {
  render: (args) => (
    <Field label="Media" help="Which kinds of item this Source takes.">
      <MultiToggle {...args} />
    </Field>
  ),
};

export const Disabled: Story = { args: { disabled: true } };
