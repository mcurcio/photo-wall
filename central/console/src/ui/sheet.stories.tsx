import type { Meta, StoryObj } from "@storybook/react-vite";

import { Sheet } from "./sheet";

const meta = {
  title: "Primitives/Sheet",
  component: Sheet,
  args: {
    open: true,
    onOpenChange: () => {},
    title: "Photos in this Source",
    children: <p className="m-0">2,301 photos · 14 videos</p>,
  },
} satisfies Meta<typeof Sheet>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Above `md` it comes from the side (the catalog renders at 800 px); below, from the bottom. */
export const Default: Story = {};
