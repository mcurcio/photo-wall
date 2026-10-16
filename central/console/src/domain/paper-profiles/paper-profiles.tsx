import { useState } from "react";

import type { PaperProfileView } from "../../generated/contract";
import { EntityList } from "../../patterns/entity-list";
import { Note } from "../../patterns/fact-row";
import { type Outcome, RemoveDialog } from "../../patterns/save-dialog";
import { captureDay } from "../../timeWords.js";
import { Button } from "../../ui/button";
import { FileChooser } from "../../ui/file-chooser";
import { Inline, Stack } from "../../ui/stack";
import { usedByWords } from "./model";

type Row = PaperProfileView & { key: string; severity: null };

export interface PaperProfilesProps {
  profiles: readonly PaperProfileView[];
  /** The last add's or remove's words, until the next. */
  note: string | null;
  /** An add is on its way: the chooser waits. */
  adding: boolean;
  onAdd: (file: File) => void;
  /** Remove one: null when Central took it, else its words (shown in the dialog). */
  onRemove: (profile: PaperProfileView) => Outcome;
}

/**
 * Settings › Paper profiles: each paper profile by its name, when it was added and the Frames whose
 * paper uses it; Add a paper profile (a file from the paper's maker, checked by Central, a refusal
 * in plain words) and Remove, confirmed, which Central refuses while a Frame's paper uses it. The
 * page reads and writes; this is what a story draws.
 */
export function PaperProfiles({ profiles, note, adding, onAdd, onRemove }: PaperProfilesProps) {
  const [removing, setRemoving] = useState<PaperProfileView | null>(null);
  const rows: Row[] = profiles.map((profile) => ({ ...profile, key: profile.profile_sha256, severity: null }));
  return (
    <Stack>
      <Note>
        A paper profile makes photos look printed on one paper. It is the colour profile (an ICC file) your
        paper&apos;s maker publishes for each paper and printer. Choose it as a Frame&apos;s paper profile, on its
        Lighting tab.
      </Note>
      <EntityList<Row>
        label="Paper profiles"
        columns={[
          { id: "name", header: "Name", rowHeader: true, cell: (row) => row.name },
          { id: "added", header: "Added", cell: (row) => captureDay(row.added_at) },
          { id: "used", header: "Used by", cell: (row) => usedByWords(row) },
          {
            id: "actions", header: "Actions",
            cell: (row) => (
              <Button variant="ghost" aria-label={`Remove ${row.name}`} onClick={() => setRemoving(row)}>Remove</Button>
            ),
          },
        ]}
        groups={[{ title: "On the list", rows }]}
        empty="Add a paper profile from your paper's maker."
      />
      {note !== null && <Note live>{note}</Note>}
      <Inline label="Paper profile actions">
        <FileChooser accept=".icc,.icm" disabled={adding} onChoose={onAdd}>
          {adding ? "Adding…" : "Add a paper profile"}
        </FileChooser>
      </Inline>
      {removing !== null && (
        <RemoveDialog name={removing.name} what="Photo Wall forgets this paper profile. To use it again, add its file again."
          onClose={() => setRemoving(null)} onRemove={() => onRemove(removing)} />
      )}
    </Stack>
  );
}
