import type { Meta, StoryObj } from "@storybook/react-vite";

import { TypedNumber } from "./typed-number";
import { unitWords } from "./units";

const meta = {
  title: "Patterns/TypedNumber",
  component: TypedNumber,
  args: {
    label: "Lux at the Frame",
    help: "What a phone light meter reads at the Frame's centre, facing out",
    value: 800,
    min: 0,
    max: 200000,
    step: 1,
    unit: "lx",
    exclusiveMin: true,
    onChange: () => {},
  },
} satisfies Meta<typeof TypedNumber>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A lux typed in range: applied as it is typed. */
export const InRange: Story = {};
/** A value outside its range: the Field says the range and nothing is applied. */
export const OutOfRange: Story = { args: { value: 0 } };
/** A place in millimetres, either side of the Frame's centre. */
export const Millimetres: Story = {
  args: { label: "Across", help: "From the Frame's centre, + right", value: -300, min: -5000, max: 5000, step: 10,
    unit: "mm", exclusiveMin: false },
};
/** While nothing can change. */
export const Disabled: Story = { args: { disabled: true } };

/** Each unit's one way of being written: a pure check the catalogue runs before any story renders. */
const checks: [string, string][] = [
  [unitWords(1000, "lx"), "1,000 lx"],
  [unitWords(200000, "lx"), "200,000 lx"],
  [unitWords(2.46, "lx"), "2.5 lx"],
  [unitWords(2700, "K"), "2700 K"],
  [unitWords(12.5, "mm"), "12.5 mm"],
  [unitWords(45, "°"), "45°"],
  [unitWords(60, "%"), "60 %"],
  [unitWords(0.03, "%"), "0.03 %"],
];
for (const [said, meant] of checks) {
  if (said !== meant) throw new Error(`unit words: ${JSON.stringify(said)} is not ${JSON.stringify(meant)}`);
}
