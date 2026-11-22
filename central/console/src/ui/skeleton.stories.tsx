import type { Meta, StoryObj } from "@storybook/react-vite";

import { Skeleton } from "./skeleton";

const meta = {
  title: "Primitives/Skeleton",
  component: Skeleton,
} satisfies Meta<typeof Skeleton>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A block in the shape of a tile or a card. */
export const Block: Story = { render: () => <div className="h-32 w-64"><Skeleton /></div> };
/** One line of text. */
export const Line: Story = { args: { shape: "line" } };
