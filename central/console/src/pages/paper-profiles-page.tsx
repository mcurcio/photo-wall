import { useCallback, useState } from "react";

import { addPaperProfile, listPaperProfiles, removePaperProfile } from "../domain/paper-profiles/api";
import { addWords, removeWords } from "../domain/paper-profiles/model";
import { PaperProfiles } from "../domain/paper-profiles/paper-profiles";
import type { PaperProfilesView } from "../generated/contract";
import { NOT_READ, nextRead, type PageRead, PolledPage } from "../patterns/polled-page";
import { useSaveNote } from "../patterns/save-dialog";
import { usePolledRead } from "../polledRead.js";

/** How often the list is read again while shown: a Frame choosing a paper profile shows in Used by within this. */
const CADENCE_MS = 10000;

/**
 * Settings › Paper profiles (`#/paper-profiles`; decision 0028): the paper profiles on the list,
 * Add a paper profile and Remove. Central checks a profile and bakes its LUT once on Add, so every
 * profile listed works; it refuses to remove one a Frame's paper uses. The list is read every
 * CADENCE_MS while shown and again after each Add or Remove.
 */
export function PaperProfilesPage() {
  const load = useCallback(async (previous: PageRead<PaperProfilesView>) => {
    const answer = await listPaperProfiles();
    return nextRead(previous, answer.ok ? answer.view : null);
  }, []);
  const { value: read, refresh } = usePolledRead(load, {
    cadenceMs: CADENCE_MS, initial: NOT_READ as PageRead<PaperProfilesView> });
  const { note, say, settle } = useSaveNote(refresh);
  const [adding, setAdding] = useState(false);
  const add = async (file: File) => {
    setAdding(true);
    say(null);
    const refused = await settle(addPaperProfile(file), (taken) => addWords(file.name, taken),
      (answer) => addWords(file.name, answer));
    setAdding(false);
    if (refused !== null) say(refused);
  };
  return (
    <PolledPage read={read} onRetry={() => void refresh()} what="Central did not answer with the paper profiles."
      stale="Central did not answer the last read: this is the list as last read.">
      {(view) => (
        <PaperProfiles profiles={view.paper_profiles} note={note} adding={adding} onAdd={(file) => void add(file)}
          onRemove={(profile) => settle(removePaperProfile(profile.profile_sha256), (taken) => removeWords(profile, taken),
            (answer) => removeWords(profile, answer))} />
      )}
    </PolledPage>
  );
}
