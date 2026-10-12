import type { Meta, StoryObj } from "@storybook/react-vite";

import { FileChooser } from "./file-chooser";

const meta = {
  title: "Primitives/FileChooser",
  component: FileChooser,
  args: { children: "Add a paper profile", accept: ".icc,.icm", onChoose: () => undefined },
} satisfies Meta<typeof FileChooser>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Outline: Story = {};
export const Primary: Story = { args: { variant: "primary" } };
export const Disabled: Story = { args: { disabled: true, children: "Adding…" } };
