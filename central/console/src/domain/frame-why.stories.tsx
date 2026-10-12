import type { Meta, StoryObj } from "@storybook/react-vite";

import { FrameWhy, type FrameWhyView } from "./frame-why";

const EVENINGS = { kind: "program" as const, activation_id: "program:p-evenings:1:1792000800", run_id: "r-1",
  scene_id: "evening", program_id: "p-evenings", name: "Evenings", rank: 1 };
const DEFAULT = { kind: "default-scene" as const, activation_id: "default:calm", run_id: "r-0", scene_id: "calm",
  program_id: null, name: "calm", rank: 0 };
const WHY: FrameWhyView = {
  frame_id: "bedroom",
  content: { winner: EVENINGS, beat: [{ request: DEFAULT, because: "lower-priority" }] },
  power: { wanted: "off", because: "night-off", until: "2026-10-15T07:00",
    pi_says: { kind: "timetable", power: "off" }, resting: true },
};
const READ_AT = 1_792_017_000;   // Wed 14 Oct 2026 23:30 in London

const meta = {
  title: "Domain/FrameWhy",
  component: FrameWhy,
  args: { read: { ok: true, why: WHY, result: "confirmed", zone: "Europe/London" }, readAt: READ_AT },
} satisfies Meta<typeof FrameWhy>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Evenings beat the Default Scene; the display rests in Night off. */
export const Resting: Story = {};
/** The display did not answer the off: a power problem, never Resting. */
export const DidNotAnswer: Story = { args: { read: { ok: true, why: WHY, result: "did-not-answer", zone: "Europe/London" } } };
/** No report answers Central's latest document yet. */
export const NotHeard: Story = {
  args: { read: { ok: true, why: { ...WHY, power: { ...WHY.power!, pi_says: "not-heard", resting: false } }, result: null, zone: "Europe/London" } },
};
/** Central did not answer. */
export const Unreachable: Story = { args: { read: { ok: false, error: "unreachable" } } };
