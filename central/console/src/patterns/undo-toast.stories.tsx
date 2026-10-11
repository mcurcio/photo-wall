import type { Meta, StoryObj } from "@storybook/react-vite";

import { UndoToast } from "./undo-toast";

const meta = {
  title: "Patterns/UndoToast",
  component: UndoToast,
  args: { message: "Deleted Source garden.", onUndo: () => {}, onExpire: () => {}, seconds: 3600 },
} satisfies Meta<typeof UndoToast>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
