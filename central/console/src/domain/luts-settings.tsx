import { useState } from "react";

import type { PaperProfilesRefusal, PaperProfileView } from "../generated/contract";
import { EntityList } from "../patterns/entity-list";
import { Note } from "../patterns/fact-row";
import { captureDay } from "../timeWords.js";
import { Button } from "../ui/button";
import { Dialog, DialogClose } from "../ui/dialog";
import { FileChooser } from "../ui/file-chooser";
import { Inline, Stack } from "../ui/stack";

/** One paper profile on the list (Central's `PaperProfileView`): `used_by` names the Frames whose
 * paper uses it. */
export type LutView = PaperProfileView;

/** Central's answer to an add (lutsApi.js `addLut`). */
export type AddAnswer = { ok: true; lut: LutView; added: boolean } | { ok: false; error: string };

/** Central's answer to a remove (lutsApi.js `removeLut`). */
export type RemoveAnswer = { ok: true } | { ok: false; error: string; frames: string[] };

/** "MNN", "MNN and Hall", "MNN, Hall and Kitchen". */
function namesWords(names: readonly string[]): string {
  return names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** Each refusal of a paper profile change, in words: every code Central has (a code without words
 * fails the typecheck). */
export const PAPER_PROFILE_REFUSALS: Record<PaperProfilesRefusal, string> = {
  not_a_colour_profile: "it is not a colour profile (an ICC file). Download the .icc profile for your paper from its maker.",
  not_a_printer_profile: "it is a colour profile, but not one for a printer and paper. Choose the profile your paper's "
    + "maker publishes for your printer.",
  too_large: "it is too large for a paper profile.",
  unknown_paper_profile: "it is no longer on the list.",
  named_by_a_frame: "a Frame's paper uses it. Choose another paper profile on that Frame's Lighting tab first.",
};
const UNREACHABLE = "Central did not answer. Try again.";

/** A refusal's words: Central's code in words, or the code itself in a sentence when it has none. */
function refusalWords(code: string): string {
  if (code in PAPER_PROFILE_REFUSALS) return PAPER_PROFILE_REFUSALS[code as PaperProfilesRefusal];
  return code === "unreachable" ? UNREACHABLE : `Central refused it (${code}).`;
}

/** What an add came to, in plain words: `file` is the name of the file chosen. */
export function addWords(file: string, answer: AddAnswer): string {
  if (answer.ok) {
    return answer.added
      ? `Added ${answer.lut.name}. Choose it as a Frame's paper profile, on its Lighting tab.`
      : `${answer.lut.name} is already on the list.`;
  }
  return `Not added (${file}): ${refusalWords(answer.error)}`;
}

/** What a remove came to, in plain words, naming the Frames whose paper uses it. */
export function removeWords(lut: LutView, answer: RemoveAnswer): string {
  if (answer.ok) return `Removed ${lut.name}.`;
  const frames = answer.frames.length === 0 ? "" : ` (${namesWords(answer.frames)})`;
  return `Not removed (${lut.name}): ${refusalWords(answer.error)}${frames}`;
}

type Row = LutView & { key: string; severity: null };

export interface LutsSettingsProps {
  luts: readonly LutView[];
  /** The last add's or remove's words, until the next. */
  note: string | null;
  /** An add is on its way: the chooser waits. */
  adding: boolean;
  onAdd: (file: File) => void;
  /** Resolves once Central has answered (the page then says what came of it). */
  onRemove: (lut: LutView) => Promise<void>;
}

/**
 * Settings › LUTs' list: each LUT by its paper profile's name, when it was added and the Frames
 * whose paper uses it;
 * Add a paper profile (a file from the paper's maker, checked by Central, a refusal in plain words)
 * and Remove, confirmed, which Central refuses while a Frame uses the LUT. The page reads and writes; this
 * is what a story draws.
 */
export function LutsSettings({ luts, note, adding, onAdd, onRemove }: LutsSettingsProps) {
  const [removing, setRemoving] = useState<LutView | null>(null);
  const [busy, setBusy] = useState(false);
  const remove = async () => {
    if (removing === null) return;
    setBusy(true);
    await onRemove(removing);
    setBusy(false);
    setRemoving(null);
  };
  const rows: Row[] = luts.map((lut) => ({ ...lut, key: lut.profile_sha256, severity: null }));
  return (
    <Stack>
      <Note>
        A LUT makes photos look printed on one paper. It is made from the colour profile (an ICC file) your
        paper&apos;s maker publishes for each paper and printer. Choose it as a Frame&apos;s paper profile, under
        Lighting on its Position tab.
      </Note>
      <EntityList<Row>
        label="LUTs"
        columns={[
          { id: "name", header: "Name", rowHeader: true, cell: (row) => row.name },
          { id: "added", header: "Added", cell: (row) => captureDay(row.added_at) },
          { id: "used", header: "Used by", cell: (row) => (row.used_by.length === 0 ? "No Frame" : namesWords(row.used_by)) },
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
      <Inline label="LUT actions">
        <FileChooser accept=".icc,.icm" disabled={adding} onChoose={onAdd}>
          {adding ? "Adding…" : "Add a paper profile"}
        </FileChooser>
      </Inline>
      <Dialog
        open={removing !== null}
        onOpenChange={(open) => { if (!open) setRemoving(null); }}
        busy={busy}
        title={`Remove ${removing?.name ?? "this LUT"}?`}
        actions={(
          <>
            <DialogClose>Cancel</DialogClose>
            <Button variant="danger" disabled={busy} onClick={() => void remove()}>Confirm remove</Button>
          </>
        )}
      >
        <p>Photo Wall forgets this paper profile. To use it again, add its file again.</p>
      </Dialog>
    </Stack>
  );
}
