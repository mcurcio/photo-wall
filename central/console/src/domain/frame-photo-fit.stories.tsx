import type { Meta, StoryObj } from "@storybook/react-vite";

import { type PhotoFitEntry, PhotoFitPanel } from "./frame-photo-fit";

const HOLIDAY: PhotoFitEntry = {
  sceneId: "holiday-scene",
  sourceRef: "holiday:1",
  cropLimit: 0.35,
  read: {
    status: "read",
    ready: true,
    fits: 412,
    skipped: { beyond_crop_limit: 88, below_frame_pixels: 6, face_cut: 3, interest_cut: 11, no_video_here: 0,
      not_measured: 0 },
  },
};

const meta = {
  title: "Domain/FramePhotoFit",
  component: PhotoFitPanel,
  args: { entries: [HOLIDAY] },
} satisfies Meta<typeof PhotoFitPanel>;

export default meta;
type Story = StoryObj<typeof meta>;

/** One Scene's Source: how many photos fit, then each reason the rest are left off. */
export const Reasons: Story = {};
/** Two Scenes on this Frame: one still measuring a Source whose first check runs, one whose read
 * failed. */
export const Several: Story = {
  args: {
    entries: [
      { sceneId: "garden", sourceRef: "spring:2", cropLimit: 0.4,
        read: { status: "read", ready: false, fits: 0, skipped: { not_measured: 24 } } },
      HOLIDAY,
      { sceneId: "kitchen", sourceRef: "family:1", cropLimit: null, read: { status: "failed", error: "unreachable" } },
    ],
  },
};
/** No Scene shows a Source's photos on this Frame. */
export const NoSources: Story = { args: { entries: [] } };
