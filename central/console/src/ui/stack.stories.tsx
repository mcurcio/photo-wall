import type { Meta, StoryObj } from "@storybook/react-vite";

import { Button } from "./button";
import { Inline, Stack } from "./stack";

const meta = {
  title: "Primitives/Stack",
  component: Stack,
  args: {
    children: (
      <>
        <p className="m-0">The first block.</p>
        <p className="m-0">The second block.</p>
      </>
    ),
  },
} satisfies Meta<typeof Stack>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Stack: blocks one under the other. */
export const Default: Story = {};

/** Inline: controls side by side, wrapping on a narrow screen. */
export const InlineRow: Story = {
  name: "Inline",
  render: () => (
    <Inline label="Answers">
      <Button variant="primary">Done</Button>
      <Button>Revert</Button>
    </Inline>
  ),
};
