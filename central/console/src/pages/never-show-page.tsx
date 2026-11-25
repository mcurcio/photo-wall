import { useCallback, useState } from "react";

import { NEVER_SHOW_SAID, notTakenSaid, SHOWN_AGAIN_SAID } from "../domain/everyday-words";
import { NeverShowList } from "../domain/never-show-list";
import { type Answered, neverShow, readNeverShowList, showAgain } from "../domain/everyday-api";
import type { NeverShowEntry, NeverShowListView } from "../generated/contract";
import { NOT_READ, nextRead, type PageRead, PolledPage } from "../patterns/polled-page";
import { UndoToast } from "../patterns/undo-toast";
import { usePolledRead } from "../polledRead.js";
import { Stack } from "../ui/stack";

/** How often the list is read again while shown: a photo listed from Home shows here within this. */
const CADENCE_MS = 10000;

interface Toast { key: string; message: string; undo?: () => void }

export interface NeverShowPageProps {
  /** Whether the page is shown: a Show section stays mounted while hidden, and reads nothing then. */
  current: boolean;
}

/**
 * Scenes › Never show list (`#/never-show`; decision 0036, F3): every listed photo with **Show
 * again**, which answers with Undo for 10 s (Undo lists it again). Read every CADENCE_MS while
 * shown and again after each change. Central's time of a read is not served with the list, so
 * each "when" is said with its day.
 */
export function NeverShowPage({ current }: NeverShowPageProps) {
  const load = useCallback(async (previous: PageRead<NeverShowListView>) => {
    const answer = await readNeverShowList();
    return nextRead(previous, answer.ok ? answer.answer : null);
  }, []);
  const { value: read, refresh } = usePolledRead(load, {
    cadenceMs: CADENCE_MS, skip: !current, initial: NOT_READ as PageRead<NeverShowListView> });
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);

  const change = async (send: () => Promise<Answered<unknown>>, done: string, lead: string, undo?: () => void) => {
    setBusy(true);
    const answer = await send();
    setToast({ key: crypto.randomUUID(), message: answer.ok ? done : notTakenSaid(answer.error, lead),
      undo: answer.ok ? undo : undefined });
    await refresh();
    setBusy(false);
  };
  const onShowAgain = (photo: NeverShowEntry) => void change(() => showAgain(photo.asset_id), SHOWN_AGAIN_SAID, "Still hidden",
    () => void change(() => neverShow(photo.asset_id, null, crypto.randomUUID()), NEVER_SHOW_SAID, "Not hidden"));

  return (
    <Stack label="Never show list">
      <PolledPage read={read} onRetry={() => void refresh()} what="Central did not answer with the Never show list."
        stale="Central did not answer the last read: this is the list as last read.">
        {(view) => <NeverShowList photos={view.photos ?? []} now={null} busy={busy} onShowAgain={onShowAgain} />}
      </PolledPage>
      {toast !== null && (
        <UndoToast key={toast.key} message={toast.message} onUndo={toast.undo === undefined ? undefined : () => {
          setToast(null);
          toast.undo?.();
        }} onExpire={() => setToast((shown) => (shown?.key === toast.key ? null : shown))} />
      )}
    </Stack>
  );
}
