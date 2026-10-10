import type { Meta, StoryObj } from "@storybook/react-vite";

import { Note } from "./fact-row";
import { ProblemCard } from "./problem-card";

const meta = {
  title: "Patterns/ProblemCard",
  component: ProblemCard,
  args: {
    scope: "central",
    variant: "inline",
    verdict: { severity: "alarm", label: "Not sent", receipt: null },
    what: "Someone saved a different position or picture for this Frame meanwhile. Press Revert to load it, then adjust again.",
    doing: null,
    action: { label: "Revert", onAction: () => {} },
  },
} satisfies Meta<typeof ProblemCard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Inline: Story = {};

/** A passing refusal: Photo Wall keeps trying; the code is under Details. */
export const WithDetails: Story = {
  args: {
    verdict: { severity: "notice", label: "Retrying", receipt: null },
    what: "This Frame's Pi is not connected to Photo Wall. Check that it is on.",
    doing: "Photo Wall keeps trying.",
    details: <Note>Photo Wall&apos;s answer: trial_display_unavailable</Note>,
  },
};

/** A setup problem in a card, named by its subject. */
export const SetupCard: Story = {
  args: {
    scope: "setup",
    subject: "Frame profile",
    variant: "card",
    verdict: { severity: "todo", label: "Check the Display", receipt: null },
    what: "The Pi reported its Display at 1920 × 1080, but this Frame's profile is 1080 × 1920.",
    action: { label: "Edit Frame profile", onAction: () => {} },
  },
};
