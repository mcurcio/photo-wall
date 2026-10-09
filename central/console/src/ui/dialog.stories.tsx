import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";

import { Button } from "./button";
import { Dialog, DialogClose } from "./dialog";

const meta = {
  title: "Primitives/Dialog",
  component: Dialog,
  args: {
    open: true,
    onOpenChange: () => {},
    title: "Retire player pi-07?",
    children: "No undo, even after re-imaging: the id comes from the serial.",
  },
} satisfies Meta<typeof Dialog>;

export default meta;
type Story = StoryObj<typeof meta>;

const actions = (
  <>
    <Button variant="danger">Confirm retire</Button>
    <DialogClose>Cancel</DialogClose>
  </>
);

export const Idle: Story = { args: { actions } };

/** In flight: Escape, an outside press and Cancel do nothing; focus is on the dialog. */
export const Busy: Story = {
  args: {
    busy: true,
    actions: (
      <>
        <Button variant="danger" disabled>
          Confirm retire
        </Button>
        <DialogClose>Cancel</DialogClose>
      </>
    ),
  },
};

/** Opened from a button; Confirm holds it busy for two seconds, then closes it. */
export const Interactive: Story = {
  args: { open: false },
  render: function Render(args) {
    const [open, setOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    const confirm = () => {
      setBusy(true);
      window.setTimeout(() => {
        setBusy(false);
        setOpen(false);
      }, 2000);
    };
    return (
      <>
        <Button onClick={() => setOpen(true)}>Retire</Button>
        <Dialog
          {...args}
          open={open}
          busy={busy}
          onOpenChange={setOpen}
          actions={
            <>
              <Button variant="danger" disabled={busy} onClick={confirm}>
                Confirm retire
              </Button>
              <DialogClose>Cancel</DialogClose>
            </>
          }
        />
      </>
    );
  },
};
