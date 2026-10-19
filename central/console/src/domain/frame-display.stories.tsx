import type { Meta, StoryObj } from "@storybook/react-vite";

import { Section } from "../ui/section";
import { DisplayChangedCard, DisplayFacts, type DisplayView, type FrameDisplayView } from "./frame-display";

const DELL: DisplayView = {
  id: "3f0c2a8e-6d1b-4e9a-9a51-0d1f6c2b7e10",
  maker: "DEL",
  product: 41200,
  name: "DELL U2720Q",
  serial: "CN0F1X8Z",
  tied_to_frame: false,
  modes: [
    { width: 3840, height: 2160, refresh_millihertz: 60000, preferred: true },
    { width: 1920, height: 1080, refresh_millihertz: 60000, preferred: false },
  ],
  size_mm: [597, 336],
};

const PORTABLE: DisplayView = {
  id: "8b9e4f2d-1c3a-4b5e-8f7d-2a6c9e0b1d34",
  maker: "XYM",
  product: 5475,
  name: "MNN",
  serial: null,
  tied_to_frame: true,
  modes: [{ width: 1920, height: 1080, refresh_millihertz: 60000, preferred: true }],
  size_mm: null,
};

const CHANGED: FrameDisplayView = {
  frame_id: "lobby",
  readiness: "display-changed",
  player_id: "player-a",
  output_id: "HDMI-A-1",
  display: DELL,
  position_display: PORTABLE,
  connected: true,
  reported_at: 1_791_000_000,
};

const meta = {
  title: "Domain/FrameDisplay",
  component: DisplayChangedCard,
  args: { view: CHANGED, onRecheckPosition: () => {} },
  render: (args) => (
    <Section title="Display">
      <DisplayChangedCard view={args.view} onRecheckPosition={args.onRecheckPosition} />
    </Section>
  ),
} satisfies Meta<typeof DisplayChangedCard>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Another display on the Frame's HDMI port: re-check the Position on it. */
export const DisplayChanged: Story = {};
/** A display with a usable serial that states its size, as the Hardware tab shows it. */
export const WithSerial: Story = {
  render: () => <Section title="Display"><DisplayFacts display={DELL} /></Section>,
};
/** A display with no usable serial that states no size: recognised by make and model on this Frame. */
export const RecognisedOnThisFrame: Story = {
  render: () => <Section title="Display"><DisplayFacts display={PORTABLE} /></Section>,
};
