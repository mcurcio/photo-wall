import type { Meta, StoryObj } from "@storybook/react-vite";

import {
  FramePicture, LutSettings, type PATTERN_NAMES, PhotoWallSettings, type PictureSession, SignalSettings, signalWords,
  TestPatternSettings, testPatternWords,
} from "./frame-picture";

type View = NonNullable<PictureSession["view"]>;
type Row = View["hardware"][number];

const LEVEL_100 = { kind: "level" as const, maximum: 100, choices: [] };
// A display counting brightness to 255 (the owner's example, 2026-10-12: "46 %  (117 of 255)").
const LEVEL_255 = { kind: "level" as const, maximum: 255, choices: [] };
const BRIGHTNESS: Row = {
  code: "10", name: "Brightness", value: 117,
  offered: { range: LEVEL_255, current: 117 }, result: { result: "taken", before: 153, after: 117 },
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
  software: {
    brightness_percent: 114, contrast_percent: 100, colour_temperature_kelvin: 6500, gamma_tenths: 22,
    saturation_percent: 100, highlight_rolloff_percent: 0, black_floor_tenths: 0, lut_amount_percent: 100, lut: null,
  },
  session: { session_id: "5b7f0c1e-2d4a-4f6b-9c8e-1a2b3c4d5e6f", sequence: 1, idle_until: 1_791_000_005 },
  signal_range: "auto",
  signal: { in_force: "auto", result: "applied", answered: true },
  test_pattern: null,
  test_pattern_seconds: 600,
};

// Two paper profiles on the LUTs list.
const LUTS = [
  { profile_sha256: "a".repeat(64), name: "Hahnemühle Photo Rag 308 (matte)" },
  { profile_sha256: "b".repeat(64), name: "Canson Baryta Photographique II" },
];

const SESSION: PictureSession = {
  view: VIEW,
  phase: "shown",
  draft: { hardware: {}, software: {}, lut: {}, signal: {}, pattern: {} },
  dirty: false,
  changeHardware: () => {},
  changeSoftware: () => {},
  changeLut: () => {},
  changeSignalRange: () => {},
  changeTestPattern: () => {},
  luts: [],
  done: async () => true,
  revert: async () => {},
  restore: async () => ({ ok: false, error: "session_ended" }),
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

/** Display brightness 46 % (117 of 255), colour preset "6500 K" and Photo Wall brightness 114 %: nothing changed yet. */
export const Shown: Story = {};
/** A display brightness change the Pi has not shown yet: Done waits. */
export const Waiting: Story = {
  args: { session: { ...SESSION, phase: "waiting", dirty: true, latest: 1, draft: { ...SESSION.draft, hardware: { "10": 75 }, software: {} } } },
};
/** The Pi showed the latest change: Done is offered. */
export const ChangeShown: Story = {
  args: {
    session: {
      ...SESSION, dirty: true, latest: 1, draft: { ...SESSION.draft, hardware: {}, software: { brightness: 130 } },
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
/** The Pi has not finished asking the display what it offers: a value set here shows alone. */
export const Checking: Story = {
  args: {
    session: {
      ...SESSION,
      view: { ...VIEW, offered_known: false, hardware: [{ ...BRIGHTNESS, offered: null, result: null }] },
    },
  },
};
/** The display is not connected: what it offered at its last check is kept, and the values apply when it is. */
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
  args: { session: { ...SESSION, phase: "waiting", dirty: true, latest: 1, draft: { ...SESSION.draft, hardware: { "14": 4 }, software: {} } } },
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
/** The Photo Wall (software) section alone (below the fold of the whole tab): every slider at no change. */
export const PhotoWallSection: Story = {
  render: () => <PhotoWallSettings view={{ ...VIEW, software: { ...VIEW.software, brightness_percent: 100 } }} session={SESSION} ready />,
};
/** The print look changed in this visit: saturation 85 %, soft highlights 40 % (brightness 114 %, so it
 * acts), black floor 2.0 %. */
export const PhotoWallPrintLook: Story = {
  render: () => (
    <PhotoWallSettings view={VIEW} ready
      session={{ ...SESSION, dirty: true, draft: { ...SESSION.draft, hardware: {}, software: { saturation: 85, "highlight-rolloff": 40, "black-floor": 20 } } }} />
  ),
};
/** Brightness and saturation at 100 % or below: nothing passes white, so soft highlights says where it acts. */
export const PhotoWallSoftHighlightsIdle: Story = {
  render: () => (
    <PhotoWallSettings view={{ ...VIEW, software: { ...VIEW.software, brightness_percent: 90, highlight_rolloff_percent: 30 } }}
      session={SESSION} ready />
  ),
};
/** The LUT picker with no LUT chosen: None and each LUT on the list. */
export const LutNone: Story = {
  render: () => <LutSettings view={VIEW} session={{ ...SESSION, luts: LUTS }} ready />,
};
/** No LUT on the list yet: the picker offers None alone. */
export const LutNoneListed: Story = {
  render: () => <LutSettings view={VIEW} session={SESSION} ready />,
};
/** A LUT chosen in this visit that the Pi has not fetched yet: the sliders show alone, Done waits;
 * its amount at 80 %. */
export const LutWaiting: Story = {
  render: () => (
    <LutSettings view={VIEW} ready
      session={{ ...SESSION, phase: "waiting", dirty: true, luts: LUTS,
        draft: { hardware: {}, software: { "lut-amount": 80 }, lut: { lut: LUTS[0].profile_sha256 }, signal: {}, pattern: {} } }} />
  ),
};
/** A LUT in force and shown, at 100 %. */
export const LutShown: Story = {
  render: () => (
    <LutSettings ready session={{ ...SESSION, luts: LUTS }} view={{ ...VIEW, software: { ...VIEW.software, lut: LUTS[1] } }} />
  ),
};
/** No session (the Pi is not connected, or someone else is adjusting): the values in force, nothing can change. */
export const PhotoWallNoSession: Story = {
  render: () => (
    <PhotoWallSettings view={{ ...VIEW, software: { ...VIEW.software, black_floor_tenths: 25 } }}
      session={{ ...SESSION, phase: "idle", latest: null }} ready={false} />
  ),
};

/** The words are the view's: a pure check the catalogue runs before any story renders (the minutes
 * come from `test_pattern_seconds`, the range the Pi still sends from its answer). */
const checks: [string, string][] = [
  [testPatternWords({ pattern: "white", state: "ended" }, 600), "Taken off after 10 min. Choose it again to show it."],
  [testPatternWords({ pattern: "white", state: "ended" }, 300), "Taken off after 5 min. Choose it again to show it."],
  [testPatternWords({ pattern: "grey-ramp", state: "waiting" }, 600),
    "Waiting for the Pi: it shows once the Player draws this Frame."],
  [signalWords("full", { in_force: "full", result: "applied", answered: true }), "The Pi sends Full."],
  [signalWords("full", { in_force: "limited", result: "failed", answered: true }),
    "The Pi could not change it: the display still gets Limited."],
  [signalWords("full", { in_force: "auto", result: "applied", answered: false }),
    "Waiting for the Pi: a change restarts the picture on every display of this Pi."],
  [signalWords("full", null), "Waiting for the Pi: a change restarts the picture on every display of this Pi."],
  [signalWords("auto", null), "The Pi chooses: Limited for TV modes."],
  [signalWords("full", { in_force: "auto", result: "unsupported", answered: true }),
    "This display's connection has no signal range setting."],
];
for (const [said, meant] of checks) {
  if (said !== meant) throw new Error(`frame-picture words: ${JSON.stringify(said)} is not ${JSON.stringify(meant)}`);
}

const pattern = (test_pattern: View["test_pattern"], drafted?: keyof typeof PATTERN_NAMES | null) => (
  <TestPatternSettings view={{ ...VIEW, test_pattern }} ready
    session={{ ...SESSION, latest: 1,
      draft: { ...SESSION.draft, pattern: drafted === undefined ? {} : { pattern: drafted } } }} />
);
/** White on every Frame of this display: the paper line, and the Pi shows it. */
export const TestPatternWhiteShown: Story = { render: () => pattern({ pattern: "white", state: "shown" }, "white") };
/** The Grey ramp chosen; the Pi has not shown it yet. */
export const TestPatternGreyRampWaiting: Story = {
  render: () => pattern({ pattern: "grey-ramp", state: "waiting" }, "grey-ramp"),
};
/** The Black level shown, with how to read it against the signal range. */
export const TestPatternBlackLevelShown: Story = {
  render: () => pattern({ pattern: "black-level", state: "shown" }, "black-level"),
};
/** The screen target drawn raw, judging the display's own settings: where the paper goes, and the paper
 * target's print links (Letter, A4); the Pi shows it. */
export const TestPatternTargetPreSoftwareShown: Story = {
  render: () => pattern({ pattern: "target-pre-software", state: "shown" }, "target-pre-software"),
};
/** The screen target through Photo Wall's settings, chosen in this visit; the Pi has not shown it yet. */
export const TestPatternTargetPostSoftwareWaiting: Story = {
  render: () => pattern({ pattern: "target-post-software", state: "waiting" }, "target-post-software"),
};
/** The Pi took the pattern off at its bound: Off, and the words to choose it again. */
export const TestPatternEnded: Story = { render: () => pattern({ pattern: "white", state: "ended" }, "white") };

const signal = (view: Partial<View>, range?: "auto" | "full" | "limited") => (
  <SignalSettings view={{ ...VIEW, ...view }} ready
    session={{ ...SESSION, latest: 1, draft: { ...SESSION.draft, signal: range === undefined ? {} : { range } } }} />
);
/** Full kept, and the Pi sends it. */
export const SignalFullApplied: Story = {
  render: () => signal({ signal_range: "full", signal: { in_force: "full", result: "applied", answered: true } }),
};
/** Full chosen in this visit: waiting for the Pi, whose change restarts every display's picture. */
export const SignalFullRestarting: Story = {
  render: () => signal({ signal_range: "full", signal: { in_force: "auto", result: "applied", answered: false } }, "full"),
};
/** The Pi could not change it: the range the display still gets. */
export const SignalFailed: Story = {
  render: () => signal({ signal_range: "full", signal: { in_force: "limited", result: "failed", answered: true } }, "full"),
};
/** A connection with no signal range setting. */
export const SignalUnsupported: Story = {
  render: () => signal({ signal_range: "full", signal: { in_force: "auto", result: "unsupported", answered: true } }, "full"),
};
