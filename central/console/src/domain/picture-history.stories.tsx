import type { Meta, StoryObj } from "@storybook/react-vite";

import type { PictureSession } from "./frame-picture";
import { type PictureEntryView, type PictureHistoryView, PictureHistoryList, type SoftwarePictureView } from "./picture-history";

type View = NonNullable<PictureSession["view"]>;

const DISPLAY = { id: "8b9e4f2d-1c3a-4b5e-8f7d-2a6c9e0b1d34", name: "DELL U2720Q" };
const SOFTWARE: SoftwarePictureView = {
  brightness_percent: 100, contrast_percent: 100, colour_temperature_kelvin: 6500, gamma_tenths: 22,
  saturation_percent: 100, highlight_rolloff_percent: 0, black_floor_tenths: 0, lut_amount_percent: 100, lut: null,
};
const PAPER = { profile_sha256: "a".repeat(64), name: "Hahnemühle Photo Rag 308 (matte)" };

const VIEW: View = {
  frame_id: "lobby",
  status: "shown",
  display_id: DISPLAY.id,
  connected: true,
  offered_known: true,
  hardware: [{
    code: "10", name: "Brightness", value: 60, offered: { range: { kind: "level", maximum: 100, choices: [] }, current: 60 },
    result: null, changed_on_buttons: false,
  }],
  software: SOFTWARE,
  session: { session_id: "5b7f0c1e-2d4a-4f6b-9c8e-1a2b3c4d5e6f", sequence: 3, idle_until: 1_791_000_005 },
  signal_range: "full",
  signal: { in_force: "full", result: "applied", answered: true },
  test_pattern: null,
  test_pattern_seconds: 600,
};

const SESSION: PictureSession = {
  view: VIEW,
  phase: "live",
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

// 2026-10-13 in UTC: the story's times show in the browser's zone (no house clock here).
function entry(id: string, kind: PictureEntryView["kind"], at: string, values: {
  hardware?: Record<string, number>; software?: Partial<SoftwarePictureView>;
  signal?: "auto" | "full" | "limited"; recipe?: string | null; pattern?: PictureEntryView["test_pattern"];
} = {}): PictureEntryView {
  const software = { ...SOFTWARE, ...values.software };
  return {
    entry_id: id, display: DISPLAY, kind, number: 1, at, frame: kind.startsWith("sent-") ? { id: "lobby", name: "Lobby" } : null,
    session_id: null, restored_from: null,
    picture: {
      hardware: values.hardware ?? { "10": 60 }, software, signal_range: values.signal ?? "full",
      lut_recipe_id: software.lut === null ? null : values.recipe ?? "lut-recipe-2",
    },
    test_pattern: values.pattern ?? null,
  };
}

const KEPT: PictureHistoryView = {
  display: DISPLAY,
  unrecorded_change: false,
  entries: [
    entry("k3", "kept-done", "2026-10-13T19:58:31Z", { hardware: { "10": 45 }, software: { brightness_percent: 110, gamma_tenths: 24, lut: PAPER } }),
    entry("k2", "kept-copied", "2026-10-13T08:12:09Z", { software: { brightness_percent: 105, lut: PAPER }, recipe: "lut-recipe-1" }),
    entry("k1", "kept-done", "2026-10-12T21:10:02Z", { hardware: { "10": 60 }, signal: "auto" }),
  ],
};

const NOT_KEPT: PictureHistoryView = {
  ...KEPT,
  entries: [
    entry("s3", "sent-back", "2026-10-13T20:04:40Z", { hardware: { "10": 45 }, software: { brightness_percent: 110, gamma_tenths: 24, lut: PAPER } }),
    entry("s2", "sent-draft", "2026-10-13T20:04:07Z", { hardware: { "10": 52 }, software: { brightness_percent: 120, gamma_tenths: 24, lut: PAPER }, pattern: "white" }),
    entry("s1", "sent-draft", "2026-10-13T20:03:55Z", { hardware: { "10": 45 }, software: { brightness_percent: 110, gamma_tenths: 24, lut: PAPER }, pattern: "white" }),
    ...KEPT.entries,
  ],
};

const meta = {
  title: "Domain/PictureHistory",
  component: PictureHistoryList,
  args: {
    history: KEPT, session: SESSION, busy: false, restoring: null, onRestore: () => {}, onRetry: () => {},
  },
} satisfies Meta<typeof PictureHistoryList>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Kept changes (Done, Copy) over two days; the LUT of the copied one was made with an older recipe. */
export const Kept: Story = {};
/** A tuning visit: changes tried with the White pattern up, not kept (dashed), then put back by Revert. */
export const NotKept: Story = { args: { history: NOT_KEPT } };
/** The display's values changed outside Photo Wall since the last kept change. */
export const Unrecorded: Story = {
  args: {
    history: {
      ...KEPT, unrecorded_change: true,
      entries: [entry("u1", "kept-unrecorded", "2026-10-13T20:30:00Z", { software: { brightness_percent: 90 } }), ...KEPT.entries],
    },
  },
};
/** A Restore whose display setting this display no longer offers, and whose LUT is made with today's recipe. */
export const Skipped: Story = {
  args: {
    outcome: {
      notes: ["Not restored: Brightness (this display does not offer them now).", "The LUT is made with today's recipe."],
    },
  },
};
/** No editor running (someone else is adjusting this display): Restore says why it waits. */
export const NoSession: Story = {
  args: {
    session: {
      ...SESSION, phase: "idle",
      waitingFor: "Someone else is adjusting this display's picture. Photo Wall connects as soon as they finish.",
    },
  },
};
