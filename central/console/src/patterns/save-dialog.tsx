import { type ReactNode, useCallback, useState } from "react";

import { Button } from "../ui/button";
import { Dialog, DialogClose } from "../ui/dialog";
import { Stack } from "../ui/stack";
import { Note } from "./fact-row";

/** What an add, a change or a remove came to: null when it was taken, else its words. */
export type Outcome = Promise<string | null>;

/** A request's answer as the domains' request modules fold it: taken, or refused with a code. */
type Sent = { ok: true } | { ok: false; error: string };

export interface SaveDialogProps {
  title: string;
  /** The confirm button's label: "Save light", "Link", "Confirm remove". */
  action: string;
  /** A remove: the confirm button is the danger one. */
  danger?: boolean;
  /**
   * Check the form and send it: null closes the dialog; words (a problem found before sending, or
   * the refusal) stay in it until the form is edited.
   */
  onSave: () => Outcome;
  onClose: () => void;
  /** The form, given whether a save is on its way (its inputs wait) and `edited`, which clears the words. */
  children: (form: { busy: boolean; edited: () => void }) => ReactNode;
}

/**
 * SaveDialog: the one add, change and remove flow. The form sits in a Dialog with Cancel and one
 * confirm button; while a save is on its way the dialog is busy (it cannot be dismissed, its
 * buttons wait); a save taken closes it, and one refused keeps it open with the refusal in words
 * beside the form, until the operator edits it or cancels.
 */
export function SaveDialog({ title, action, danger = false, onSave, onClose, children }: SaveDialogProps) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const edited = useCallback(() => setProblem(null), []);
  const save = async () => {
    setBusy(true);
    const refused = await onSave();
    setBusy(false);
    if (refused === null) onClose();
    else setProblem(refused);
  };
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }} busy={busy} title={title}
      actions={(
        <>
          <DialogClose>Cancel</DialogClose>
          <Button variant={danger ? "danger" : "primary"} disabled={busy} onClick={() => void save()}>{action}</Button>
        </>
      )}>
      <Stack>
        {children({ busy, edited })}
        {problem !== null && <Note live>{problem}</Note>}
      </Stack>
    </Dialog>
  );
}

/** RemoveDialog: SaveDialog's remove, "Remove <name>?", saying what is forgotten. */
export function RemoveDialog({ name, what, onClose, onRemove }: {
  name: string; what: string; onClose: () => void; onRemove: () => Outcome;
}) {
  return (
    <SaveDialog title={`Remove ${name}?`} action="Confirm remove" danger onSave={onRemove} onClose={onClose}>
      {() => <p>{what}</p>}
    </SaveDialog>
  );
}

/**
 * The page's side of the flow: `settle` waits for a request, reads the page again whatever came of
 * it (a refusal may mean the page is out of date), and answers the dialog's Outcome: null with
 * `done` as the page's note when it was taken, else the refusal's words. `say` sets the note
 * directly, for a change made without a dialog.
 */
export function useSaveNote(reread: () => unknown) {
  const [note, say] = useState<string | null>(null);
  const settle = useCallback(async <A extends Sent>(
    request: Promise<A>,
    done: string | ((taken: Extract<A, { ok: true }>) => string),
    refused: (answer: Extract<A, { ok: false }>) => string,
  ): Outcome => {
    const answer = await request;
    void reread();
    if (!answer.ok) return refused(answer as Extract<A, { ok: false }>);
    say(typeof done === "string" ? done : done(answer as Extract<A, { ok: true }>));
    return null;
  }, [reread]);
  return { note, say, settle };
}
