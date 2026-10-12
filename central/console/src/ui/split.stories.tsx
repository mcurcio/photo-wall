import type { Meta, StoryObj } from "@storybook/react-vite";

import { Button } from "./button";
import { Split } from "./split";

const meta = {
  title: "Primitives/Split",
  component: Split,
  args: {
    children: <span className="text-sm text-text">Fits 2 of 2 Frames · Only 1 photo fits hall</span>,
    action: <Button variant="ghost">More</Button>,
  },
} satisfies Meta<typeof Split>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Content and its action on one line, the action at the end. */
export const Default: Story = {};

/**
 * At phone width (384 px), under a container that lets text wrap anywhere (the legacy regions
 * do): the long content wraps between its words and the action stays whole, "More", never
 * "Mor / e". The browser test reads that each action's words sit on one line.
 */
export const LongTitlePhone: Story = {
  render: () => (
    <div className="flex w-96 flex-col gap-4 wrap-anywhere">
      <Split action={<Button variant="ghost">More</Button>}>
        <span className="text-base font-medium text-text">favourite-landscapes-from-the-summer-holidays</span>
      </Split>
      <Split action={<span className="text-sm text-accent">Show ›</span>}>
        <span className="text-sm text-text">8 photos · 0 videos · Fits 2 of 2 Frames · Only 1 photo fits hall</span>
      </Split>
    </div>
  ),
};

/** Narrower than the content's readable width and the action together: the action moves to a
 * row of its own, at the end. */
export const Narrow: Story = {
  render: () => (
    <div className="w-56 wrap-anywhere">
      <Split action={<span className="text-sm text-accent">Show ›</span>}>
        <span className="text-sm text-text">Fits 2 of 2 Frames · Only 1 photo fits hall</span>
      </Split>
    </div>
  ),
};
