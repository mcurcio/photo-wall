import type { Meta, StoryObj } from "@storybook/react-vite";

import { StepProgress } from "./step-progress";

const meta = {
  title: "Patterns/StepProgress",
  component: StepProgress,
  args: {
    label: "Restarting the photo app on Pi pw-3f2a",
    steps: [
      { label: "Requested", state: "done", at: "21:04:07" },
      { label: "App stopped", state: "done", at: "21:04:09" },
      { label: "App started", state: "current" },
      { label: "Showing", state: "waiting" },
    ],
  },
} satisfies Meta<typeof StepProgress>;

export default meta;
type Story = StoryObj<typeof meta>;

/** In progress: two steps reported with their times, the third awaited. */
export const Running: Story = {};
/** Every step reported. */
export const Finished: Story = {
  args: {
    steps: [
      { label: "Requested", state: "done", at: "21:04:07" },
      { label: "App stopped", state: "done", at: "21:04:09" },
      { label: "App started", state: "done", at: "21:04:12" },
      { label: "Showing", state: "done", at: "21:04:20" },
    ],
  },
};
/** A step that did not happen by its deadline: where it stopped, and why in plain words. */
export const FailedStep: Story = {
  args: {
    label: "Rebooting Pi pw-3f2a",
    steps: [
      { label: "Requested", state: "done", at: "21:04:07" },
      { label: "Restarting", state: "failed", note: "no report from the Pi by 21:14." },
      { label: "Showing again", state: "waiting" },
    ],
  },
};
/** At phone width: one step under the other. */
export const Phone: Story = { parameters: { phone: true } };
