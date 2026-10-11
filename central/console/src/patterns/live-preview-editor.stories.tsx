import type { Meta, StoryObj } from "@storybook/react-vite";

import { Field } from "../ui/field";
import { Slider } from "../ui/slider";
import { LivePreviewEditor } from "./live-preview-editor";
import { ProblemCard } from "./problem-card";

const controls = (
  <Field label="Brightness — Photo Wall picture adjustment">
    <Slider value={145} min={0} max={200} step={5} unit="%" onCommit={() => {}} />
  </Field>
);

const meta = {
  title: "Patterns/LivePreviewEditor",
  component: LivePreviewEditor,
  args: {
    label: "Show on the Display",
    latestRevision: 3,
    ack: { revision: 3, at: "21:04:07" },
    dirty: false,
    onDone: () => {},
    onRevert: () => {},
    controls,
  },
} satisfies Meta<typeof LivePreviewEditor>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Nothing changed: the Pi presents the saved values. */
export const Clean: Story = {};

/** A change sent, not yet presented: Done waits, and says why. */
export const Requested: Story = { args: { dirty: true, latestRevision: 4 } };

/** The latest change presented: Done is offered. */
export const Acknowledged: Story = { args: { dirty: true } };

export const Unavailable: Story = {
  args: {
    unavailable: {
      reason: "Photo Wall could not check this Frame's Pi.",
      action: { label: "Try again", onAction: () => {} },
    },
  },
};

export const WithProblem: Story = {
  args: {
    dirty: true,
    latestRevision: null,
    ack: null,
    status: "Your changes are not being sent to the Pi.",
    problem: (
      <ProblemCard
        scope="central"
        variant="inline"
        verdict={{ severity: "alarm", label: "Not sent", receipt: null }}
        what="Someone saved a different position or picture for this Frame meanwhile. Press Revert to load it, then adjust again."
        doing={null}
        action={{ label: "Revert", onAction: () => {} }}
      />
    ),
  },
};

/** Saved, and a previous visit's unkept changes reverted. */
export const WithNotes: Story = {
  args: { notes: ["Your unsaved changes were reverted at 21:10:02 because you left this Frame's page.", "Saved at 21:12:40."] },
};
