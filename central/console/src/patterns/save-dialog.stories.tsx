import type { Meta, StoryObj } from "@storybook/react-vite";

import { Field, TextInput } from "../ui/field";
import { RemoveDialog, SaveDialog } from "./save-dialog";

const meta = {
  title: "Patterns/SaveDialog",
  component: SaveDialog,
  args: {
    title: "Change Window Sensor",
    action: "Save Sensor",
    onSave: async () => null,
    onClose: () => undefined,
    children: ({ busy, edited }) => (
      <Field label="Name">
        <TextInput value="Window Sensor" onChange={edited} disabled={busy} />
      </Field>
    ),
  },
} satisfies Meta<typeof SaveDialog>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A change: the form, Cancel and the confirm button. */
export const Change: Story = {};

/** A remove: what is forgotten, and the danger button. */
export const Remove: Story = {
  render: () => (
    <RemoveDialog name="Bookshelf spot" what="Photo Wall forgets this light." onClose={() => undefined}
      onRemove={async () => null} />
  ),
};
