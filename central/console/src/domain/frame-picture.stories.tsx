import type { Meta, StoryObj } from "@storybook/react-vite";

import { FramePicture, type PictureSession } from "./frame-picture";

type View = NonNullable<PictureSession["view"]>;

const VIEW: View = {
  frame_id: "lobby",
  status: "shown",
  display_id: "8b9e4f2d-1c3a-4b5e-8f7d-2a6c9e0b1d34",
  brightness: { acts_on: "display", value: 60, answered: true, read: 60, result: "taken" },
  contrast: { acts_on: "picture-adjustment", value: 40, answered: false, read: null, result: null },
  colour_temperature_kelvin: 6500,
  gamma_tenths: 22,
  connected: true,
  session: { session_id: "5b7f0c1e-2d4a-4f6b-9c8e-1a2b3c4d5e6f", sequence: 1, idle_until: 1_791_000_005 },
};

const SESSION: PictureSession = {
  view: VIEW,
  phase: "shown",
  draft: { values: {}, places: {} },
  dirty: false,
  change: () => {},
  place: () => {},
  done: async () => true,
  revert: async () => {},
  busy: false,
  error: null,
  unavailable: null,
  waitingFor: null,
  latest: 0,
  savedAt: null,
  leftAt: null,
};

const meta = {
  title: "Domain/FramePicture",
  component: FramePicture,
  args: { frameId: "lobby", piHref: "#/players/device-a", session: SESSION, otherFrames: ["hall", "stairs"] },
} satisfies Meta<typeof FramePicture>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Brightness on the display, contrast as picture adjustment: nothing changed yet. */
export const Shown: Story = {};
/** A brightness change the Pi has not shown yet: Done waits. */
export const Waiting: Story = {
  args: { session: { ...SESSION, phase: "waiting", dirty: true, latest: 1, draft: { values: { brightness: 75 }, places: {} } } },
};
/** The Pi showed the latest change: Done is offered. */
export const ChangeShown: Story = {
  args: {
    session: {
      ...SESSION, dirty: true, latest: 1, draft: { values: { brightness: 75 }, places: {} },
      view: { ...VIEW, brightness: { ...VIEW.brightness, value: 75, read: 75 } },
    },
  },
};
/** The display read back another value than the one written. */
export const NotTaken: Story = {
  args: { session: { ...SESSION, view: { ...VIEW, brightness: { ...VIEW.brightness, read: 20, result: "not-taken" } } } },
};
/** Someone used the display's own buttons since. */
export const ChangedOnTheButtons: Story = {
  args: { session: { ...SESSION, view: { ...VIEW, brightness: { ...VIEW.brightness, read: 35 } } } },
};
/** Nobody set brightness or contrast: nothing is written to the display. */
export const AsTheDisplayHasIt: Story = {
  args: {
    session: {
      ...SESSION,
      view: {
        ...VIEW,
        brightness: { ...VIEW.brightness, value: null, read: 52, result: null },
        contrast: { ...VIEW.contrast, value: null },
      },
    },
  },
};
/** The display is not connected: the values apply when it is. */
export const NotConnected: Story = { args: { session: { ...SESSION, view: { ...VIEW, connected: false } } } };
/** The Pi has not reported a display on this port: no session. */
export const NoDisplay: Story = {
  args: {
    session: {
      ...SESSION, view: { ...VIEW, status: "no-display", display_id: null }, phase: "idle", latest: null,
      unavailable: { words: "The Pi has not reported a display on this HDMI port yet, so its picture waits until it does.",
        code: "no_display" },
    },
  },
};
/** Someone else is adjusting this display's picture. */
export const SomeoneElse: Story = {
  args: {
    session: {
      ...SESSION, phase: "idle", latest: null,
      waitingFor: "Someone else is adjusting this display's picture. Photo Wall connects as soon as they finish.",
    },
  },
};
/** A refused value. */
export const Refused: Story = {
  args: {
    session: {
      ...SESSION,
      error: { words: "The display did not answer the Pi's check, so this setting stays a Photo Wall picture adjustment.",
        code: "display_did_not_answer" },
    },
  },
};
