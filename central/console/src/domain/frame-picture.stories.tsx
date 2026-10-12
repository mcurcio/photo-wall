import type { Meta, StoryObj } from "@storybook/react-vite";

import { FramePicture, type PictureSession } from "./frame-picture";

type View = NonNullable<PictureSession["view"]>;
type Row = View["hardware"][number];

const LEVEL_100 = { kind: "level" as const, maximum: 100, choices: [] };
const BRIGHTNESS: Row = {
  code: "10", name: "Brightness", value: 46,
  offered: { range: LEVEL_100, current: 46 }, result: { result: "taken", before: 60, after: 46 },
  changed_on_buttons: false,
};
const CONTRAST: Row = {
  code: "12", name: "Contrast", value: null, offered: { range: LEVEL_100, current: 50 }, result: null,
  changed_on_buttons: false,
};
const PRESET: Row = {
  code: "14", name: "Select color preset", value: 5, result: { result: "taken", before: 4, after: 5 },
  changed_on_buttons: false,
  offered: {
    range: { kind: "choice", maximum: null, choices: [{ value: 4, name: "5000 K" }, { value: 5, name: "6500 K" }] },
    current: 5,
  },
};
const MODE: Row = {
  code: "DC", name: "Display mode", value: null, result: null, changed_on_buttons: false,
  offered: { range: { kind: "read-only-choice", maximum: null, choices: [] }, current: 2 },
};

const VIEW: View = {
  frame_id: "lobby",
  status: "shown",
  display_id: "8b9e4f2d-1c3a-4b5e-8f7d-2a6c9e0b1d34",
  connected: true,
  offered_known: true,
  hardware: [BRIGHTNESS, CONTRAST, PRESET, MODE],
  software: { brightness_percent: 114, contrast_percent: 100, colour_temperature_kelvin: 6500, gamma_tenths: 22 },
  session: { session_id: "5b7f0c1e-2d4a-4f6b-9c8e-1a2b3c4d5e6f", sequence: 1, idle_until: 1_791_000_005 },
};

const SESSION: PictureSession = {
  view: VIEW,
  phase: "shown",
  draft: { hardware: {}, software: {} },
  dirty: false,
  changeHardware: () => {},
  changeSoftware: () => {},
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

/** Display brightness 46 of 100, colour preset "6500 K" and Photo Wall brightness 114 %: nothing changed yet. */
export const Shown: Story = {};
/** A display brightness change the Pi has not shown yet: Done waits. */
export const Waiting: Story = {
  args: { session: { ...SESSION, phase: "waiting", dirty: true, latest: 1, draft: { hardware: { "10": 75 }, software: {} } } },
};
/** The Pi showed the latest change: Done is offered. */
export const ChangeShown: Story = {
  args: {
    session: {
      ...SESSION, dirty: true, latest: 1, draft: { hardware: {}, software: { brightness: 130 } },
      view: { ...VIEW, software: { ...VIEW.software, brightness_percent: 130 } },
    },
  },
};
/** The display read back another value than the one written. */
export const NotTaken: Story = {
  args: {
    session: {
      ...SESSION,
      view: { ...VIEW, hardware: [{ ...BRIGHTNESS, result: { result: "not-taken", before: 60, after: 20 } }, CONTRAST, PRESET, MODE] },
    },
  },
};
/** Nobody set the display's own settings: nothing is written to the display. */
export const AsTheDisplayHasIt: Story = {
  args: { session: { ...SESSION, view: { ...VIEW, hardware: [{ ...BRIGHTNESS, value: null, result: null }, CONTRAST, { ...PRESET, value: null, result: null }, MODE] } } },
};
/** The Pi has not finished asking the display what it offers. */
export const Checking: Story = {
  args: { session: { ...SESSION, view: { ...VIEW, offered_known: false, hardware: [] } } },
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
      error: { words: "This display does not offer that setting, so Photo Wall cannot set it.", code: "not_offered" },
    },
  },
};
/** A list setting: its choices and "As the display has it", one being chosen in this visit. */
export const Choices: Story = {
  args: { session: { ...SESSION, phase: "waiting", dirty: true, latest: 1, draft: { hardware: { "14": 4 }, software: {} } } },
};
/** Settings the display answers but lists no choices for, or no longer offers: shown, no control. */
export const ReadOnly: Story = {
  args: {
    session: {
      ...SESSION,
      view: { ...VIEW, hardware: [MODE, {
        code: "87", name: "Sharpness", value: 7, offered: null, result: null, changed_on_buttons: false,
      }] },
    },
  },
};
/** The Pi's re-read found another value than the one set here: someone used the display's buttons. */
export const ChangedOnTheButtons: Story = {
  args: {
    session: {
      ...SESSION,
      view: { ...VIEW, hardware: [{ ...BRIGHTNESS, offered: { range: LEVEL_100, current: 35 }, changed_on_buttons: true }, CONTRAST, PRESET, MODE] },
    },
  },
};
