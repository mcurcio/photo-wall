import type { Meta, StoryObj } from "@storybook/react-vite";

import { HALL, LIVING, OFFICE, STAIRS } from "./fit-board.fixtures";
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

/** A 9:16 Frame nothing fits: drawn empty, its alert said once with what to do, and its
 * host's raise. */
export const NoneFit: Story = {
  args: { frame: { ...HALL, alert: { ...HALL.alert!, action: { label: "Raise the crop limit to 58 %", onAction: () => {} } } } },
};

/** A Frame with a single photo, shared with others: a notice, not an alarm. */
export const FewPhotos: Story = { args: { frame: STAIRS } };

/** Under a wall strip that draws the tile and its alert: the counts, photos and reasons only. */
export const UnderWall: Story = { args: { frame: HALL, tile: false } };

/** On a Frame's own page: its tile, large, and the photos it would show lead. */
export const Pictures: Story = { args: { layout: "pictures", named: false } };

/** A Frame whose Pi has not reported its display's size: judged by shape only. */
export const Unmeasured: Story = { args: { frame: OFFICE } };

/** Compact: the counts and the bar, no photos. */
export const Compact: Story = { args: { size: "compact" } };

/** A Frame judged with unsaved values. */
export const Draft: Story = { args: { frame: { ...LIVING, draft: "As you are setting it" }, size: "compact" } };

/** On a Frame's own page the host names the Frame: the row leads with its count. */
export const Unnamed: Story = { args: { size: "compact", named: false } };
