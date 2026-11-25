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
/** Never show's answer: what it came to, with Undo. */
export const NeverShow: Story = { args: { message: "Won't show again. It shows once more on Hall, where it's already downloaded." } };
/** An answer with no way back (a wall's Skip): the same line, no Undo. */
export const NoUndo: Story = { args: { message: "Skipped on 4 Frames. Hall stayed: its photo is hand-placed.", onUndo: undefined } };
