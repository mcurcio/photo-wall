import type { Meta, StoryObj } from "@storybook/react-vite";

import { Link } from "./link";

const meta = {
  title: "Primitives/Link",
  component: Link,
  args: { href: "#/wall", children: "Wall" },
} satisfies Meta<typeof Link>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
