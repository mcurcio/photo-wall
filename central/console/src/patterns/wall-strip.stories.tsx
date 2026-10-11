import type { Meta, StoryObj } from "@storybook/react-vite";

import { HALL, LIVING, OFFICE, STAIRS } from "./fit-board.fixtures";
import { FrameFitTile, WallStrip } from "./wall-strip";

const meta = {
  title: "Patterns/WallStrip",
  component: WallStrip,
  args: { frames: [LIVING, STAIRS, HALL] },
} satisfies Meta<typeof WallStrip>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A Scene's Frames together: each with a different photo; one with a single photo, one with none. */
export const Wall: Story = {};
/** Every Frame has photos that fit. */
export const AllFit: Story = { args: { frames: [LIVING, OFFICE] } };
/** Small, under a line (Review, Show now). */
export const Small: Story = { args: { size: "small" } };
/** An older answer while a newer one is read. */
export const Faded: Story = { args: { faded: true } };

/** One tile as a Frame picker draws it (beside a checkbox that names it): its alert's chip only. */
export const PickerTile: Story = { render: () => <FrameFitTile frame={HALL} named={false} brief /> };
