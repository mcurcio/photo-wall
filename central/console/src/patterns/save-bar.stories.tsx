import type { Meta, StoryObj } from "@storybook/react-vite";

import { Button } from "../ui/button";
import { SaveBar } from "./save-bar";

const meta = {
  title: "Patterns/SaveBar",
  component: SaveBar,
  args: {
    label: "Source actions",
    children: (
      <>
        <Button variant="primary">Save Photo source</Button>
        <Button variant="ghost">Discard changes</Button>
      </>
    ),
  },
} satisfies Meta<typeof SaveBar>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};

/** A refused Save says why beside the buttons. */
export const Refused: Story = { args: { message: "Name this Source before saving it." } };
