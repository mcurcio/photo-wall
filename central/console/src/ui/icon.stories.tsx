import type { Meta, StoryObj } from "@storybook/react-vite";

import { Icon, ICON_NAMES } from "./icon";

const meta = {
  title: "Primitives/Icon",
  component: Icon,
  args: { name: "home", label: "Home" },
} satisfies Meta<typeof Icon>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Named in words: an image to a screen reader. */
export const Named: Story = {};
/** Beside the words that already say it: hidden from a screen reader. */
export const Decorative: Story = {
  render: () => (
    <span className="inline-flex items-center gap-2 text-sm text-text">
      <Icon name="calendar" decorative />
      Schedule
    </span>
  ),
};
/** Every drawing, each with its name. */
export const All: Story = {
  render: () => (
    <ul className="m-0 flex list-none flex-wrap gap-4 p-0 text-xs text-muted">
      {ICON_NAMES.map((name) => (
        <li key={name} className="flex flex-col items-center gap-1 text-text">
          <Icon name={name} label={name} size="tab" />
          <span className="text-muted">{name}</span>
        </li>
      ))}
    </ul>
  ),
};
