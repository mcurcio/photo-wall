import { Button } from "../ui/button";
import { Dialog, DialogClose } from "../ui/dialog";

export interface LeaveGuardProps {
  open: boolean;
  /** Keep the changes (Done), then leave. */
  onKeep: () => void;
  /** Undo the changes, then leave. */
  onRevert: () => void;
  /** Stay where you are. */
  onStay: () => void;
  /** Why Keep cannot be pressed yet (the Pi has not presented the latest change); null when it can. */
  keepBlocked: string | null;
  /** Keep or Revert is running. */
  busy?: boolean;
}

/**
 * LeaveGuard (design language §4, dialog guards): leaving a live editor with changes not kept.
 * Plain, not dangerous: both answers can be undone. Keep waits for the Pi's acknowledgement, as
 * Done does, and says so.
 */
export function LeaveGuard({ open, onKeep, onRevert, onStay, keepBlocked, busy = false }: LeaveGuardProps) {
  return (
    <Dialog
      open={open}
      busy={busy}
      onOpenChange={(next) => { if (!next) onStay(); }}
      title="Keep your changes?"
      actions={(
        <>
          <Button variant="primary" disabled={keepBlocked !== null || busy} onClick={onKeep}>Keep</Button>
          <Button disabled={busy} onClick={onRevert}>Revert</Button>
          <DialogClose>Stay</DialogClose>
        </>
      )}
    >
      <p className="m-0">
        The Pi presents your changes, but they are not kept. Keep them, or revert to the saved values.
      </p>
      {keepBlocked !== null && <p className="m-0 mt-2 text-muted">{keepBlocked}</p>}
    </Dialog>
  );
}
