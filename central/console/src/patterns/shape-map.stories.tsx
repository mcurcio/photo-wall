import type { Meta, StoryObj } from "@storybook/react-vite";

import { BANDS, BINS, TICKS } from "./fit-board.fixtures";
import { ShapeMap } from "./shape-map";

const meta = {
  title: "Patterns/ShapeMap",
  component: ShapeMap,
  args: { bins: BINS, bands: BANDS, range: [0.25, 4], ticks: TICKS },
} satisfies Meta<typeof ShapeMap>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The varied library's shapes, and the band of ratios each of three Frames can take. */
export const Default: Story = {};

/** One Frame focused: its band in the accent. */
export const OneFrameFocused: Story = { args: { focused: "hall" } };

/** No photo yet. */
export const Empty: Story = { args: { bins: BINS.map((bin) => ({ ...bin, total: 0, count: "none" })) } };
