import type { Meta, StoryObj } from "@storybook/react-vite";

import { FrameWhy, type FrameWhyView } from "./frame-why";

const EVENINGS = { kind: "program" as const, activation_id: "program:p-evenings:1:1792000800", run_id: "r-1",
  scene_id: "evening", program_id: "p-evenings", name: "Evenings", rank: 1 };
const DEFAULT = { kind: "default-scene" as const, activation_id: "default:calm", run_id: "r-0", scene_id: "calm",
  program_id: null, name: "calm", rank: -1 };
const WHY: FrameWhyView = {
  frame_id: "bedroom",
  content: { winner: EVENINGS, beat: [{ request: DEFAULT, because: "lower-priority" }] },
  power: { wanted: "off", because: "night-off", until: "2026-10-15T07:00",
    pi_says: { kind: "timetable", power: "off" }, result: "confirmed", resting: true },
};
const READ_AT = 1_792_017_000;   // Wed 14 Oct 2026 23:30 in London

const meta = {
  title: "Domain/FrameWhy",
  component: FrameWhy,
  args: { read: { ok: true, why: WHY, zone: "Europe/London" }, readAt: READ_AT },
} satisfies Meta<typeof FrameWhy>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Evenings beat the Default Scene; the display rests in Night off. */
export const Resting: Story = {};
/** The display did not answer the off: a power problem, never Resting. */
export const DidNotAnswer: Story = {
  args: { read: { ok: true, why: { ...WHY, power: { ...WHY.power!, result: "did-not-answer", resting: false } }, zone: "Europe/London" } },
};
/** No report answers Central's latest document yet. */
export const NotHeard: Story = {
  args: { read: { ok: true, why: { ...WHY, power: { ...WHY.power!, pi_says: "not-heard", result: null, resting: false } }, zone: "Europe/London" } },
};
/** Central did not answer. */
export const Unreachable: Story = { args: { read: { ok: false, error: "unreachable" } } };
const SHOW_NOW = { kind: "show-now" as const, activation_id: "show-1", run_id: "r-2", scene_id: "party",
  program_id: null, name: "party", rank: 1 };
/** Two requests at the same priority: the one that started later plays; the other started earlier. */
export const AdmittedEarlier: Story = {
  args: { read: { ok: true, why: { ...WHY, content: { winner: SHOW_NOW,
    beat: [{ request: EVENINGS, because: "admitted-earlier" }] } }, zone: "Europe/London" } },
};
/** A Schedule the playing Show now's protected Scene keeps off this Frame is named after it. */
export const KeptOut: Story = {
  args: { read: { ok: true, why: { ...WHY, content: { winner: SHOW_NOW,
    beat: [{ request: EVENINGS, because: "kept-out-by-protected-frames" }] } }, zone: "Europe/London" } },
};
/** Central wants the display off and the Pi has not carried it out yet: not Resting. */
export const TurningOff: Story = {
  args: { read: { ok: true, why: { ...WHY, power: { ...WHY.power!, pi_says: { kind: "timetable", power: "on" },
    result: null, resting: false } }, zone: "Europe/London" } },
};
