import type { Meta, StoryObj } from "@storybook/react-vite";

import { QuadEditor } from "./quad-editor";

const meta = {
  title: "Patterns/QuadEditor",
  component: QuadEditor,
  args: {
    label: "Picture position",
    corners: [[0.05, 0.08], [0.95, 0.04], [0.93, 0.95], [0.06, 0.9]],
    crop: [0.02, 0, 1, 0.97],
    aspect: 16 / 9,
    selected: null,
    onSelect: () => {},
    onCorner: () => {},
    onCrop: () => {},
    onArrow: () => {},
  },
} satisfies Meta<typeof QuadEditor>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
export const CornerSelected: Story = { args: { selected: 0 } };
export const Disabled: Story = { args: { disabled: true } };
