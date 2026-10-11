import type { Meta, StoryObj } from "@storybook/react-vite";

import { CropThumb, FrameTile } from "./crop-thumb";
import { crop } from "./fit-board.fixtures";

const meta = {
  title: "Patterns/CropThumb",
  component: CropThumb,
  args: { sample: crop("fits", 3, 2, 16 / 9, 0.35), height: 120 },
} satisfies Meta<typeof CropThumb>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A 3:2 photo a 16:9 Frame shows: the window clear, the cut-away dimmed. */
export const Fits: Story = {};

/** A 4:3 photo a 16:9 Frame would cut through a face (the face near the top): dashed window. */
export const RefusedFace: Story = {
  args: { sample: crop("face", 4, 3, 16 / 9, 0.5, "a face would be cut", true, [0.5, 0.12]) },
};

/** A 3:2 photo a 9:16 Frame cannot take: the centred window, dashed. */
export const RefusedShape: Story = {
  args: { sample: crop("shape", 3, 2, 9 / 16, 0.5, "doesn't fit this Frame's shape", true) },
};

/** A video, marked in words. */
export const Video: Story = {
  args: { sample: { ...crop("video", 16, 9, 4 / 3), video: true, alt: "Video, 16:9" } },
};

/** It opens the photo's sheet when pressed. */
export const Openable: Story = { args: { onOpen: () => {} } };

/** A Frame drawn at its shape, filled with what it would show; and an empty one. */
export const Tiles: Story = {
  render: () => (
    <div className="flex gap-4">
      <FrameTile aspect={16 / 9} lead={crop("tile", 3, 2, 16 / 9, 0.35)} label="living-room, 16:9" />
      <FrameTile aspect={9 / 16} lead={null} label="hall, 9:16" />
    </div>
  ),
};
