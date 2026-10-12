import { useState } from "react";

import { EntityList } from "../patterns/entity-list";
import { Note } from "../patterns/fact-row";
import { captureDay } from "../timeWords.js";
import { Button } from "../ui/button";
import { Dialog, DialogClose } from "../ui/dialog";
import { FileChooser } from "../ui/file-chooser";
import { Inline, Stack } from "../ui/stack";

/** One LUT on the list (central/displays/views.py `LutView`, read through lutsApi.js). */
export interface LutView {
  profile_sha256: string;
  name: string;
  added_at: number;
  used_by: string[];
}

/** Central's answer to an add (lutsApi.js `addLut`). */
export type AddAnswer = { ok: true; lut: LutView; added: boolean } | { ok: false; error: string };

/** Central's answer to a remove (lutsApi.js `removeLut`). */
export type RemoveAnswer = { ok: true } | { ok: false; error: string; displays: string[] };

/** "MNN", "MNN and Hall", "MNN, Hall and Kitchen". */
function namesWords(names: readonly string[]): string {
  return names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** What an add came to, in plain words: `file` is the name of the file chosen. */
export function addWords(file: string, answer: AddAnswer): string {
  if (answer.ok) {
    return answer.added
      ? `Added ${answer.lut.name}. Choose it on a Frame's Picture tab, under Photo Wall (software).`
      : `${answer.lut.name} is already on the list.`;
  }
  switch (answer.error) {
    case "not_a_colour_profile":
      return `Not added: ${file} is not a colour profile (an ICC file). Download the .icc profile for your paper from its maker.`;
    case "not_a_printer_profile":
      return `Not added: ${file} is a colour profile, but not one for a printer and paper. `
        + "Choose the profile your paper's maker publishes for your printer.";
    case "too_large":
      return `Not added: ${file} is larger than 8 MB, too large for a paper profile.`;
    case "unreachable":
      return "Not added: Central did not answer. Try again.";
    default:
      return `Not added: Central refused it (${answer.error}).`;
  }
}

/** What a remove came to, in plain words. */
export function removeWords(lut: LutView, answer: RemoveAnswer): string {
  if (answer.ok) return `Removed ${lut.name}.`;
  switch (answer.error) {
    case "lut_in_use":
      return `Not removed: ${namesWords(answer.displays)} ${answer.displays.length === 1 ? "uses" : "use"} ${lut.name}. `
        + "Choose another LUT on their Frame's Picture tab first.";
    case "lut_unknown":
      return `${lut.name} was already removed.`;
    case "unreachable":
      return "Not removed: Central did not answer. Try again.";
    default:
      return `Not removed: Central refused it (${answer.error}).`;
  }
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
 * Settings › LUTs' list (print look, design "The LUT"; design-language §8 row B5, T8): each LUT by
 * its paper profile's name, when it was added and the Displays that use it; Add a paper profile
 * (a file from the paper's maker, checked by Central, a refusal in plain words) and Remove,
 * confirmed, which Central refuses while a Display uses the LUT. The page reads and writes; this
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
        paper&apos;s maker publishes for each paper and printer. Choose it for a Display on a Frame&apos;s Picture tab.
      </Note>
      <EntityList<Row>
        label="LUTs"
        columns={[
          { id: "name", header: "Name", rowHeader: true, cell: (row) => row.name },
          { id: "added", header: "Added", cell: (row) => captureDay(row.added_at) },
          { id: "used", header: "Used by", cell: (row) => (row.used_by.length === 0 ? "No Display" : namesWords(row.used_by)) },
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
