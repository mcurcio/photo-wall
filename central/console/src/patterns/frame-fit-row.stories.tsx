import type { Meta, StoryObj } from "@storybook/react-vite";

import { Button } from "../ui/button";
import { HALL, LIVING, OFFICE } from "./fit-board.fixtures";
import { FrameFitRow } from "./frame-fit-row";

const meta = {
  title: "Patterns/FrameFitRow",
  component: FrameFitRow,
  args: { frame: LIVING, size: "full", onOpen: () => {} },
} satisfies Meta<typeof FrameFitRow>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A 16:9 Frame: what it would show, how many fit, a bar of why the rest don't, photos cropped. */
export const Ready: Story = {};

/** A 9:16 Frame nothing fits: drawn empty, with one action from its host. */
export const NoneFit: Story = {
  args: { frame: HALL, action: <Button className="w-fit">Raise the crop limit to 58 %</Button> },
};

/** A Frame whose Pi has not reported its display's size: judged by shape only. */
export const Unmeasured: Story = { args: { frame: OFFICE } };

/** Compact: the counts and the bar, no photos. */
export const Compact: Story = { args: { size: "compact" } };

/** A Frame judged with unsaved values. */
export const Draft: Story = { args: { frame: { ...LIVING, draft: "As you are setting it" }, size: "compact" } };
