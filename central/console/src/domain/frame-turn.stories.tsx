import type { Meta, StoryObj } from "@storybook/react-vite";

import { FrameTurnChoice } from "./frame-turn";

const meta = {
  title: "Domain/FrameTurn",
  component: FrameTurnChoice,
  args: { turn: 0, sending: false, answer: null, positionHref: "#/wall/frames/lobby/position", onChoose: () => {} },
} satisfies Meta<typeof FrameTurnChoice>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The screen hangs as built: its own top edge at the top. */
export const Upright: Story = {};
/** Just turned: the picture turns now, and its position is checked on the Position tab. */
export const TopToTheRight: Story = { args: { turn: 90, answer: { kind: "changed" } } };
/** A choice on its way to Central: the control waits for the answer. */
export const Sending: Story = { args: { turn: 270, sending: true } };
/** Central refused the choice: the Frame changed meanwhile. */
export const Refused: Story = {
  args: {
    answer: { kind: "refused", words: "This Frame changed while you chose. Choose again.",
      code: "binding_generation_conflict" },
  },
};
